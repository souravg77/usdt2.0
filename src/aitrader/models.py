"""Exchange-neutral data types. Every adapter returns these, so strategy code never sees ccxt."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. ``ts`` is the bar open time in UTC milliseconds."""

    exchange: str
    symbol: str
    timeframe: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBook:
    exchange: str
    symbol: str
    ts: int
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


@dataclass
class Order:
    id: str
    exchange: str
    symbol: str
    side: Side
    type: OrderType
    amount: float
    price: float | None
    status: OrderStatus
    filled: float = 0.0
    average: float | None = None
    fee: float = 0.0
    fee_currency: str | None = None
    ts: int = 0
    reason: str = ""


@dataclass
class Position:
    """Spot holding (base asset) with its average entry price in quote currency."""

    exchange: str
    symbol: str
    amount: float
    entry_price: float
    unrealized_pnl: float = 0.0


@dataclass
class Balance:
    exchange: str
    free: dict[str, float] = field(default_factory=dict)
    used: dict[str, float] = field(default_factory=dict)

    def total(self, asset: str) -> float:
        return self.free.get(asset, 0.0) + self.used.get(asset, 0.0)
