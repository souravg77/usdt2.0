"""Strategy contract: explicit rules in, signals with entry/stop/target out.

Every strategy answers the same questions (the master prompt's BaseStrategy interface):

- ``generate``: every signal over a frame of closed candles (backtests, paper and live all use it,
  so the rules are the same everywhere).
- ``entry_signal``: the signal on the newest closed bar, if any.
- ``exit_signal``: whether an open trade from a signal should close on the newest bar, and why.
- ``invalidation_condition``: whether the reason for the trade is gone (the close is through the
  stop, or the market is no longer a regime the strategy is meant for).
- ``calculate_stop_loss``: the stop for an entry price.
- ``eligible_regimes``: the market regimes (``market.regime.REGIMES``) it may open trades in.
  Signals on bars of any other regime are dropped (``regime_filter``).
- ``no_trade_conditions``: the reasons it must not trade on the newest bar, in plain words.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..indicators import atr, atr_percentile, missing_candles
from ..market.regime import REGIMES, RegimeConfig, regime_labels
from ..models import TIMEFRAME_MS, Side

# The strategy regime gate leaves liquidity to the pair ranking and the liquidity check, which
# judge it with real order books; here a thin market never counts as a regime of its own.
GATE_REGIME = RegimeConfig(min_daily_usdt=0.0)


@dataclass(frozen=True)
class StrategySpec:
    """The rule card written down before coding (roadmap Phase 2, step 1)."""

    name: str
    source: str
    timeframes: tuple[str, ...]
    setup: str
    entry: str
    stop: str
    target: str
    filter: str
    size: str
    decisions: tuple[str, ...] = ()
    regime: str = ""  # the market state the rules are meant for ("" = not stated)


@dataclass(frozen=True)
class Signal:
    """A trade idea. ``ts`` is the close time of the signal bar (UTC ms); the order is meant to go
    in at the next bar's open, so nothing here depends on a bar that has not closed yet.
    ``entry`` is the signal bar's close (the reference price); the backtest fills at the next open."""

    ts: int
    strategy: str
    exchange: str
    symbol: str
    timeframe: str
    side: Side
    entry: float
    stop: float
    target: float | None
    trailing: str | None = None
    risk_pct: float = 0.005
    reason: str = ""
    features: dict = field(default_factory=dict)

    @property
    def r_multiple_target(self) -> float | None:
        if self.target is None:
            return None
        return abs(self.target - self.entry) / abs(self.entry - self.stop)

    def as_row(self) -> dict:
        row = asdict(self)
        row["side"] = self.side.value
        row["features"] = ";".join(f"{k}={v:.4g}" for k, v in self.features.items())
        return row


