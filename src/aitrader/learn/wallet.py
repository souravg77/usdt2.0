"""The paper trading wallets: two fake-money accounts, one per trading workspace.

- **Spot AI** buys coins (long only, no leverage) from the Spot wallet.
- **Futures AI** trades USDT perpetual futures, long and short, with leverage, from the Futures
  wallet.

Each engine can be turned on or off by itself, and each wallet has its own starting amount,
positions, trade history and results: a spot trade never touches the futures wallet or the other
way round. The header's Spot / Futures switch only chooses which workspace the pages show.

You choose each wallet's starting amount (100 to 10,000 USDT) on the Trading page. Its balance is that amount
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


MARKETS = ("spot", "futures")


def wallet_path(state_path: str, market: str = "spot") -> str:
    name = "paper_wallet_futures.json" if market == "futures" else "paper_wallet.json"
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), name)


def market_of(row: dict) -> str:
    """Which workspace an open position or a closed trade belongs to: shorts and perpetual longs
    are Futures, spot buys are Spot."""
    side = row.get("side") or (row.get("signal") or {}).get("side")
    return "futures" if row.get("perp") or side in ("sell", "short") else "spot"


def engines_path(state_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_engines.json")


def engines(path: str) -> dict:
    """{"spot": on, "futures": on}. Both engines run unless a person turned one off."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {m: data.get(m, True) is not False for m in MARKETS}


