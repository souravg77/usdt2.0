"""One pass of agents A-G over a pair, plus the candidate signals and the reasons for abstaining
(the dashboard's signal and abstention stream).

Nothing here trades: a candidate still has to go through the risk manager (J), the executor's
checks and, for real money, the live lock.
"""

from __future__ import annotations

import math

import pandas as pd

from .core import (BreakoutAgent, DerivativesAgent, IndicatorAgent, MarketDataAgent, RegimeAgent, ReversalAgent,
                   TrendAgent)
from .messages import Board


def default_strategies():
    """The catalogue's trading strategies, without the slow ML baseline."""
    from ..strategies.catalog import tradeable

    return [e.cls for e in tradeable() if e.name != "ml_classifier"]


def analyse(df: pd.DataFrame, exchange: str, symbol: str, timeframe: str, strategies=None, quote=None,
            book: dict | None = None, deriv_snapshot=None, now_ms: int | None = None) -> Board:
    """Run A-G on the closed candles in ``df`` and ask each strategy for a signal on the newest bar.
    ``strategies``: classes or instances (default: the catalogue)."""
    strategies = [s() if isinstance(s, type) else s for s in (strategies or default_strategies())]
    a = MarketDataAgent().run(df, symbol, timeframe, now_ms=now_ms, quote=quote, book=book)
    funding = None
    if "funding_rate" in df and len(df) and pd.notna(df["funding_rate"].iloc[-1]):
        funding = float(df["funding_rate"].iloc[-1])
    b = IndicatorAgent().run(df, symbol, timeframe, funding)
    c = TrendAgent().run(b)
    d = ReversalAgent().run(df, b)
    e = BreakoutAgent().run(df, b)
    history = None
    if "funding_rate" in df:
        fr = df["funding_rate"].dropna()
        history = list(fr[fr.ne(fr.shift())].iloc[-10:])  # one value per funding period
    f = DerivativesAgent().run(symbol, deriv_snapshot, funding_history=history, ts=b.ts)
    g = RegimeAgent().run(df, symbol, timeframe, [type(s) for s in strategies])
    candidates, abstentions = [], []
    for s in strategies:
        name = s.spec.name
        if not a.ok:
            abstentions.append({"strategy": name, "reason": "market data: " + "; ".join(a.problems)})
            continue
        if g.no_trade:
            abstentions.append({"strategy": name, "reason": f"regime {g.regime}: {g.no_trade_reason}"})
            continue
        why = s.no_trade_conditions(df, timeframe)
        if why:
            abstentions.append({"strategy": name, "reason": why[0]})
            continue
        try:
            sig = s.entry_signal(df, exchange, symbol, timeframe)
        except Exception as exc:  # one broken strategy never hides the others
            abstentions.append({"strategy": name, "reason": f"error: {type(exc).__name__}: {exc}"})
            continue
        if sig is None:
            abstentions.append({"strategy": name, "reason": "no setup on this bar"})
            continue
        side = 1 if sig.side.value == "buy" else -1
        # the share of the trend, breakout and reversal views that point the same way, times how
        # clearly the market fits its regime; a description, not a chance of winning
        agree = [x for x in (c.direction, e.breakout, d.candidate) if x]
        confidence = (sum(1 for x in agree if x == side) / len(agree)) if agree else 0.5
        if g.confidence and math.isfinite(g.confidence):
            confidence *= g.confidence
        candidates.append({"strategy": name, "side": sig.side.value, "entry": sig.entry, "stop": sig.stop,
                           "target": sig.target, "reason": sig.reason, "regime": g.regime,
                           "confidence": round(confidence, 3)})
    msgs = {m.agent: m.as_dict() for m in (a, b, c, d, e, f, g)}
    return Board("board", b.ts, symbol, timeframe, msgs, candidates, abstentions)
