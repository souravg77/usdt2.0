"""Short twins of the long strategies, for Bybit USDT perpetual futures.

Every long strategy in the library has a short twin: the same rules run on the price chart turned
upside down (each price ``p`` becomes ``1 / p``, so highs and lows swap). Where the long rule
buys a dip in an uptrend, its twin sells a bounce in a downtrend; a breakout up becomes a
breakdown; an RSI below 30 becomes an RSI above 70. The twin's long signal on the flipped chart
is turned back into a short on the real one: entry ``1 / e``, stop ``1 / s`` (now *above* the
entry), target ``1 / t`` (below). Its trailing exits are the long rule's exits read on the
flipped chart. Funding-rate columns change sign, so "longs are crowded" reads as "shorts are
crowded".

A twin is only a candidate: it goes through the same backtest gate, holdout and paper trading as
every other strategy, and most will fail. Nothing here sends an order.
"""

from __future__ import annotations

import pandas as pd

from ..models import Side
from .base import Signal, Strategy, StrategySpec

PRICE_COLS = ("open", "high", "low", "close")


def flip(df: pd.DataFrame) -> pd.DataFrame:
    """The chart upside down: 1/price, with high and low swapped. Volume, time and open interest stay;
    funding and the premium index change sign (what longs pay, shorts earn)."""
    out = df.copy()
    out["open"] = 1.0 / df["open"]
    out["close"] = 1.0 / df["close"]
    out["high"] = 1.0 / df["low"]
    out["low"] = 1.0 / df["high"]
    for col in df.columns:
        if col.startswith("funding") or col.startswith("premium"):  # who pays flips with the side
            out[col] = -df[col]
    return out


def _register_exit(part: str) -> str:
    """The key under which the engine finds the flipped version of one trailing-exit rule."""
    from ..backtest.engine import CUSTOM_EXITS, _one_trailing_exit

    if part.startswith("time_stop="):
        return part
    key = f"flipped:{part}"
    if key not in CUSTOM_EXITS:
        CUSTOM_EXITS[key] = lambda df, part=part: _one_trailing_exit(part, flip(df))
    return key


class ShortMirror(Strategy):
    """``base`` (a long strategy) traded short on the flipped chart."""

    def __init__(self, base: Strategy):
        self.base = base
        self.params = dict(base.params)
        self.needs_context = getattr(base, "needs_context", False)
        self.history_bars = getattr(base, "history_bars", 0)
        self.history_align = getattr(base, "history_align", 1)
        b = base.spec
        self.spec = StrategySpec(
            name=f"short: {b.name}", source=b.source, timeframes=b.timeframes,
            setup=f"Upside-down twin of: {b.setup}", entry=f"Sell short (perp) when, on the flipped chart: {b.entry}",
            stop=f"Above the entry: {b.stop} (flipped)", target=f"Below the entry: {b.target} (flipped)",
            filter=b.filter, size=b.size,
            decisions=(*b.decisions, "Short twin: the long rules on the chart turned upside down (1/price); "
                       "traded as a Bybit USDT perpetual short."),
            regime=f"flipped: {b.regime}" if b.regime else "")
        # the base judges regimes on the flipped chart, where a downtrend reads as an uptrend
        swap = {"trend-up": "trend-down", "trend-down": "trend-up"}
        self.eligible_regimes = tuple(swap.get(r, r) for r in base.eligible_regimes)

    @classmethod
    def default_params(cls) -> dict:
        return {}

    def prime(self, df: pd.DataFrame, timeframe: str) -> None:
        """Let a base that learns from the whole chart (the ML strategies) work out the flipped one."""
        if hasattr(self.base, "prime"):
            self.base.prime(flip(df), timeframe)

    def generate(self, df: pd.DataFrame, exchange: str, symbol: str, timeframe: str) -> list[Signal]:
        out = []
        for s in self.base.generate(flip(df), exchange, symbol, timeframe):
            if s.side is not Side.BUY or s.entry <= 0 or s.stop <= 0 or s.stop >= s.entry:
                continue
            trailing = None
            if s.trailing:
                trailing = "|".join(_register_exit(p.strip()) for p in s.trailing.split("|"))
            out.append(Signal(
                ts=s.ts, strategy=self.spec.name, exchange=s.exchange, symbol=s.symbol,
                timeframe=s.timeframe, side=Side.SELL, entry=1.0 / s.entry, stop=1.0 / s.stop,
                target=1.0 / s.target if s.target and s.target > 0 else None, trailing=trailing,
                risk_pct=s.risk_pct, reason=f"short twin: {s.reason}", features=dict(s.features)))
        return out

    def calculate_stop_loss(self, entry: float, df: pd.DataFrame, side: Side = Side.SELL) -> float:
        """The base's long stop on the flipped chart, turned back: above the entry."""
        if side is Side.BUY:
            return super().calculate_stop_loss(entry, df, side)
        return 1.0 / self.base.calculate_stop_loss(1.0 / entry, flip(df), Side.BUY)
