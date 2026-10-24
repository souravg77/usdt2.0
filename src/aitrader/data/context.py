"""Market context: funding, open interest and the state of the whole market (roadmap Phase 2,
step 3, the "fundamental layer").

These series never create a trade. ``strategies/context_filter.py`` reads them to veto or shrink
an entry, and ``attach_context`` adds funding and open interest to candle frames for the Phase 4
trade filter.

Sources (all free, no key):

- Bybit USDT perpetuals through ccxt (``bybit``, linear contracts): funding-rate history (one
  value per settlement, usually 8h), open-interest history (1h, the last 29 days are fetched) and
  the long/short account ratio (1h). A spot pair ``BTC/USDT`` maps to the perpetual
  ``BTC/USDT:USDT``; rows are stored under the spot symbol so they line up with candles.
- Bybit's premium index (1h klines of the perpetual against Bybit's index price, through ccxt's
  ``fetch_premium_index_ohlcv``): the basis. Each hour is stored at its close, when it is known.
  ``context backfill`` fetches years of funding, open interest and premium index, so the ML
  strategies that read them (``ml_boosted_fund``, ``ml_ensemble_fund``) have them for every
  candle they train on.
- CoinGecko ``/api/v3/global`` for BTC dominance (percent), total market cap (USD) and its 24h
  change (a fraction: -0.08 is -8%), and ``/api/v3/coins/markets?ids=tether,usd-coin`` for the
  stablecoin (USDT + USDC) market cap. These are snapshots, stored under
  ``exchange="global", symbol="*"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Iterable

import numpy as np
import pandas as pd

from ..config import EXCHANGE
from ..models import TIMEFRAME_MS
from .store import CandleStore

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
GLOBAL = ("global", "*")
SOURCE_EXCHANGE = EXCHANGE  # derivatives data comes from Bybit's USDT perpetuals

# How often each series gets a new value. A value older than twice this is stale: the filter
# ignores it and ``attach_context`` stops carrying it forward.
REFRESH_MS = {
    "funding_rate": 8 * HOUR_MS,
    "open_interest": HOUR_MS,
    "long_short_ratio": HOUR_MS,
    "premium_index": HOUR_MS,
    "btc_dominance": HOUR_MS,
    "total_mcap": HOUR_MS,
    "total_mcap_change_24h": HOUR_MS,
    "stable_mcap": HOUR_MS,
}
SYMBOL_METRICS = ("funding_rate", "open_interest", "long_short_ratio", "premium_index")
# the columns ``attach_context`` adds to candle frames
ATTACHED = ("funding_rate", "open_interest", "premium_index")
GLOBAL_METRICS = ("btc_dominance", "total_mcap", "total_mcap_change_24h", "stable_mcap")

COINGECKO = "https://api.coingecko.com/api/v3"
COINGECKO_GLOBAL = f"{COINGECKO}/global"
COINGECKO_STABLES = f"{COINGECKO}/coins/markets?vs_currency=usd&ids=tether,usd-coin"

OI_HISTORY_DAYS = 29  # open interest and long/short history fetched on first run
HISTORY_DAYS = 1100  # ``context backfill``: about three years, the ML strategies' training span


def stale_after(metric: str) -> int:
    return 2 * REFRESH_MS.get(metric, HOUR_MS)


class ContextStore:
    """The ``context`` table, sharing the candle database (schema migration v2)."""

    def __init__(self, store: CandleStore | str):
        self.candles = CandleStore(store) if isinstance(store, str) else store
        self._own = isinstance(store, str)
        self.candles.migrate()
        self.conn = self.candles.conn

    def upsert(self, rows: Iterable[tuple[str, str, str, int, float]]) -> int:
        """Rows are ``(exchange, symbol, metric, ts, value)``."""
        rows = [(e, s, m, int(t), float(v)) for e, s, m, t, v in rows
                if v is not None and np.isfinite(float(v))]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO context VALUES (?,?,?,?,?)", rows)
        return len(rows)

    def load(self, metric: str, exchange: str = "global", symbol: str = "*",
             start: int | None = None, end: int | None = None) -> list[tuple[int, float]]:
        """``(ts, value)`` pairs in time order, ``start <= ts < end``."""
        q = "SELECT ts, value FROM context WHERE exchange=? AND symbol=? AND metric=?"
        args: list = [exchange, symbol, metric]
        if start is not None:
            q += " AND ts>=?"
            args.append(start)
        if end is not None:
            q += " AND ts<?"
            args.append(end)
        return [(int(t), float(v)) for t, v in self.conn.execute(q + " ORDER BY ts", args)]

    def value_at(self, metric: str, exchange: str, symbol: str,
                 at: int | None = None) -> tuple[int, float] | None:
        """The newest ``(ts, value)`` with ``ts <= at`` (default: the newest of all)."""
        q = "SELECT ts, value FROM context WHERE exchange=? AND symbol=? AND metric=?"
        args: list = [exchange, symbol, metric]
        if at is not None:
            q += " AND ts<=?"
            args.append(at)
        row = self.conn.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        return (int(row[0]), float(row[1])) if row else None

    def last_ts(self, metric: str, exchange: str, symbol: str) -> int | None:
        row = self.value_at(metric, exchange, symbol)
        return row[0] if row else None

    def summary(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT exchange, symbol, metric, COUNT(*), MIN(ts), MAX(ts) FROM context "
            "GROUP BY exchange, symbol, metric ORDER BY 1,2,3"
        ).fetchall()

    def close(self) -> None:
        if self._own:
            self.candles.close()


def has_context(store) -> bool:
    """True when ``store``'s database has context rows (never creates the table)."""
    conn = getattr(store, "conn", None)
    if conn is None:
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context'").fetchone():
        return False
    return conn.execute("SELECT 1 FROM context LIMIT 1").fetchone() is not None


