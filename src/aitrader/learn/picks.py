"""Timeframe picks: on one chart timeframe (say 4h), trade only the strategies that have made money
there, steadily.

``learn pick-timeframe 4h`` backtests every strategy that trades 4h on every pair's saved 4h
candles (after fees, slippage and the fee filter), splits each one's trades into calendar quarters
(the 3-month windows of the Replay page) and keeps a strategy when, over at least
``MIN_TRADES`` trades, its total is positive AND it made money in at least as many quarters as it
lost in. One good quarter is often luck: a strategy that tops one window is often near the bottom
of the next, so the quarter rule asks for steadiness, not a single lucky run.

What a pick changes, in paper trading and on the Replay page:

* each kept strategy gets a copy that trades that timeframe only, named "WIN 4h: <name>" (id
  ``win4h-<id>``, status paper), so it stands out on every page and in the trade log;
* every other strategy stops trading that timeframe (its other timeframes carry on).

The backtest report and keep-top still test everything, so a later pick can change its mind.
``learn pick-timeframe 4h --clear`` undoes it. The picks are a JSON file next to the registry.

Each engine has its own picks (``--market spot`` or ``futures``): a Futures pick tests the Futures
AI pairs with perpetual costs, saves to ``timeframe_picks_futures.json`` and makes "WIN 4h Futures"
copies (id ``fwin4h-<id>``) that only Futures AI trades; Spot AI's picks and copies are the
original ones and only Spot AI trades them.

"Winners only" (a switch on the Trading page, ``winners_only.json`` next to the registry, one per
engine): that engine's paper trading then opens trades for the WIN copies only, so the best-N
strategies of keep-top stop opening their own there. Their open trades still close normally, and switching it off brings them back.
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone

from ..backtest import BacktestConfig, run_backtest
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from .lab import MIN_BARS, with_context
from .registry import Entry, Registry, make_strategy

MIN_TRADES = 10
PICKED = "kept for its timeframe pick"


def picks_path(registry_path: str, market: str = "spot") -> str:
    name = "timeframe_picks_futures.json" if market == "futures" else "timeframe_picks.json"
    return os.path.join(os.path.dirname(os.path.abspath(registry_path)), name)


def load(path: str) -> dict:
    """{timeframe: {"at", "picked": [ids], "results": [...], ...}}; {} without a file."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: str, picks: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(picks, fh, indent=1)
    os.replace(tmp, path)


def winners_only_path(registry_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(registry_path)), "winners_only.json")


