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
        client_id: str | None = None,
    ) -> Order:
        """``client_id`` makes the order idempotent: re-sending the same id after a timeout
        can't create a second order, and ``find_order`` can look it up after a restart."""

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> Order: ...

    async def find_order(self, client_id: str, symbol: str) -> Order | None:
        """The order sent with ``client_id``, or None if the exchange never received it."""
        return None

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def balance(self) -> Balance: ...

    async def close(self) -> None:
        """Release network resources. Safe to call more than once."""
