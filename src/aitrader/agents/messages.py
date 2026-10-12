"""Typed messages the agents (A-L) exchange. Each one says which agent wrote it, when, and the
schema version, and turns into plain JSON with ``as_dict``.

Messages are descriptions. None of them places an order: only the executor does that, after the
risk manager's ``RiskDecision`` (agent J) and the live lock.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Agent G's labels (the master prompt's names for market/regime.py's labels)
REGIME_ENUM = {"trend-up": "TRENDING_BULL", "trend-down": "TRENDING_BEAR", "range": "RANGING",
               "high-volatility": "HIGH_VOLATILITY", "low-liquidity": "LOW_LIQUIDITY", "uncertain": "UNCERTAIN"}


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


@dataclass(frozen=True)
class Message:
    agent: str  # "A".."L"
    ts: int  # ms, the newest closed bar (or the moment) the message describes

    def as_dict(self) -> dict:
        return _clean({"schema": SCHEMA_VERSION, "type": type(self).__name__, **asdict(self)})


@dataclass(frozen=True)
class MarketSnapshot(Message):  # A
    symbol: str = ""
    timeframe: str = ""
    bars: int = 0
    last_close: float = math.nan
    missing_candles: int = 0
    duplicate_candles: int = 0
    candles_stale: bool = False
    feed_age_s: float | None = None
    feed_stale: bool = False
    abnormal_moves: int = 0
    spread_bps: float | None = None
    depth_usdt: float | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class FeatureReport(Message):  # B
    symbol: str = ""
    timeframe: str = ""
    results: dict[str, dict] = field(default_factory=dict)  # name -> IndicatorResult.as_dict()
    quality: float = 0.0  # share of the 20 indicators that are valid
    invalid: list[str] = field(default_factory=list)
    uncertainty: float = 1.0  # 1 - mean confidence of the valid ones


@dataclass(frozen=True)
class TrendView(Message):  # C
    symbol: str = ""
    direction: int = 0
    strength: float = 0.0  # ADX / 100
    confidence: float = 0.0
    votes: dict[str, int] = field(default_factory=dict)
    timeframes: dict[str, int] = field(default_factory=dict)  # timeframe -> direction
    consensus: float = 0.0  # share of timeframes agreeing with ``direction``


@dataclass(frozen=True)
class ReversalView(Message):  # D
    symbol: str = ""
    ranging: bool = False
    divergence: str | None = None  # "bullish" / "bearish" in the last few bars
    extremes: dict[str, str] = field(default_factory=dict)  # oscillator -> zone
    candidate: int = 0  # +1 / -1 only when an extreme is confirmed (divergence or rejection)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BreakoutView(Message):  # E
    symbol: str = ""
    squeeze: bool = False
    expansion: bool = False
    volume_anomaly: bool = False
    breakout: int = 0
    failed_breakout: bool = False
    relative_volume: float = math.nan


@dataclass(frozen=True)
class DerivativesView(Message):  # F
    symbol: str = ""
    basis: float | None = None
    mark_premium: float | None = None
    funding_rate: float | None = None
    funding_annualized: float | None = None
    funding_velocity: float | None = None  # change vs the average of the previous payments
    open_interest_change: float | None = None
    liquidation_distance: float | None = None
    round_trip_cost: float = 0.0  # fees + slippage for opening and closing a perp position
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RegimeView(Message):  # G
    symbol: str = ""
    label: str = "uncertain"
    regime: str = "UNCERTAIN"
    confidence: float = 0.0
    reason: str = ""
    eligible: list[str] = field(default_factory=list)  # strategies that may trade in it
    no_trade: bool = False
    no_trade_reason: str = ""


@dataclass(frozen=True)
class ResearchReport(Message):  # H
    strategy: str = ""
    dataset: str = ""  # fingerprint of the candles used
    params: dict[str, Any] = field(default_factory=dict)
    folds: list[dict] = field(default_factory=list)
    sensitivity: list[dict] = field(default_factory=list)
    stable: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioView(Message):  # I
    selected: list[str] = field(default_factory=list)
    clusters: dict[str, list[str]] = field(default_factory=dict)
    correlation: dict[str, dict[str, float]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision(Message):  # J
    symbol: str = ""
    approved: bool = False
    units: float = 0.0
    notional: float = 0.0
    risk_usdt: float = 0.0
    leverage: float = 1.0
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionReport(Message):  # K
    mismatches: list[dict] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)  # held on the exchange, not in the ledger
    ok: bool = True


@dataclass(frozen=True)
class AuditReport(Message):  # L
    strategy: str = ""
    trades: int = 0
    live_expectancy: float | None = None
    backtest_expectancy: float | None = None
    slippage_r: float | None = None  # live minus paper/backtest R per trade
    degraded: bool = False
    ladder_state: str = ""
    may_trade_live: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Board(Message):
    """One pass of A-G for a pair: what every agent said, the candidate signals and why each
    strategy that didn't trade abstained."""

    symbol: str = ""
    timeframe: str = ""
    messages: dict[str, dict] = field(default_factory=dict)  # agent letter -> message dict
    candidates: list[dict] = field(default_factory=list)
    abstentions: list[dict] = field(default_factory=list)