# ---- fetchers ---------------------------------------------------------------------------------


def perp_symbol(symbol: str) -> str:
    """``BTC/USDT`` -> ``BTC/USDT:USDT`` (Bybit USDT linear perpetual)."""
    if ":" in symbol:
        return symbol
    return f"{symbol}:{symbol.split('/')[1]}"


def make_client():
    import ccxt.async_support as ccxt_async

    return ccxt_async.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})


async def _paged(fetch, since: int, now: int, limit: int, max_pages: int = 1000) -> list[dict]:
    out: list[dict] = []
    for _ in range(max_pages):
        rows = await fetch(since, limit)
        rows = [r for r in rows if r.get("timestamp") is not None and int(r["timestamp"]) >= since]
        if not rows:
            break
        out += rows
        last = max(int(r["timestamp"]) for r in rows)
        if len(rows) < limit or last >= now:
            break
        since = last + 1
    return out


async def fetch_funding(client, symbol: str, since: int, now: int | None = None,
                        limit: int = 200) -> list[tuple[int, float]]:
    now = now or int(time.time() * 1000)
    rows = await _paged(lambda s, n: client.fetch_funding_rate_history(perp_symbol(symbol), since=s, limit=n),
                        since, now, limit)
    return sorted({int(r["timestamp"]): float(r["fundingRate"]) for r in rows
                   if r.get("fundingRate") is not None}.items())


async def fetch_open_interest(client, symbol: str, since: int, now: int | None = None,
                              timeframe: str = "1h", limit: int = 200,
                              max_days: int = OI_HISTORY_DAYS) -> list[tuple[int, float]]:
    """Open interest in coins (falls back to USD value when the amount is missing)."""
    now = now or int(time.time() * 1000)
    since = max(since, now - max_days * DAY_MS)
    rows = await _paged(lambda s, n: client.fetch_open_interest_history(
        perp_symbol(symbol), timeframe, since=s, limit=n), since, now, limit)
    out = {}
    for r in rows:
        v = r.get("openInterestAmount")
        v = r.get("openInterestValue") if v is None else v
        if v is not None:
            out[int(r["timestamp"])] = float(v)
    return sorted(out.items())


