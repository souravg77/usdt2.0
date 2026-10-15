"""The paper trading wallet: one fake-money account every paper strategy trades from.

You choose its starting amount (100 to 10,000 USDT) on the Trading page. Its balance is that amount
plus the profit or loss of every paper trade closed since it was set; choosing a new amount starts
a new wallet (the trade log keeps the old trades). Trade sizes follow the balance: each trade risks
its strategy's share (``risk_pct``, e.g. 0.5%) of it, one position is worth at most half of it, and
all open positions together never tie up more than it. A trade smaller than Bybit's minimum order
(``MIN_ORDER_USDT``) is skipped, as the exchange would refuse it.

The setting is a small JSON file next to the paper state, written by the dashboard and read by the
paper trader on every pass. Fake money only: nothing here touches an exchange.
"""

from __future__ import annotations

import json
import os
import time

MIN_START, MAX_START, DEFAULT_START = 100.0, 10_000.0, 10_000.0
MIN_ORDER_USDT = 5.0  # Bybit's smallest order for most USDT spot pairs and perpetuals


def wallet_path(state_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_wallet.json")


def clamp(amount: float) -> float:
    return max(MIN_START, min(MAX_START, float(amount)))


def load(path: str) -> dict:
    """{"start": USDT, "since": ms}. Without a file: the default amount, counting every trade."""
    try:
        with open(path, encoding="utf-8") as fh:
            w = json.load(fh)
        return {"start": clamp(w["start"]), "since": int(w.get("since", 0))}
    except (OSError, ValueError, KeyError, TypeError):
        return {"start": DEFAULT_START, "since": 0}


def save(path: str, start: float, now_ms: int | None = None) -> dict:
    w = {"start": clamp(start), "since": int(now_ms if now_ms is not None else time.time() * 1000)}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(w, fh)
    os.replace(tmp, path)
    return w


def market_path(state_path: str) -> str:
    """Where the Trading page's Spot / Futures choice for paper longs is kept."""
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_market.json")


def futures_on(path: str) -> bool:
    """True when paper longs trade as USDT perpetual futures instead of spot buys."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("market") == "futures"
    except (OSError, ValueError, AttributeError):
        return False


def set_market(path: str, futures: bool, by: str = "") -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"market": "futures" if futures else "spot", "by": by, "at": int(time.time() * 1000)}, fh)
    os.replace(tmp, path)


def in_use(state: dict, skip: str = "") -> float:
    """USDT tied up by the open positions: what a spot position cost, a perp position's margin."""
    return sum(o.get("units", 0) * o.get("entry", 0) / o.get("leverage", 1.0)
               for k, o in state.get("open", {}).items() if k != skip and o.get("filled"))


def balance(state: dict, w: dict) -> float:
    """The starting amount plus every paper trade closed since the wallet was set."""
    return w["start"] + sum(t.get("pnl_usdt", 0.0) for t in state.get("trades", [])
                            if t.get("exit_ts", 0) >= w["since"])


def summary(state: dict, w: dict) -> dict:
    bal = balance(state, w)
    used = in_use(state)
    closed = [t for t in state.get("trades", []) if t.get("exit_ts", 0) >= w["since"]]
    return {"start": w["start"], "since": w["since"], "balance": bal, "in_use": used,
            "free": max(0.0, bal - used), "closed_pnl": bal - w["start"], "trades": len(closed),
            "open": sum(1 for o in state.get("open", {}).values() if o.get("filled"))}
