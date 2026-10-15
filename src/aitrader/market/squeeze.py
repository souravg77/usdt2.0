"""Squeeze scanner: Futures AI pairs that are waking up after a quiet spell, not ones already up.

The top gainers list finds coins *after* a big move. This scanner (an idea from a prompt Shawn
shared, 2026-10-15) looks for the moment before: a Bybit USDT perpetual whose 1h Bollinger Bands
were squeezed tight and whose last closed hourly candle traded far more volume than usual.

- Candidates: the same filters as the Futures top gainers (open USDT linear perpetual, also listed on
  spot where the candles come from, not a stablecoin or leveraged token, at least ``MIN_TURNOVER`` USDT
  of 24h turnover, spread at most ``MAX_SPREAD_PCT``), up or down: the ``CANDIDATES`` most traded.
- Squeezed: the narrowest Bollinger Band width (20, 2) of the last ``RECENT`` closed hours is in the
  lowest ``WIDTH_PCT`` of the last ``WEEK`` hours (7 days).
- Waking up: the last closed hour's volume is more than ``MIN_Z`` standard deviations above the
  average of the ``VOL_BARS`` hours before it.

Closed candles only. Pairs are ranked by that volume z-score. The list joins the Futures AI pairs
while its switch is on and fresh (``symbols``), next to the top gainers; the strategies still decide
whether and which way to trade, with the usual risk limits. It changes paper trading only.
"""

from __future__ import annotations

import logging
import os
import time

import numpy as np

from . import gainers
from .pairs import is_leveraged_token, is_stablecoin

DEFAULT_PATH = "data/pairs/squeeze.json"
CANDIDATES = 80
WEEK, RECENT, VOL_BARS = 168, 6, 48
WIDTH_PCT = 0.10
MIN_Z = 2.0
BB_N, BB_K = 20, 2.0
DEFAULT_TOP, MIN_TOP, MAX_TOP = 5, 1, 15
REFRESH_MS = gainers.REFRESH_MS
MAX_AGE_HOURS = gainers.MAX_AGE_HOURS
HOUR_MS = 3_600_000

log = logging.getLogger(__name__)


def clamp_top(n) -> int:
    try:
        return max(MIN_TOP, min(MAX_TOP, int(n)))
    except (TypeError, ValueError):
        return DEFAULT_TOP


def load(path: str = DEFAULT_PATH) -> dict:
    return gainers.load(path)


def switch(on: bool, top: int | None = None, path: str = DEFAULT_PATH, by: str = "") -> dict:
    data = load(path)
    data.update(on=bool(on), top=clamp_top(top if top is not None else data.get("top", DEFAULT_TOP)), by=by)
    gainers.save(data, path)
    return data


def symbols(path: str = DEFAULT_PATH, now_ms: int | None = None, max_age_hours: float = MAX_AGE_HOURS) -> list[str]:
    """The squeeze pairs to trade now: [] when the switch is off or the list is missing or stale."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    if not data.get("on") or not data.get("generated") or now_ms - int(data["generated"]) > max_age_hours * HOUR_MS:
        return []
    return [p["symbol"] for p in data.get("pairs", [])][: clamp_top(data.get("top", DEFAULT_TOP))]


def due(path: str = DEFAULT_PATH, now_ms: int | None = None) -> bool:
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    return bool(data.get("on")) and now_ms - int(data.get("generated") or 0) >= REFRESH_MS


def candidates(markets: dict, tickers: dict, n: int = CANDIDATES) -> list[dict]:
    """The ``n`` most traded perpetuals that pass the top gainers' filters, rising or falling."""
    rows = []
    for sym, m in markets.items():
        if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
                and m.get("active", True)):
            continue
        base = m.get("base") or ""
        pair = f"{base}/USDT"
        spot = markets.get(pair) or {}
        if is_stablecoin(base) or is_leveraged_token(base) or not (spot.get("spot") and spot.get("active", True)):
            continue
        t = tickers.get(sym) or {}
        turnover = t.get("quoteVolume")
        if not turnover or turnover < gainers.MIN_TURNOVER:
            continue
        spread = gainers._spread_pct(t)
        if spread is not None and spread > gainers.MAX_SPREAD_PCT:
            continue
        rows.append({"symbol": pair, "contract": sym, "turnover": float(turnover), "price": t.get("last"),
                     "change": None if t.get("percentage") is None else round(float(t["percentage"]), 2),
                     "spread": None if spread is None else round(spread, 4)})
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return rows[:max(1, int(n))]


def measure(ohlcv: list, now_ms: int) -> dict | None:
    """From hourly candles ([ts, o, h, l, c, v], oldest first): the squeeze and volume numbers, using
    closed candles only. None when there are too few."""
    bars = [c for c in ohlcv if c and c[4] and int(c[0]) + HOUR_MS <= now_ms]
    if len(bars) < WEEK + BB_N:
        return None
    close = np.array([float(c[4]) for c in bars])
    vol = np.array([float(c[5] or 0) for c in bars])
    win = np.lib.stride_tricks.sliding_window_view(close, BB_N)
    mid, sd = win.mean(axis=1), win.std(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        width = np.where(mid > 0, 2 * BB_K * sd / mid, np.nan)[-WEEK:]
    if not np.isfinite(width).all():
        return None
    recent = width[-RECENT:].min()
    pct = float((width <= recent * (1 + 1e-9)).mean())  # share of the week at or below the recent low
    past = vol[-VOL_BARS - 1:-1]
    sdv = past.std()
    z = 0.0 if sdv == 0 else float((vol[-1] - past.mean()) / sdv)
    move = float((close[-1] / close[-2] - 1) * 100)
    return {"width_pct": round(pct, 3), "vol_z": round(z, 2), "change_1h": round(move, 2),
            "squeezed": pct <= WIDTH_PCT, "waking": z > MIN_Z}


def rank(rows: list[dict]) -> list[dict]:
    """The pairs that are squeezed and waking up, the biggest volume jump first."""
    hits = [r for r in rows if r.get("squeezed") and r.get("waking")]
    return sorted(hits, key=lambda r: r["vol_z"], reverse=True)


async def refresh(client, path: str = DEFAULT_PATH, top: int | None = None, now_ms: int | None = None) -> dict:
    """Scan Bybit perpetuals and save the squeeze list (the switch stays as it is)."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    markets = await client.load_markets()
    tickers = await client.fetch_tickers()
    rows = []
    for r in candidates(markets, tickers):
        try:
            m = measure(await client.fetch_ohlcv(r["contract"], "1h", limit=WEEK + BB_N + 6), now_ms)
        except Exception as exc:  # one coin's candles failing must not stop the scan
            log.warning("squeeze: no candles for %s: %s", r["symbol"], exc)
            continue
        if m:
            rows.append({**r, **m})
    data.update(top=clamp_top(top if top is not None else data.get("top", DEFAULT_TOP)), generated=now_ms,
                scanned=len(rows), pairs=rank(rows)[:MAX_TOP])
    gainers.save(data, path)
    return data


def path_from_env(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("AITRADER_SQUEEZE_FILE") or DEFAULT_PATH

