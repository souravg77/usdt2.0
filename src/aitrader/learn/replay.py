"""Replay backtest: one pair, one timeframe, from a chosen month, candle by candle.

The server runs every strategy that trades the chosen timeframe over the window (with enough
earlier candles to warm up the indicators) and returns the candles plus every trade. The page
then plays the candles one at a time and shows each trade when its candle appears. Signals use
only closed candles, so the result is the same as if the agent had watched those candles live.
"""

from __future__ import annotations

from ..backtest import BacktestConfig, run_backtest
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from ..models import TIMEFRAME_MS
from . import picks
from .lab import MIN_BARS
from .registry import Entry, make_strategy

WARMUP_BARS = 300
MAX_BARS = 20_000


def replay(entries: list[Entry], store: CandleStore, exchange: str, symbol: str, timeframe: str,
           start_ms: int, end_ms: int, only: str | list[str] = "", cfg: BacktestConfig | None = None,
           top: int = 0, tf_picks: dict | None = None) -> dict:
    """``only``: one strategy id, or a list of ids best first; with ``top``, the first ``top`` of
    that list that trade ``timeframe`` (so "the best 10" means 10 that can trade this chart).
    ``tf_picks`` (``learn.picks``): on a picked timeframe only the strategies kept for it trade."""
    step = TIMEFRAME_MS[timeframe]
    end_ms = min(end_ms, start_ms + MAX_BARS * step)
    rows = store.load(exchange, symbol, timeframe, start=start_ms - WARMUP_BARS * step, end=end_ms)
    shown = [c for c in rows if c.ts >= start_ms]
    out = {"symbol": symbol, "timeframe": timeframe, "start": start_ms, "end": end_ms,
           "candles": [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in shown],
           "trades": [], "strategies": [], "skipped": [], "fee_skipped": 0,
           "max_cost_r": (cfg or BacktestConfig()).max_cost_r}
    if not shown:
        return out
    if len(rows) < MIN_BARS:
        out["skipped"].append({"id": "*", "why": f"only {len(rows)} candles; strategies need {MIN_BARS}"})
        return out
    df = candles_to_frame(rows)
    if isinstance(only, str):
        chosen = [e for e in entries if not only or e.id == only]
    else:
        by_id = {e.id: e for e in entries}
        chosen = [by_id[i] for i in only if i in by_id]
    for entry in chosen:
        if top and len(out["strategies"]) >= top:
            break
        try:
            strat = make_strategy(entry)
        except Exception as exc:
            out["skipped"].append({"id": entry.id, "why": f"can't build it ({type(exc).__name__})"})
            continue
        if timeframe not in picks.allowed(entry, strat.spec.timeframes, tf_picks or {}):
            continue
        out["strategies"].append({"id": entry.id, "name": entry.name, "source": entry.source})
        costly: list = []
        for t in run_backtest(strat, df, exchange, symbol, timeframe, cfg or BacktestConfig(), costly=costly):
            if t.entry_ts < start_ms:
                continue
            side = "sell" if t.stop > t.entry else "buy"  # a short's stop sits above its entry
            out["trades"].append({"strategy": entry.id, "side": side, "entry_ts": t.entry_ts, "entry": t.entry, "stop": t.stop,
                                  "target": t.target, "exit_ts": t.exit_ts, "exit": t.exit,
                                  "reason": t.exit_reason, "r": round(t.r, 3)})
        out["fee_skipped"] += sum(1 for s in costly if s.ts >= start_ms)
    out["trades"].sort(key=lambda t: t["entry_ts"])
    return out