def winners_only(registry_path: str, market: str = "spot") -> bool:
    """Whether ``market``'s engine (Spot AI or Futures AI) trades only the WIN strategies. An older
    file with one "on" for both counts for each engine until one is set on its own."""
    try:
        with open(winners_only_path(registry_path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if market in data:
        return bool(data[market])
    return bool(data.get("on"))


def set_winners_only(registry_path: str, on: bool, by: str = "", market: str | None = None) -> None:
    """Switch WIN strategies only for one engine (``market``), or for both when it is None."""
    path = winners_only_path(registry_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {m: winners_only(registry_path, m) for m in ("spot", "futures")}
    for m in ([market] if market else ["spot", "futures"]):
        data[m] = bool(on)
    data.update({"by": by, "at": int(time.time() * 1000)})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def trades_now(entry: Entry, only_winners: bool, market: str | None = None, kept: set | None = None) -> bool:
    """Whether ``market``'s engine opens paper trades for ``entry`` (status aside). A pick copy
    trades only on the engine whose pick made it; any other strategy only on an engine whose own
    "Keep only the best" ranking (``kept``, its ids; None before its first ranking) kept it."""
    if market and picked_for(entry) and pick_market(entry) != market:
        return False
    if market and not picked_for(entry) and kept is not None and entry.id not in kept:
        return False
    return not only_winners or bool(picked_for(entry))


def pick_market(entry: Entry) -> str:
    """The engine whose pick made this copy: "futures", or "spot" (every copy from before the
    engines had their own picks); "" for a strategy that isn't a pick copy."""
    if not picked_for(entry):
        return ""
    return "futures" if (entry.spec or {}).get("pick_market") == "futures" else "spot"


def picked_for(entry: Entry) -> str:
    """The timeframe a pick copy was made for, or ""."""
    return (entry.spec or {}).get("pick", "") or ""


def is_copy(entry: Entry) -> bool:
    """A pick copy, a short twin of one, or another twin: judged through the strategy it came from."""
    spec = entry.spec or {}
    return bool(spec.get("pick") or (spec.get("base_spec") or {}).get("pick") or spec.get("twin_of"))


def allowed(entry: Entry, timeframes, picks: dict) -> list[str]:
    """``timeframes`` without the picked ones this strategy wasn't kept for."""
    mine = picked_for(entry)
    return [tf for tf in timeframes if tf == mine or tf not in picks]


def _quarter(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def evaluate(entries: list[Entry], store: CandleStore, exchange: str, symbols: list[str], timeframe: str,
             since_ms: int | None = None, cfg: BacktestConfig | None = None,
             until_ms: int | None = None) -> list[dict]:
    """Each strategy that trades ``timeframe``: its trades on every pair entered from ``since_ms`` to
    ``until_ms``, totalled and by quarter. The candles before ``since_ms`` only warm the indicators up."""
    from ..models import TIMEFRAME_MS

    cfg = cfg or BacktestConfig()
    warm = since_ms - MIN_BARS * TIMEFRAME_MS[timeframe] if since_ms is not None else None
    frames = {}
    for sym in symbols:
        candles = store.load(exchange, sym, timeframe, start=warm, end=until_ms)
        if len(candles) >= MIN_BARS:
            frames[sym] = candles_to_frame(candles)
    out = []
    for entry in entries:
        if is_copy(entry):
            continue  # copies: the strategy they were made from is judged instead
        try:
            strat = make_strategy(entry)
        except Exception:
            continue
        if timeframe not in strat.spec.timeframes:
            continue
        trades = []
        for sym, df in frames.items():
            try:
                trades += [t for t in run_backtest(strat, with_context(strat, df, store, exchange, sym, timeframe),
                                                   exchange, sym, timeframe, cfg)
                           if since_ms is None or t.entry_ts >= since_ms]
            except Exception:
                continue
        quarters: dict[str, float] = {}
        for t in trades:
            q = _quarter(t.exit_ts)
            quarters[q] = quarters.get(q, 0.0) + t.r
        wins = sum(1 for t in trades if t.r > 0)
        out.append({"id": entry.id, "name": entry.name, "trades": len(trades),
                    "win_rate": round(100 * wins / len(trades), 1) if trades else 0.0,
                    "total_r": round(sum(t.r for t in trades), 2),
                    "quarters_up": sum(1 for r in quarters.values() if r > 0),
                    "quarters_down": sum(1 for r in quarters.values() if r <= 0),
                    "quarters": {q: round(r, 2) for q, r in sorted(quarters.items())}})
    out.sort(key=lambda r: r["total_r"], reverse=True)
    return out


def why_not(r: dict, min_trades: int = MIN_TRADES) -> str:
    """Why a tested strategy was not kept ("" when it was a winner)."""
    if r["trades"] < min_trades:
        return f"only {r['trades']} trades (needs {min_trades})"
    if r["total_r"] <= 0:
        return f"lost money overall ({r['total_r']:+.2f}R after fees)"
    if r["quarters_up"] < r["quarters_down"]:
        return f"down in more quarters than up ({r['quarters_up']} of {r['quarters_up'] + r['quarters_down']} up)"
    return ""


def winners(results: list[dict], min_trades: int = MIN_TRADES) -> list[dict]:
    return [r for r in results if r["trades"] >= min_trades and r["total_r"] > 0
            and r["quarters_up"] >= r["quarters_down"]]


def pick_copy(entry: Entry, timeframe: str, market: str = "spot") -> Entry:
    """A copy of ``entry`` that trades ``timeframe`` only, on ``market``'s engine."""
    spec = copy.deepcopy(entry.spec or {})
    if entry.kind == "short":
        base = dict(spec.get("base_spec") or {})
        base["timeframes"] = [timeframe]
        spec["base_spec"] = base
    else:
        spec["timeframes"] = [timeframe]
        if entry.kind == "builtin":  # its new name no longer names the hand-coded strategy
            spec["builtin"] = entry.name
    spec["pick"], spec["pick_of"] = timeframe, entry.id
    if market == "futures":
        spec["pick_market"] = "futures"
    label = f"WIN {timeframe} Futures" if market == "futures" else f"WIN {timeframe}"
    return Entry(id=copy_id(entry.id, timeframe, market), kind=entry.kind, name=f"{label}: {entry.name}",
                 spec=spec, source=entry.source)


def copy_id(entry_id: str, timeframe: str, market: str = "spot") -> str:
    return f"{'f' if market == 'futures' else ''}win{timeframe}-{entry_id}"


def apply(reg: Registry, entries: list[Entry], timeframe: str, results: list[dict], picks_file: str,
          info: dict | None = None, min_trades: int = MIN_TRADES, market: str = "spot") -> dict:
    """Paper-trade a ``timeframe`` copy of each winner on ``market``'s engine, retire that engine's
    copies of earlier picks that no longer win, and save the pick (``info`` is stored with it:
    pairs, period...)."""
    by_id = {e.id: e for e in entries}
    keep = [r for r in winners(results, min_trades) if r["id"] in by_id]
    keep_ids = {copy_id(r["id"], timeframe, market) for r in keep}
    added, back, dropped = [], [], []
    for r in keep:
        c = pick_copy(by_id[r["id"]], timeframe, market)
        if reg.add(c):
            c = reg.entries[c.id]
            c.set_status("paper", f"{PICKED} {timeframe}: {r['total_r']:+.2f}R over {r['trades']} trades, "
                                  f"up in {r['quarters_up']} of {r['quarters_up'] + r['quarters_down']} quarters")
            added.append(c.id)
        elif reg.entries[c.id].status != "paper":
            reg.entries[c.id].set_status("paper", f"{PICKED} {timeframe} again")
            back.append(c.id)
    for e in reg.entries.values():
        if (picked_for(e) == timeframe and pick_market(e) == market and e.id not in keep_ids
                and e.status == "paper"):
            e.set_status("retired", f"not kept by the new {timeframe} pick")
            dropped.append(e.id)
    reg.save()
    picks = load(picks_file)
    picks[timeframe] = {"at": int(time.time() * 1000), "min_trades": min_trades, "market": market,
                        "picked": [r["id"] for r in keep], "results": results, **(info or {})}
    save(picks_file, picks)
    return {"picked": [r["id"] for r in keep], "added": added, "returned": back, "dropped": dropped,
            "tested": len(results)}


def clear(reg: Registry, timeframe: str, picks_file: str, market: str = "spot") -> list[str]:
    """Undo ``market``'s pick: every strategy trades ``timeframe`` again on that engine and its
    pick copies stop."""
    gone = []
    for e in reg.entries.values():
        if picked_for(e) == timeframe and pick_market(e) == market and e.status == "paper":
            e.set_status("retired", f"the {timeframe} pick was undone")
            gone.append(e.id)
    reg.save()
    picks = load(picks_file)
    picks.pop(timeframe, None)
    save(picks_file, picks)
    return gone
