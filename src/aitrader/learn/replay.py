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
from .lab import MIN_BARS
from .registry import Entry, make_strategy

WARMUP_BARS = 300
MAX_BARS = 20_000


def replay(entries: list[Entry], store: CandleStore, exchange: str, symbol: str, timeframe: str,
           start_ms: int, end_ms: int, only: str = "", cfg: BacktestConfig | None = None) -> dict:
    step = TIMEFRAME_MS[timeframe]
    end_ms = min(end_ms, start_ms + MAX_BARS * step)
    rows = store.load(exchange, symbol, timeframe, start=start_ms - WARMUP_BARS * step, end=end_ms)
    shown = [c for c in rows if c.ts >= start_ms]
    out = {"symbol": symbol, "timeframe": timeframe, "start": start_ms, "end": end_ms,
           "candles": [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in shown],
           "trades": [], "strategies": [], "skipped": []}
    if not shown:
        return out
    if len(rows) < MIN_BARS:
        out["skipped"].append({"id": "*", "why": f"only {len(rows)} candles; strategies need {MIN_BARS}"})
        return out
    df = candles_to_frame(rows)
    for entry in entries:
        if only and entry.id != only:
            continue
        try:
            strat = make_strategy(entry)
        except Exception as exc:
            out["skipped"].append({"id": entry.id, "why": f"can't build it ({type(exc).__name__})"})
            continue
        if timeframe not in strat.spec.timeframes:
            continue
        out["strategies"].append({"id": entry.id, "name": entry.name, "source": entry.source})
        for t in run_backtest(strat, df, exchange, symbol, timeframe, cfg or BacktestConfig()):
            if t.entry_ts < start_ms:
                continue
            out["trades"].append({"strategy": entry.id, "entry_ts": t.entry_ts, "entry": t.entry, "stop": t.stop,
                                  "target": t.target, "exit_ts": t.exit_ts, "exit": t.exit,
                                  "reason": t.exit_reason, "r": round(t.r, 3)})
    out["trades"].sort(key=lambda t: t["entry_ts"])
    return out
