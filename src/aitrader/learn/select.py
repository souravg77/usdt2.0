"""Backtest strategies a person picks, over a period they pick (the dashboard's Backtest page and
``learn backtest-select``).

Each strategy runs on its own timeframes over every chosen pair, on the engine's own candles and
costs (Futures AI: the perpetuals, perp fees and funding; Spot AI: spot). It sees the whole stored
history before the period (indicators, ML training), and only trades that open inside the period
count. Nothing moves on the ladder and nothing trades: this only reports.
"""

from __future__ import annotations

import json
import os
import time

from ..backtest import BacktestConfig, metrics, run_backtest
from ..indicators import candles_to_frame
from .lab import MIN_BARS, with_context
from .registry import Entry, make_strategy

DAY_MS = 86_400_000
KEEP = ("trades", "win_rate", "expectancy_r", "total_r", "profit_factor", "max_drawdown_r", "t_stat")
MAX_STRATEGIES = 60
MAX_TRADES = 5000  # trades kept per strategy in the report (for the chart and the download)


def _trade(t) -> dict:
    """One trade as the report keeps it: enough for the R curve, the trade list and the CSV."""
    side = "short" if t.stop > t.entry else "long"
    return {"symbol": t.symbol, "timeframe": t.timeframe, "side": side, "entry_ts": t.entry_ts,
            "exit_ts": t.exit_ts, "entry": t.entry, "exit": t.exit, "stop": t.stop, "target": t.target,
            "exit_reason": t.exit_reason, "r": round(t.r, 4)}


def _m(trades) -> dict:
    m = metrics(trades)
    return {k: (round(m[k], 4) if isinstance(m[k], float) else m[k]) for k in KEEP if k in m}


def period(months: int = 0, start: str = "", end: str = "", now_ms: int | None = None) -> tuple[int, int]:
    """(start, end) in ms: the dates given (YYYY-MM-DD, end day included), else the last ``months``."""
    import datetime as dt

    def day(s: str) -> int:
        d = dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp() * 1000)

    stop = day(end) + DAY_MS if end else int(now_ms if now_ms is not None else time.time() * 1000)
    if start:
        begin = day(start)
    else:
        begin = stop - int(max(1, months) * 30.44 * DAY_MS)
    if begin >= stop:
        raise ValueError("the start must be before the end")
    return begin, stop


def run_selected(entries: list[Entry], store, exchange: str, pairs: list[str], start_ms: int, end_ms: int,
                 cfg: BacktestConfig, progress=None) -> dict:
    """Backtest ``entries`` on ``pairs`` for trades opening in [start_ms, end_ms)."""
    rows, pooled = [], []
    for n, e in enumerate(entries, 1):
        if progress:
            progress(f"[{n}/{len(entries)}] {e.id}")
        try:
            strat = make_strategy(e)
        except Exception as exc:  # a strategy that can't be built: say so, keep going
            rows.append({"id": e.id, "status": e.status, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        trades, by_pair, no_data, late = [], {}, [], {}
        for sym in pairs:
            for tf in strat.spec.timeframes:
                candles = store.load(exchange, sym, tf, None, end_ms)
                if len(candles) < MIN_BARS or candles[-1].ts < start_ms:
                    no_data.append(f"{sym} {tf}")
                    continue
                if candles[MIN_BARS - 1].ts > start_ms:  # stored candles (after the warm-up) start inside the period
                    late[f"{sym} {tf}"] = candles[MIN_BARS - 1].ts
                df = with_context(strat, candles_to_frame(candles), store, exchange, sym, tf)
                got = [t for t in run_backtest(strat, df, exchange, sym, tf, cfg) if start_ms <= t.entry_ts < end_ms]
                trades += got
                if got:
                    by_pair[f"{sym} {tf}"] = _m(got)
        pooled += trades
        trades.sort(key=lambda t: t.exit_ts)
        rows.append({"id": e.id, "status": e.status, "timeframes": list(strat.spec.timeframes),
                     **_m(trades), "by_pair": by_pair, "no_data": no_data, "tested_from": late,
                     "trade_list": [_trade(t) for t in trades[-MAX_TRADES:]]})
    return {"start": start_ms, "end": end_ms, "pairs": pairs, "strategies": rows, "total": _m(pooled)}


def curve(trades: list[dict]) -> list[tuple[int, float]]:
    """Running total in R at each trade's exit, oldest first."""
    total, pts = 0.0, []
    for t in sorted(trades, key=lambda t: t["exit_ts"]):
        total += t["r"]
        pts.append((t["exit_ts"], round(total, 4)))
    return pts


def trades_csv(report: dict) -> str:
    """Every kept trade of every strategy as CSV (times in UTC)."""
    import csv
    import io

    def when(ms):
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else ""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["strategy", "pair", "timeframe", "side", "opened_utc", "closed_utc", "entry", "exit", "stop",
                "target", "exit_reason", "result_r"])
    for r in report.get("strategies", []):
        for t in r.get("trade_list") or []:
            w.writerow([r["id"], t["symbol"], t["timeframe"], t["side"], when(t["entry_ts"]), when(t["exit_ts"]),
                        t["entry"], t["exit"], t["stop"], "" if t["target"] is None else t["target"],
                        t["exit_reason"], t["r"]])
    return buf.getvalue()


def summary_csv(report: dict) -> str:
    """One row per strategy (and per strategy and pair), then all together."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["strategy", "pair", *KEEP])
    for r in report.get("strategies", []):
        if r.get("error"):
            w.writerow([r["id"], "could not run: " + r["error"]])
            continue
        w.writerow([r["id"], "all", *[r.get(k, "") for k in KEEP]])
        for k, m in sorted((r.get("by_pair") or {}).items()):
            w.writerow([r["id"], k, *[m.get(x, "") for x in KEEP]])
    w.writerow(["ALL TOGETHER", "all", *[report.get("total", {}).get(k, "") for k in KEEP]])
    return buf.getvalue()


def save(report: dict, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    os.replace(tmp, path)
    return path


def text(report: dict) -> str:
    fmt = "%Y-%m-%d"
    lines = [f"{report.get('engine', '')} backtest {time.strftime(fmt, time.gmtime(report['start'] / 1000))} to "
             f"{time.strftime(fmt, time.gmtime((report['end'] - 1) / 1000))} on {len(report['pairs'])} pairs, after fees"]
    for r in report["strategies"]:
        if r.get("error"):
            lines.append(f"  {r['id'][:60]:60} could not run: {r['error']}")
            continue
        lines.append(f"  {r['id'][:60]:60} trades={r['trades']:5d} win={r['win_rate']:.0%} "
                     f"avg={r['expectancy_r']:+.3f}R total={r['total_r']:+.1f}R maxDD={r['max_drawdown_r']:.1f}R")
    late = {k: ts for r in report["strategies"] for k, ts in (r.get("tested_from") or {}).items()}
    if late:
        lines.append(f"  NOTE: on {len(late)} pair/timeframes the stored candles start after the period began "
                     f"(latest start {time.strftime(fmt, time.gmtime(max(late.values()) / 1000))}), so only the "
                     "time since then was tested. Tick 'Download the missing candles' to test the whole period.")
    t = report["total"]
    lines.append(f"  {'ALL TOGETHER':60} trades={t['trades']:5d} win={t['win_rate']:.0%} "
                 f"avg={t['expectancy_r']:+.3f}R total={t['total_r']:+.1f}R maxDD={t['max_drawdown_r']:.1f}R")
    return "\n".join(lines)
