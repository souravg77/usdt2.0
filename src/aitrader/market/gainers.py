"""Top gainers: two scanners, one per trading workspace.

- **Futures** (``DEFAULT_PATH``): Bybit USDT linear perpetuals that are open for trading and also
  listed on spot (where the candles come from). Futures AI trades them.
- **Spot** (``SPOT_PATH``): Bybit USDT spot pairs. Spot AI trades them.

Each scanner has its own switch on the Pairs page, its own count and its own window (1h, 4h or 24h).
A pair must not be a stablecoin or a leveraged token, must have at least ``MIN_TURNOVER`` USDT of
24h turnover, and its bid/ask spread must be at most ``MAX_SPREAD_PCT``. The candidates (the
``CANDIDATES`` biggest 24h risers that pass) are then scored, in the open:

    score = rise over the window (%)
            + 2 x (relative volume - 1), capped at +6   (last hour's volume / the 24h hourly average)
            - 5 x spread (%)

and ranked by score. A pair that looks like a pump (``pump_warning``: up over ``PUMP_1H_PCT`` in an
hour, or over ``PUMP_24H_PCT`` in a day) is listed with a warning but not traded.

Each list is refreshed by the collector about every hour while its switch is on (``REFRESH_MS``)
and by ``aitrader pairs gainers``. It is dropped from trading when it is older than
``MAX_AGE_HOURS`` (the collector stopped); pairs with an open paper position keep getting
candles until it closes.

Today's biggest movers are often the most volatile: big gains are often followed by sharp drops,
spreads widen and stops slip. Every trade still goes through the usual risk limits and fee filter,
and this only changes paper trading: live orders stay locked behind the go/no-go gate.
"""

from __future__ import annotations

import json
import os
import time

from .pairs import is_leveraged_token, is_stablecoin

DEFAULT_PATH = "data/pairs/gainers.json"  # the Futures scanner (its name from before the spot one)
SPOT_PATH = "data/pairs/gainers_spot.json"
MARKETS = ("spot", "futures")
WINDOWS = ("1h", "4h", "24h")
DEFAULT_WINDOW = "24h"
MAX_SPREAD_PCT = 0.2
CANDIDATES = 40
PUMP_1H_PCT, PUMP_24H_PCT = 15.0, 60.0
DEFAULT_TOP = 10
MIN_TOP, MAX_TOP = 3, 30
MIN_TURNOVER = 10_000_000  # USDT of 24h perp turnover
REFRESH_MS = 3_600_000
MAX_AGE_HOURS = 6.0


def path_for(market: str) -> str:
    return SPOT_PATH if market == "spot" else DEFAULT_PATH


def window_of(data: dict) -> str:
    w = data.get("window")
    return w if w in WINDOWS else DEFAULT_WINDOW


def clamp_top(n) -> int:
    try:
        return max(MIN_TOP, min(MAX_TOP, int(n)))
    except (TypeError, ValueError):
        return DEFAULT_TOP


def load(path: str = DEFAULT_PATH) -> dict:
    """{"on", "top", "generated", "pairs": [{"symbol", "change", "turnover", "price"}]}; {} without a file."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict, path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def switch(on: bool, top: int | None = None, path: str = DEFAULT_PATH, by: str = "",
           window: str | None = None) -> dict:
    data = load(path)
    data.update(on=bool(on), top=clamp_top(top if top is not None else data.get("top", DEFAULT_TOP)), by=by,
                window=window if window in WINDOWS else window_of(data))
    save(data, path)
    return data


def symbols(path: str = DEFAULT_PATH, now_ms: int | None = None, max_age_hours: float = MAX_AGE_HOURS) -> list[str]:
    """The gainers to trade now: [] when the switch is off or the list is missing or stale."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    if not data.get("on") or not data.get("generated") or now_ms - int(data["generated"]) > max_age_hours * 3_600_000:
        return []
    return [p["symbol"] for p in data.get("pairs", []) if not p.get("pump")][: clamp_top(data.get("top", DEFAULT_TOP))]


