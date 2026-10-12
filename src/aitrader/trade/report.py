"""Reports for people: the weekly paper-vs-backtest review (roadmap Phase 3/5), the daily summary
and the ``/status`` text the Telegram bot and the CLI print.

The weekly report compares each paper strategy's results with its out-of-sample backtest and
flags any strategy whose paper expectancy is more than ``max_gap_r`` away once it has at least
``min_trades`` paper trades. For the executors it compares each real fill with its paper twin,
so slippage and missed fills show up.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone

from ..backtest import metrics

DAY_MS = 86_400_000


class _R:
    def __init__(self, r):
        self.r, self.cost_r = float(r), 0.0


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def weekly(registry_path: str = "data/learn/registry.json",
           paper_state: str = "data/learn/paper_state.json",
           executor_states: dict[str, str] | None = None, max_gap_r: float = 0.15,
           min_trades: int = 20, now: float | None = None) -> dict:
    from ..learn import Registry

    now_ms = int((now or time.time()) * 1000)
    week_ms = now_ms - 7 * DAY_MS
    reg = Registry(registry_path)
    paper = _load(paper_state)
    trades = paper.get("trades", [])
    rows = []
    for e in reg.entries.values():
        mine = [t for t in trades if t.get("entry_id") == e.id]
        if not mine and e.status != "paper":
            continue
        m = metrics([_R(t["r"]) for t in mine])
        week = [t for t in mine if t.get("exit_ts", 0) >= week_ms]
        bt = (e.backtest or {}).get("out_of_sample") or {}
        bt_exp = bt.get("expectancy_r")
        gap = None if bt_exp is None or not mine else m["expectancy_r"] - bt_exp
        flagged = gap is not None and m["trades"] >= min_trades and abs(gap) > max_gap_r
        rows.append({"id": e.id, "name": e.name, "status": e.status, "paper_trades": m["trades"],
                     "week_trades": len(week), "week_r": round(sum(t["r"] for t in week), 3),
                     "paper_expectancy_r": round(m["expectancy_r"], 4),
                     "backtest_expectancy_r": None if bt_exp is None else round(bt_exp, 4),
                     "gap_r": None if gap is None else round(gap, 4), "flagged": flagged,
                     "max_drawdown_r": round(m["max_drawdown_r"], 3)})
    rows.sort(key=lambda r: (not r["flagged"], r["id"]))
    execs = {}
    for mode, path in (executor_states or {}).items():
        st = _load(path)
        closed = st.get("closed", [])
        if not closed and not st.get("positions"):
            continue
        twins = [t for t in closed if t.get("paper_r") is not None]
        week = [t for t in closed if t.get("exit_ts", 0) >= week_ms]
        execs[mode] = {
            "trades": len(closed), "week_trades": len(week),
            "week_pnl_usdt": round(sum(t.get("pnl_usdt", 0) for t in week), 2),
            "expectancy_r": round(metrics([_R(t["r"]) for t in closed])["expectancy_r"], 4) if closed else 0.0,
            "vs_paper_r": round(sum(t["r"] - t["paper_r"] for t in twins) / len(twins), 4) if twins else None,
            "slip_entry_bps": _avg(t.get("slip_entry_bps") for t in twins),
            "slip_exit_bps": _avg(t.get("slip_exit_bps") for t in twins),
            "open": sum(1 for p in st.get("positions", {}).values() if p.get("status") == "open"),
        }
    return {"at": now_ms, "max_gap_r": max_gap_r, "min_trades": min_trades, "strategies": rows,
            "executors": execs, "flagged": [r["id"] for r in rows if r["flagged"]]}


def _avg(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def weekly_text(rep: dict) -> str:
    lines = [f"Weekly report, {_day(rep['at'])}"]
    if not rep["strategies"]:
        lines.append("No paper strategies yet.")
    for r in rep["strategies"]:
        bt = "n/a" if r["backtest_expectancy_r"] is None else f"{r['backtest_expectancy_r']:+.3f}R"
        gap = "" if r["gap_r"] is None else f", gap {r['gap_r']:+.3f}R"
        flag = "  <- CHECK: paper is far from the backtest" if r["flagged"] else ""
        lines.append(f"- {r['id']} ({r['status']}): week {r['week_trades']} trades {r['week_r']:+.2f}R; "
                     f"all {r['paper_trades']} trades {r['paper_expectancy_r']:+.3f}R vs backtest {bt}"
                     f"{gap}{flag}")
    for mode, x in rep["executors"].items():
        vs = "" if x["vs_paper_r"] is None else f", {x['vs_paper_r']:+.3f}R per trade vs paper"
        slip = "" if x["slip_entry_bps"] is None else \
            f", slippage in {x['slip_entry_bps']:+.1f} / out {x['slip_exit_bps']:+.1f} bps"
        lines.append(f"- {mode}: week {x['week_trades']} trades {x['week_pnl_usdt']:+.2f} USDT; "
                     f"{x['trades']} in all{vs}{slip}; {x['open']} open")
    if rep["flagged"]:
        lines.append(f"Flagged (gap over {rep['max_gap_r']:g}R after {rep['min_trades']}+ trades): "
                     + ", ".join(rep["flagged"]))
    return "\n".join(lines)


def daily_text(day: date, paper_state: str, executor_states: dict[str, str]) -> str:
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    end = start + DAY_MS
    lines = [f"Daily summary {day.isoformat()} (UTC)"]
    sources = {"paper": _load(paper_state).get("trades", [])}
    for mode, path in executor_states.items():
        sources[mode] = _load(path).get("closed", [])
    for name, closed in sources.items():
        day_t = [t for t in closed if start <= t.get("exit_ts", 0) < end]
        if not day_t and name != "paper":
            continue
        wins = sum(1 for t in day_t if t["r"] > 0)
        lines.append(f"- {name}: {len(day_t)} closed, {wins} won, {sum(t['r'] for t in day_t):+.2f}R, "
                     f"{sum(t.get('pnl_usdt', 0) for t in day_t):+.2f} USDT")
    return "\n".join(lines)


def status_text(settings, control, paper_state: str, executor_states: dict[str, str]) -> str:
    from ..safety import live_gate_problems

    c = control.state()
    sw = "KILLED" if c["killed"] else "PAUSED" if c["paused"] else "running"
    lines = [f"mode {settings.mode.value}; live trading {'UNLOCKED' if not live_gate_problems(settings) else 'locked'}; trading {sw}"
             + (f" ({c['reason']})" if c.get("reason") else "")]
    paper = _load(paper_state)
    today = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    pt = [t for t in paper.get("trades", []) if t.get("exit_ts", 0) >= today]
    lines.append(f"paper: {len(paper.get('open', {}))} open, today {len(pt)} closed "
                 f"{sum(t['r'] for t in pt):+.2f}R")
    for mode, path in executor_states.items():
        st = _load(path)
        if not st:
            continue
        opn = [p for p in st.get("positions", {}).values() if p.get("status") == "open"]
        ct = [t for t in st.get("closed", []) if t.get("exit_ts", 0) >= today]
        eq = st.get("last_equity")
        lines.append(f"{mode}: {len(opn)} open, today {len(ct)} closed "
                     f"{sum(t.get('pnl_usdt', 0) for t in ct):+.2f} USDT"
                     + (f", equity {eq:,.2f}" if eq else ""))
        for p in opn[:5]:
            lines.append(f"  {p['symbol']} {p.get('amount', 0):g} @ {p.get('entry', 0):g} stop {p['stop']:g}")
    return "\n".join(lines)


def executor_state_paths(root: str = "data/trade") -> dict[str, str]:
    return {m: os.path.join(root, f"{m}_state.json") for m in ("live",)}


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
