"""Strategy contract: explicit rules in, signals with entry/stop/target out."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

import pandas as pd

from ..models import Side


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