async def fetch_long_short(client, symbol: str, since: int, now: int | None = None,
                           timeframe: str = "1h", limit: int = 500) -> list[tuple[int, float]]:
    """Global long/short account ratio; empty when this ccxt version does not expose it."""
    has = getattr(client, "has", {}) or {}
    fetch = getattr(client, "fetch_long_short_ratio_history", None)
    if fetch is None or not has.get("fetchLongShortRatioHistory"):
        return []
    now = now or int(time.time() * 1000)
    since = max(since, now - OI_HISTORY_DAYS * DAY_MS)
    rows = await _paged(lambda s, n: fetch(perp_symbol(symbol), timeframe, since=s, limit=n),
                        since, now, limit)
    return sorted({int(r["timestamp"]): float(r["longShortRatio"]) for r in rows
                   if r.get("longShortRatio") is not None}.items())


async def fetch_premium_index(client, symbol: str, since: int, now: int | None = None,
                              timeframe: str = "1h", limit: int = 1000) -> list[tuple[int, float]]:
    """The perpetual's premium index (its basis against the index price) per hour, stamped at each
    hour's close so a candle never reads an hour that had not finished yet."""
    now = now or int(time.time() * 1000)
    step = TIMEFRAME_MS[timeframe]
    out: dict[int, float] = {}
    for _ in range(1000):
        rows = await client.fetch_premium_index_ohlcv(perp_symbol(symbol), timeframe, since=since, limit=limit)
        rows = [r for r in rows if r and r[0] is not None and int(r[0]) >= since and r[4] is not None]
        for r in rows:
            if int(r[0]) + step <= now:  # only finished hours
                out[int(r[0]) + step] = float(r[4])
        if len(rows) < limit:
            break
        since = max(int(r[0]) for r in rows) + 1
        if since >= now:
            break
    return sorted(out.items())