def due(path: str = DEFAULT_PATH, now_ms: int | None = None) -> bool:
    """Whether the collector should refresh the list (switch on and the list over an hour old)."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    return bool(data.get("on")) and now_ms - int(data.get("generated") or 0) >= REFRESH_MS


def _spread_pct(t: dict) -> float | None:
    bid, ask = t.get("bid"), t.get("ask")
    if not bid or not ask or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100


def pick(markets: dict, tickers: dict, top: int = DEFAULT_TOP, min_turnover: float = MIN_TURNOVER,
         market: str = "futures", max_spread: float = MAX_SPREAD_PCT) -> list[dict]:
    """The ``top`` biggest 24h risers that pass the filters, before scoring. ``markets`` and
    ``tickers`` are ccxt's ``load_markets`` / ``fetch_tickers`` results for that market."""
    spot = {m["symbol"] for m in markets.values() if m.get("spot") and m.get("active", True)}
    rows = []
    for sym, m in markets.items():
        if market == "spot":
            if not (m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)):
                continue
        elif not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
                  and m.get("active", True)):
            continue
        base = m.get("base") or ""
        pair = f"{base}/USDT"
        if is_stablecoin(base) or is_leveraged_token(base) or (market != "spot" and pair not in spot):
            continue
        t = tickers.get(sym) or {}
        change, turnover = t.get("percentage"), t.get("quoteVolume")
        if change is None or not turnover or turnover < min_turnover:
            continue
        spread = _spread_pct(t)
        if spread is not None and spread > max_spread:
            continue
        rows.append({"symbol": pair, "contract": sym, "change": round(float(change), 2), "turnover": float(turnover),
                     "price": t.get("last"), "spread": None if spread is None else round(spread, 4)})
    rows.sort(key=lambda r: r["change"], reverse=True)
    return [r for r in rows if r["change"] > 0][:max(1, int(top))]


def pump_warning(row: dict) -> str:
    """Why a pair looks like a pump (listed, not traded), or ""."""
    if (row.get("change_1h") or 0) > PUMP_1H_PCT:
        return f"up {row['change_1h']:.0f}% in an hour"
    if (row.get("change") or 0) > PUMP_24H_PCT:
        return f"up {row['change']:.0f}% in a day"
    return ""


def score(row: dict, window: str = DEFAULT_WINDOW) -> float:
    """The open score: the rise over the window, plus a capped bonus for unusual volume, minus the spread."""
    rise = row.get({"1h": "change_1h", "4h": "change_4h"}.get(window, "change")) or 0.0
    relvol = row.get("relvol")
    bonus = 0.0 if relvol is None else max(-2.0, min(6.0, 2.0 * (relvol - 1.0)))
    return round(rise + bonus - 5.0 * (row.get("spread") or 0.0), 2)


def enrich(row: dict, ohlcv: list) -> dict:
    """Add the 1h and 4h rise and the relative volume from hourly candles ([ts, o, h, l, c, v], oldest
    first, the newest possibly still forming)."""
    closes = [float(c[4]) for c in ohlcv if c and c[4]]
    vols = [float(c[5] or 0) for c in ohlcv if c and c[4]]
    out = dict(row)
    if len(closes) >= 2:
        out["change_1h"] = round((closes[-1] / closes[-2] - 1) * 100, 2)
    if len(closes) >= 5:
        out["change_4h"] = round((closes[-1] / closes[-5] - 1) * 100, 2)
    past = vols[-25:-1]
    if past and sum(past) > 0:
        out["relvol"] = round(vols[-1] / (sum(past) / len(past)), 2)
    return out


def rank(rows: list[dict], window: str = DEFAULT_WINDOW) -> list[dict]:
    """Score, flag pumps, and order: the best first, rises over the window only."""
    out = []
    for r in rows:
        r = {**r, "score": score(r, window), "pump": pump_warning(r)}
        rise = r.get({"1h": "change_1h", "4h": "change_4h"}.get(window, "change"))
        if rise is not None and rise > 0:
            out.append(r)
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def spot_client(exchange_id: str = "bybit"):
    """A ccxt async client for public spot data."""
    import ccxt.async_support as ccxt_async

    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def perp_client(exchange_id: str = "bybit"):
    """A ccxt async client for public data whose tickers are the USDT linear perpetuals."""
    import ccxt.async_support as ccxt_async

    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True,
                                             "options": {"defaultType": "swap", "defaultSubType": "linear"}})


def client_for(market: str, exchange_id: str = "bybit"):
    return spot_client(exchange_id) if market == "spot" else perp_client(exchange_id)


async def refresh(client, path: str = DEFAULT_PATH, top: int | None = None, now_ms: int | None = None,
                  market: str | None = None, window: str | None = None) -> dict:
    """Scan, score and save one market's gainers (the switch stays as it is)."""
    import logging

    market = market or ("spot" if os.path.abspath(path) == os.path.abspath(SPOT_PATH) else "futures")
    data = load(path)
    top = clamp_top(top if top is not None else data.get("top", DEFAULT_TOP))
    window = window if window in WINDOWS else window_of(data)
    markets = await client.load_markets()
    tickers = await client.fetch_tickers()
    rows = []
    for r in pick(markets, tickers, top=CANDIDATES, market=market):
        try:
            r = enrich(r, await client.fetch_ohlcv(r["contract"], "1h", limit=26))
        except Exception as exc:  # one coin's candles failing must not stop the scan
            logging.getLogger(__name__).warning("gainers: no candles for %s: %s", r["symbol"], exc)
        rows.append(r)
    data.update(top=top, window=window, market=market, generated=now_ms or int(time.time() * 1000),
                scanned=len(rows), pairs=rank(rows, window)[:MAX_TOP])  # ``top`` decides what trades
    save(data, path)
    return data
