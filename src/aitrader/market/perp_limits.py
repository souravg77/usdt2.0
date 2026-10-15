"""Bybit's highest leverage for each USDT perpetual (public market data), for paper futures.

Bybit lets each perpetual go up to its own maximum (100x on BTC, often 25x-50x on smaller coins).
The collector saves these once a day while the dashboard is in Futures mode; the paper trader
then uses the leverage chosen on the Trading page, cut to that coin's maximum. When the list is
missing, the choice is used as it is.
"""

from __future__ import annotations

import json
import os
import time

DEFAULT_PATH = "data/pairs/perp_limits.json"
REFRESH_MS = 86_400_000


def load(path: str = DEFAULT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def max_leverage(symbol: str, path: str = DEFAULT_PATH) -> float | None:
    """Bybit's maximum leverage for ``symbol``'s USDT perpetual (BTC/USDT), or None if unknown."""
    v = (load(path).get("max_leverage") or {}).get(symbol)
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def due(path: str = DEFAULT_PATH, now_ms: int | None = None) -> bool:
    now_ms = now_ms or int(time.time() * 1000)
    return now_ms - int(load(path).get("generated") or 0) >= REFRESH_MS


def from_markets(markets: dict) -> dict:
    """{"BTC/USDT": 100.0, ...} from ccxt's ``load_markets`` (USDT linear perpetuals only)."""
    out = {}
    for m in markets.values():
        if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("settle") == "USDT"):
            continue
        top = ((m.get("limits") or {}).get("leverage") or {}).get("max")
        if top:
            out[f"{m.get('base')}/USDT"] = float(top)
    return out


async def refresh(client, path: str = DEFAULT_PATH, now_ms: int | None = None) -> dict:
    data = {"generated": now_ms or int(time.time() * 1000), "max_leverage": from_markets(await client.load_markets())}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data