def _get_json(url: str, urlopen=None, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "aitrader/0.1 (market context)",
                                               "Accept": "application/json"})
    with (urlopen or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_global(urlopen=None, now: int | None = None, timeout: float = 15.0) -> dict[str, tuple[int, float]]:
    """CoinGecko snapshot: ``{metric: (ts, value)}``. Each half may fail on its own."""
    now = now or int(time.time() * 1000)
    out: dict[str, tuple[int, float]] = {}
    try:
        data = _get_json(COINGECKO_GLOBAL, urlopen, timeout)["data"]
        ts = int(data.get("updated_at") or now / 1000) * 1000
        dom = (data.get("market_cap_percentage") or {}).get("btc")
        mcap = (data.get("total_market_cap") or {}).get("usd")
        chg = data.get("market_cap_change_percentage_24h_usd")
        if dom is not None:
            out["btc_dominance"] = (ts, float(dom))
        if mcap is not None:
            out["total_mcap"] = (ts, float(mcap))
        if chg is not None:
            out["total_mcap_change_24h"] = (ts, float(chg) / 100)
    except Exception as exc:
        log.warning("coingecko global fetch failed: %s", exc)
    try:
        coins = _get_json(COINGECKO_STABLES, urlopen, timeout)
        caps = [float(c["market_cap"]) for c in coins if c.get("market_cap") is not None]
        if caps:
            out["stable_mcap"] = (now, sum(caps))
    except Exception as exc:
        log.warning("coingecko stablecoin fetch failed: %s", exc)
    return out


async def collect_context(store: ContextStore, symbols: list[str], exchange: str = SOURCE_EXCHANGE,
                          client=None, urlopen=None, backfill_days: int = 30,
                          now_ms: int | None = None, history: bool = False) -> dict[str, int]:
    """Top up every context series. Returns rows written per metric. A failing source is logged
    and skipped; the others still update. ``history`` fetches the last ``backfill_days`` again
    even where rows exist (``context backfill``: open interest and premium index too, years back)."""
    now = now_ms or int(time.time() * 1000)
    own = client is None
    client = client or make_client()
    added = {m: 0 for m in SYMBOL_METRICS + GLOBAL_METRICS}
    oi_days = backfill_days if history else OI_HISTORY_DAYS

    async def oi(client, sym, since, now):
        return await fetch_open_interest(client, sym, since, now, max_days=oi_days)

    fetchers = {"funding_rate": fetch_funding, "open_interest": oi,
                "long_short_ratio": fetch_long_short, "premium_index": fetch_premium_index}
    try:
        for sym in symbols:
            for metric, fetch in fetchers.items():
                last = store.last_ts(metric, exchange, sym)
                since = last + 1 if last is not None and not history else now - backfill_days * DAY_MS
                try:
                    pairs = await fetch(client, sym, since, now)
                except Exception as exc:
                    log.warning("%s %s %s failed: %s", exchange, sym, metric, exc)
                    continue
                added[metric] += store.upsert((exchange, sym, metric, t, v) for t, v in pairs)
    finally:
        if own:
            await client.close()
    snap = await asyncio.to_thread(fetch_global, urlopen, now)
    for metric, (t, v) in snap.items():
        added[metric] += store.upsert([(*GLOBAL, metric, t, v)])
    return added


# ---- candles -----------------------------------------------------------------------------------


def _timeframe_ms(df: pd.DataFrame, timeframe: str | None) -> int:
    if timeframe:
        return TIMEFRAME_MS[timeframe]
    diffs = np.diff(df["ts"].to_numpy())
    return int(np.median(diffs)) if len(diffs) else HOUR_MS


def attach_context(df: pd.DataFrame, store: ContextStore, exchange: str, symbol: str,
                   timeframe: str | None = None) -> pd.DataFrame:
    """Copy of ``df`` with ``funding_rate``, ``open_interest`` and ``premium_index`` columns.

    Each bar sees the newest value stamped at or before its close (``ts + timeframe``), never a
    later one, and only while that value is fresh (``stale_after``); otherwise NaN. Rows stored
    under ``exchange`` are used when there are any, else the Bybit perpetual's.
    """
    out = df.copy()
    if out.empty:
        for metric in ATTACHED:
            out[metric] = np.nan
        return out
    step = _timeframe_ms(out, timeframe)
    close = pd.DataFrame({"_close": out["ts"].astype("int64").to_numpy() + step,
                          "_pos": np.arange(len(out))}).sort_values("_close")
    for metric in ATTACHED:
        ex = exchange if store.last_ts(metric, exchange, symbol) is not None else SOURCE_EXCHANGE
        rows = store.load(metric, ex, symbol)
        if not rows:
            out[metric] = np.nan
            continue
        series = pd.DataFrame(rows, columns=["_close", metric]).astype({"_close": "int64"})
        merged = pd.merge_asof(close, series, on="_close", direction="backward",
                               allow_exact_matches=True, tolerance=stale_after(metric))
        out[metric] = merged.sort_values("_pos")[metric].to_numpy()
    return out


def mirror_context(src: ContextStore, dst: ContextStore, metrics=ATTACHED) -> int:
    """Copy the perpetuals' series (funding, open interest, premium index) from ``src`` into ``dst``,
    rows newer than what ``dst`` has: Futures AI's own database gets them too, so its strategy test
    reads them without opening Spot AI's. Returns rows copied."""
    total = 0
    for exchange, symbol, metric in src.conn.execute(
            "SELECT DISTINCT exchange, symbol, metric FROM context WHERE exchange != 'global'").fetchall():
        if metric not in metrics:
            continue
        last = dst.last_ts(metric, exchange, symbol)
        rows = src.load(metric, exchange, symbol, start=None if last is None else last + 1)
        total += dst.upsert((exchange, symbol, metric, t, v) for t, v in rows)
    return total