def set_engine(path: str, market: str, on: bool, by: str = "") -> dict:
    if market not in MARKETS:
        raise ValueError(f"unknown market {market!r}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data.update({market: bool(on), "by": by, "at": int(time.time() * 1000)})
    _save_market(path, data)
    return engines(path)


FILTERS = ("ai", "teacher")


def filters_path(state_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_filters.json")


def filters(path: str) -> dict:
    """{"spot": {"ai": on, "teacher": on}, "futures": {...}}: whether each engine uses the AI filter
    and the backtest teacher (when they are turned on at all). Both do unless a person said no."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    out = {}
    for m in MARKETS:
        mine = data.get(m) if isinstance(data.get(m), dict) else {}
        out[m] = {f: mine.get(f, True) is not False for f in FILTERS}
    return out


def set_filter(path: str, market: str, name: str, on: bool, by: str = "") -> dict:
    """Let one engine use (or skip) the AI filter or the teacher; the other engine is not changed."""
    if market not in MARKETS or name not in FILTERS:
        raise ValueError(f"unknown engine or filter {market!r} {name!r}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    mine = data.get(market) if isinstance(data.get(market), dict) else {}
    mine.update({name: bool(on), "by": by, "at": int(time.time() * 1000)})
    data[market] = mine
    _save_market(path, data)
    return filters(path)


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
    """Where the Spot / Futures workspace choice (which one the pages show) and the futures
    leverage are kept."""
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_market.json")


def _market(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def futures_on(path: str) -> bool:
    """True when the pages show the Futures workspace (futures prices, wallet and positions).
    It never changes what either engine trades."""
    return _market(path).get("market") == "futures"


def paper_leverage(path: str) -> float | None:
    """The leverage chosen on the Trading page for paper perpetuals, or None (use AITRADER_LEVERAGE)."""
    try:
        lev = _market(path).get("leverage")
        return float(lev) if lev else None
    except (TypeError, ValueError):
        return None


def _save_market(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def set_market(path: str, futures: bool, by: str = "") -> None:
    data = _market(path)
    data.update(market="futures" if futures else "spot", by=by, at=int(time.time() * 1000))
    _save_market(path, data)


def set_leverage(path: str, lev: float, by: str = "") -> None:
    data = _market(path)
    data.update(leverage=lev, by=by, at=int(time.time() * 1000))
    _save_market(path, data)


def _of(row: dict, market: str | None) -> bool:
    return market is None or market_of(row) == market


DEFAULT_DAILY_LOSS_PCT, DEFAULT_MAX_OPEN = 5.0, 30
DAILY_LOSS_RANGE, MAX_OPEN_RANGE = (0.5, 50.0), (1, 200)


def limits_path(state_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_limits.json")


def _clamp_limits(d: dict) -> dict:
    try:
        loss = float(d.get("daily_loss_pct", DEFAULT_DAILY_LOSS_PCT))
    except (TypeError, ValueError):
        loss = DEFAULT_DAILY_LOSS_PCT
    try:
        most = int(d.get("max_open", DEFAULT_MAX_OPEN))
    except (TypeError, ValueError):
        most = DEFAULT_MAX_OPEN
    return {"daily_loss_pct": max(DAILY_LOSS_RANGE[0], min(DAILY_LOSS_RANGE[1], loss)),
            "max_open": max(MAX_OPEN_RANGE[0], min(MAX_OPEN_RANGE[1], most))}


def limits(path: str) -> dict:
    """Each engine's own risk limits: {"spot": {"daily_loss_pct", "max_open"}, "futures": {...}}."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    return {m: _clamp_limits(data.get(m) if isinstance(data.get(m), dict) else {}) for m in MARKETS}


def set_limits(path: str, market: str, daily_loss_pct, max_open, by: str = "") -> dict:
    if market not in MARKETS:
        raise ValueError(f"unknown market {market!r}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[market] = {**_clamp_limits({"daily_loss_pct": daily_loss_pct, "max_open": max_open}), "by": by,
                    "at": int(time.time() * 1000)}
    _save_market(path, data)
    return limits(path)[market]


def day_start_ms(now_ms: int) -> int:
    """Midnight today on the dashboard's clock (AITRADER_TZ, India by default)."""
    from datetime import datetime

    from ..clock import zone

    now = datetime.fromtimestamp(now_ms / 1000, zone())
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def today_pnl(state: dict, market: str, now_ms: int) -> float:
    start = day_start_ms(now_ms)
    return sum(t.get("pnl_usdt", 0.0) for t in state.get("trades", [])
               if t.get("exit_ts", 0) >= start and market_of(t) == market)


def entry_block(state: dict, w: dict, market: str, lim: dict, now_ms: int) -> str:
    """Why this engine may not open a new trade now (its own limits), or ""."""
    name = "Futures AI" if market == "futures" else "Spot AI"
    opened = sum(1 for k, o in state.get("open", {}).items()
                 if (k.endswith("|futures") or market_of(o) == "futures") == (market == "futures"))
    if opened >= lim["max_open"]:
        return f"{name} has {opened} open positions (its limit is {lim['max_open']})"
    today = today_pnl(state, market, now_ms)
    day_start_balance = balance(state, w, market) - today
    if day_start_balance > 0 and today <= -lim["daily_loss_pct"] / 100 * day_start_balance:
        return (f"{name} lost {-today:,.2f} USDT today, its daily limit of {lim['daily_loss_pct']:g}%: "
                "no new trades until midnight")
    return ""


def in_use(state: dict, skip: str = "", market: str | None = None) -> float:
    """USDT tied up by the open positions (of one workspace, when ``market`` is given): what a
    spot position cost, a perp position's margin."""
    return sum(o.get("units", 0) * o.get("entry", 0) / o.get("leverage", 1.0)
               for k, o in state.get("open", {}).items() if k != skip and o.get("filled") and _of(o, market))


def balance(state: dict, w: dict, market: str | None = None) -> float:
    """The starting amount plus every paper trade (of that workspace) closed since the wallet was set."""
    return w["start"] + sum(t.get("pnl_usdt", 0.0) for t in state.get("trades", [])
                            if t.get("exit_ts", 0) >= w["since"] and _of(t, market))


def summary(state: dict, w: dict, market: str | None = None) -> dict:
    bal = balance(state, w, market)
    used = in_use(state, market=market)
    closed = [t for t in state.get("trades", []) if t.get("exit_ts", 0) >= w["since"] and _of(t, market)]
    return {"start": w["start"], "since": w["since"], "balance": bal, "in_use": used,
            "free": max(0.0, bal - used), "closed_pnl": bal - w["start"], "trades": len(closed),
            "open": sum(1 for o in state.get("open", {}).values() if o.get("filled") and _of(o, market))}
