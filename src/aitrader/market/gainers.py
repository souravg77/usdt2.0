"""Top gainers: the Bybit USDT perpetual futures that rose most in the last 24 hours.

A switch on the Pairs page ("Trade the top gainers") adds them to the pairs the collector, the
paper trader and the Markets page use, next to the usual list. Each kept pair must be:

- a USDT-settled linear perpetual on Bybit that is open for trading, so a short can trade it as a
  future (the strategies' short twins, isolated margin at ``AITRADER_LEVERAGE``);
- also listed on Bybit spot, where the candles come from and long trades go;
- not a stablecoin or a leveraged token, with at least ``min_turnover`` USDT of 24h perp turnover.

The list is refreshed by the collector about every hour while the switch is on (``REFRESH_MS``)
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

DEFAULT_PATH = "data/pairs/gainers.json"
DEFAULT_TOP = 10
MIN_TOP, MAX_TOP = 3, 30
MIN_TURNOVER = 10_000_000  # USDT of 24h perp turnover
REFRESH_MS = 3_600_000
MAX_AGE_HOURS = 6.0


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


def switch(on: bool, top: int | None = None, path: str = DEFAULT_PATH, by: str = "") -> dict:
    data = load(path)
    data.update(on=bool(on), top=clamp_top(top if top is not None else data.get("top", DEFAULT_TOP)), by=by)
    save(data, path)
    return data


def symbols(path: str = DEFAULT_PATH, now_ms: int | None = None, max_age_hours: float = MAX_AGE_HOURS) -> list[str]:
    """The gainers to trade now: [] when the switch is off or the list is missing or stale."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    if not data.get("on") or not data.get("generated") or now_ms - int(data["generated"]) > max_age_hours * 3_600_000:
        return []
    return [p["symbol"] for p in data.get("pairs", [])][: clamp_top(data.get("top", DEFAULT_TOP))]


def due(path: str = DEFAULT_PATH, now_ms: int | None = None) -> bool:
    """Whether the collector should refresh the list (switch on and the list over an hour old)."""
    data = load(path)
    now_ms = now_ms or int(time.time() * 1000)
    return bool(data.get("on")) and now_ms - int(data.get("generated") or 0) >= REFRESH_MS


def pick(markets: dict, tickers: dict, top: int = DEFAULT_TOP, min_turnover: float = MIN_TURNOVER) -> list[dict]:
    """The ``top`` biggest 24h risers among Bybit's USDT linear perpetuals that also trade on spot.
    ``markets`` and ``tickers`` are ccxt's ``load_markets`` / ``fetch_tickers`` results."""
    spot = {m["symbol"] for m in markets.values() if m.get("spot") and m.get("active", True)}
    rows = []
    for sym, m in markets.items():
        if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
                and m.get("active", True)):
            continue
        base = m.get("base") or ""
        pair = f"{base}/USDT"
        if is_stablecoin(base) or is_leveraged_token(base) or pair not in spot:
            continue
        t = tickers.get(sym) or {}
        change, turnover = t.get("percentage"), t.get("quoteVolume")
        if change is None or not turnover or turnover < min_turnover:
            continue
        rows.append({"symbol": pair, "change": round(float(change), 2), "turnover": float(turnover),
                     "price": t.get("last")})
    rows.sort(key=lambda r: r["change"], reverse=True)
    return [r for r in rows if r["change"] > 0][:clamp_top(top)]


def perp_client(exchange_id: str = "bybit"):
    """A ccxt async client for public data whose tickers are the USDT linear perpetuals."""
    import ccxt.async_support as ccxt_async

    return getattr(ccxt_async, exchange_id)({"enableRateLimit": True,
                                             "options": {"defaultType": "swap", "defaultSubType": "linear"}})


async def refresh(client, path: str = DEFAULT_PATH, top: int | None = None, now_ms: int | None = None) -> dict:
    """Fetch today's gainers and save them (the switch stays as it is)."""
    data = load(path)
    top = clamp_top(top if top is not None else data.get("top", DEFAULT_TOP))
    markets = await client.load_markets()
    tickers = await client.fetch_tickers()
    data.update(top=top, generated=now_ms or int(time.time() * 1000),
                pairs=pick(markets, tickers, top=MAX_TOP))  # keep a longer list; ``top`` decides what trades
    save(data, path)
    return data
