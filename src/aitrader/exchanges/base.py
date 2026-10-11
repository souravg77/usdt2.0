"""The one interface every exchange (real or simulated) implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Balance, Candle, Order, OrderBook, OrderType, Position, Side


class ExchangeAdapter(ABC):
    name: str

    @abstractmethod
    async def get_candles(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int = 500
    ) -> list[Candle]:
        """Closed and in-progress candles, oldest first, ``ts`` in UTC ms."""

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook: ...

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: Side,
        amount: float,
        type: OrderType = OrderType.MARKET,
        price: float | None = None,
        reason: str = "",
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def balance(self) -> Balance: ...

    async def close(self) -> None:
        """Release network resources. Safe to call more than once."""