class Strategy(ABC):
    spec: StrategySpec
    params: dict
    # True when ``generate`` reads market-context columns (``funding_rate``...) that
    # ``data.context.attach_context`` adds to the candle frame. Without them it must not trade.
    needs_context: bool = False

    # market regimes the strategy may open trades in (every one unless a strategy narrows it)
    eligible_regimes: tuple[str, ...] = REGIMES

    def __init__(self, **params):
        self.params = {**self.default_params(), **params}

    @classmethod
    @abstractmethod
    def default_params(cls) -> dict: ...

    @abstractmethod
    def generate(
        self, df: pd.DataFrame, exchange: str, symbol: str, timeframe: str
    ) -> list[Signal]:
        """Evaluate every closed bar in ``df`` and return the signals it produces, oldest first."""

    # ---- the shared interface -------------------------------------------------------------------

    def market_regimes(self, df: pd.DataFrame, timeframe: str) -> pd.Series:
        """The regime label of every bar (only past bars feed each label)."""
        return regime_labels(df, timeframe, GATE_REGIME)

    def regime_filter(self, df: pd.DataFrame, timeframe: str, signals: list[Signal]) -> list[Signal]:
        """``signals`` without those on bars whose regime isn't in ``eligible_regimes``."""
        if not signals or set(REGIMES) <= set(self.eligible_regimes):
            return signals
        step = TIMEFRAME_MS[timeframe]
        labels = dict(zip((df["ts"].to_numpy(np.int64) + step).tolist(), self.market_regimes(df, timeframe).tolist()))
        return [s for s in signals if labels.get(s.ts) in self.eligible_regimes]

    def entry_signal(self, df: pd.DataFrame, exchange: str, symbol: str, timeframe: str) -> Signal | None:
        """The signal on the newest closed bar of ``df``, or None."""
        if not len(df):
            return None
        newest = int(df["ts"].iloc[-1]) + TIMEFRAME_MS[timeframe]
        sigs = [s for s in self.generate(df, exchange, symbol, timeframe) if s.ts == newest]
        return sigs[-1] if sigs else None

    def exit_signal(self, df: pd.DataFrame, signal: Signal) -> str | None:
        """Why a trade opened on ``signal`` closes on the newest bar of ``df`` ("stop", "target"
        or "exit rule: ..."), or None to keep it."""
        from ..backtest.engine import _trailing_exit  # the engine imports strategies

        bar = df.iloc[-1]
        short = signal.side is Side.SELL
        if (bar["high"] >= signal.stop) if short else (bar["low"] <= signal.stop):
            return "stop"
        if signal.target is not None and ((bar["low"] <= signal.target) if short else (bar["high"] >= signal.target)):
            return "target"
        hit = _trailing_exit(signal.trailing, df)
        if hit is not None and len(hit) and bool(hit.iloc[-1]):
            return f"exit rule: {signal.trailing}"
        return None

    def invalidation_condition(self, df: pd.DataFrame, signal: Signal) -> bool:
        """True when the trade's premise is gone on the newest bar: the close is through the stop,
        or the market has moved into a regime the strategy isn't meant for."""
        close = float(df["close"].iloc[-1])
        if (close >= signal.stop) if signal.side is Side.SELL else (close <= signal.stop):
            return True
        return str(self.market_regimes(df, signal.timeframe).iloc[-1]) not in self.eligible_regimes

    def stop_series(self, df: pd.DataFrame) -> pd.Series | None:
        """The stop each bar would use for a long (strategies with a structural stop set it)."""
        return None

    def calculate_stop_loss(self, entry: float, df: pd.DataFrame, side: Side = Side.BUY) -> float:
        """The stop for an entry at ``entry`` on the newest bar: the strategy's own stop when it
        has one below the entry, else ``stop_atr`` (default 2) x ATR(14) away."""
        if side is Side.BUY:
            s = self.stop_series(df)
            if s is not None and len(s):
                v = float(s.iloc[-1])
                if math.isfinite(v) and 0 < v < entry:
                    return v
        a = float(atr(df).iloc[-1]) if len(df) else math.nan
        k = float(self.params.get("stop_atr") or 2.0)
        if not math.isfinite(a) or a <= 0:
            a = 0.02 * entry
        return entry + k * a if side is Side.SELL else entry - k * a

    def no_trade_conditions(self, df: pd.DataFrame, timeframe: str) -> list[str]:
        """Plain-language reasons the strategy must not open a trade on the newest bar."""
        if not len(df):
            return ["no candles"]
        out = []
        clean = int(self.params.get("clean_bars") or 0)
        if clean and (len(df) < clean or missing_candles(df.iloc[-clean:], TIMEFRAME_MS[timeframe]).iloc[1:].any()):
            out.append(f"fewer than {clean} clean candles (warm-up or a missing candle)")
        cap = self.params.get("atr_pct_max")
        if cap is not None:
            rank = float(atr_percentile(df).iloc[-1]) if len(df) else math.nan
            if math.isfinite(rank) and rank > cap:
                out.append(f"ATR at the {rank * 100:.0f}th percentile (disorderly market)")
        label = str(self.market_regimes(df, timeframe).iloc[-1])
        if label not in self.eligible_regimes:
            out.append(f"regime {label} is not one this strategy trades ({', '.join(self.eligible_regimes)})")
        if self.needs_context and ("funding_rate" not in df or pd.isna(df["funding_rate"].iloc[-1])):
            out.append("no fresh funding-rate data")
        return out
