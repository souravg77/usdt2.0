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
        trades, by_pair, no_data = [], {}, []
        for sym in pairs:
            for tf in strat.spec.timeframes:
                candles = store.load(exchange, sym, tf, None, end_ms)
                if len(candles) < MIN_BARS or candles[-1].ts < start_ms:
                    no_data.append(f"{sym} {tf}")
                    continue
                df = with_context(strat, candles_to_frame(candles), store, exchange, sym, tf)
                got = [t for t in run_backtest(strat, df, exchange, sym, tf, cfg) if start_ms <= t.entry_ts < end_ms]
                trades += got
                if got:
                    by_pair[f"{sym} {tf}"] = _m(got)
        pooled += trades
        rows.append({"id": e.id, "status": e.status, "timeframes": list(strat.spec.timeframes),
                     **_m(trades), "by_pair": by_pair, "no_data": no_data})
    return {"start": start_ms, "end": end_ms, "pairs": pairs, "strategies": rows, "total": _m(pooled)}


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
            lines.append(f"  {r['id'][:50]:50} could not run: {r['error']}")
            continue
        lines.append(f"  {r['id'][:50]:50} trades={r['trades']:5d} win={r['win_rate']:.0%} "
                     f"avg={r['expectancy_r']:+.3f}R total={r['total_r']:+.1f}R maxDD={r['max_drawdown_r']:.1f}R")
    t = report["total"]
    lines.append(f"  {'ALL TOGETHER':50} trades={t['trades']:5d} win={t['win_rate']:.0%} "
                 f"avg={t['expectancy_r']:+.3f}R total={t['total_r']:+.1f}R maxDD={t['max_drawdown_r']:.1f}R")
    return "\n".join(lines)
