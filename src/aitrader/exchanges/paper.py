"""PaperExchange: simulated fills on real prices, with fees and slippage. No money at risk."""

from __future__ import annotations

import itertools
import time

from ..config import Settings
from ..models import Balance, Order, OrderStatus, OrderType, Position, Side
from .base import ExchangeAdapter


def _split(symbol: str) -> tuple[str, str]:
    base, quote = symbol.split("/")
    return base, quote.split(":")[0]


class PaperExchange(ExchangeAdapter):
    """Spot-only simulator. Market data comes from ``market`` (a real adapter, read-only).

    Market orders fill immediately at the top of book (or last close) moved against you by the
    slippage setting and pay the taker fee. Limit orders rest until ``on_price`` crosses them and
    pay the maker fee. Selling more than you hold is rejected (no shorting, no leverage).
    """

    def __init__(self, market: ExchangeAdapter, settings: Settings, name: str | None = None):
        self.market = market
        self.name = name or f"paper:{market.name}"
        self.taker_fee = settings.paper_taker_fee
        self.maker_fee = settings.paper_maker_fee
        self.slippage = settings.paper_slippage_bps / 10_000
        self.free: dict[str, float] = {"USDT": settings.paper_start_usdt}
        self.used: dict[str, float] = {}
        self.cost_basis: dict[str, float] = {}  # symbol -> average entry price
        self.last_price: dict[str, float] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Order] = []
        self._ids = itertools.count(1)

    # ---- market data is delegated --------------------------------------------------------------

    async def get_candles(self, symbol, timeframe, since=None, limit=500):
        candles = await self.market.get_candles(symbol, timeframe, since, limit)
        if candles:
            self.last_price[symbol] = candles[-1].close
        return candles

    async def get_orderbook(self, symbol, depth=20):
        return await self.market.get_orderbook(symbol, depth)

    async def _reference_price(self, symbol: str, side: Side) -> float:
        try:
            ob = await self.market.get_orderbook(symbol, 5)
            px = ob.best_ask if side is Side.BUY else ob.best_bid
            if px:
                return px
        except Exception:  # fall back to last known price
            pass
        if symbol in self.last_price:
            return self.last_price[symbol]
        candles = await self.get_candles(symbol, "1m", limit=1)
        if not candles:
            raise RuntimeError(f"no price available for {symbol}")
        return candles[-1].close

    # ---- orders --------------------------------------------------------------------------------

    def _new_order(self, symbol, side, type, amount, price, reason) -> Order:
        return Order(
            id=f"paper-{next(self._ids)}",
            exchange=self.name,
            symbol=symbol,
            side=side,
            type=type,
            amount=amount,
            price=price,
            status=OrderStatus.OPEN,
            ts=int(time.time() * 1000),
            reason=reason,
        )

    async def place_order(self, symbol, side, amount, type=OrderType.MARKET, price=None, reason=""):
        if amount <= 0:
            raise ValueError("amount must be positive")
        base, quote = _split(symbol)
        order = self._new_order(symbol, side, type, amount, price, reason)

        if type is OrderType.MARKET:
            ref = await self._reference_price(symbol, side)
            fill_px = ref * (1 + self.slippage) if side is Side.BUY else ref * (1 - self.slippage)
            if not self._can_afford(base, quote, side, amount, fill_px, self.taker_fee):
                order.status = OrderStatus.REJECTED
                order.reason = (reason + " | insufficient balance").strip(" |")
                self.orders[order.id] = order
                return order
            self._fill(order, fill_px, self.taker_fee)
            return order

        if price is None or price <= 0:
            raise ValueError("limit orders need a positive price")
        if not self._can_afford(base, quote, side, amount, price, self.maker_fee):
            order.status = OrderStatus.REJECTED
            self.orders[order.id] = order
            return order
        # reserve funds
        if side is Side.BUY:
            self._move(quote, price * amount * (1 + self.maker_fee), to_used=True)
        else:
            self._move(base, amount, to_used=True)
        self.orders[order.id] = order
        return order

    def _can_afford(self, base, quote, side, amount, px, fee) -> bool:
        if side is Side.BUY:
            return self.free.get(quote, 0.0) + 1e-12 >= px * amount * (1 + fee)
        return self.free.get(base, 0.0) + 1e-12 >= amount

    def _move(self, asset: str, qty: float, to_used: bool) -> None:
        src, dst = (self.free, self.used) if to_used else (self.used, self.free)
        src[asset] = src.get(asset, 0.0) - qty
        dst[asset] = dst.get(asset, 0.0) + qty

    def _fill(self, order: Order, px: float, fee_rate: float, reserved: bool = False) -> None:
        base, quote = _split(order.symbol)
        wallet = self.used if reserved else self.free
        notional = px * order.amount
        if order.side is Side.BUY:
            fee = notional * fee_rate
            if reserved:
                reserved_amt = order.price * order.amount * (1 + fee_rate)
                self.used[quote] -= reserved_amt
                self.free[quote] = self.free.get(quote, 0.0) + reserved_amt - notional - fee
            else:
                wallet[quote] = wallet.get(quote, 0.0) - notional - fee
            held = self.free.get(base, 0.0) + self.used.get(base, 0.0)
            prev = self.cost_basis.get(order.symbol, 0.0)
            self.cost_basis[order.symbol] = (prev * held + notional) / (held + order.amount)
            self.free[base] = self.free.get(base, 0.0) + order.amount
        else:
            fee = notional * fee_rate
            wallet[base] = wallet.get(base, 0.0) - order.amount
            self.free[quote] = self.free.get(quote, 0.0) + notional - fee
            if self.free.get(base, 0.0) + self.used.get(base, 0.0) <= 1e-12:
                self.cost_basis.pop(order.symbol, None)
        order.status = OrderStatus.FILLED
        order.filled = order.amount
        order.average = px
        order.fee = fee
        order.fee_currency = quote
        self.last_price[order.symbol] = px
        self.orders[order.id] = order
        self.fills.append(order)

    def on_price(self, symbol: str, price: float) -> list[Order]:
        """Feed a live trade/ticker price; fills any resting limit orders it crosses."""
        self.last_price[symbol] = price
        filled = []
        for order in list(self.orders.values()):
            if order.symbol != symbol or order.status is not OrderStatus.OPEN:
                continue
            crossed = price <= order.price if order.side is Side.BUY else price >= order.price
            if crossed:
                self._fill(order, order.price, self.maker_fee, reserved=True)
                filled.append(order)
        return filled

    async def cancel_order(self, order_id, symbol):
        order = self.orders[order_id]
        if order.status is OrderStatus.OPEN:
            base, quote = _split(order.symbol)
            if order.side is Side.BUY:
                self._move(quote, order.price * order.amount * (1 + self.maker_fee), to_used=False)
            else:
                self._move(base, order.amount, to_used=False)
            order.status = OrderStatus.CANCELED
        return order

    async def positions(self):
        out = []
        for symbol, entry in self.cost_basis.items():
            base, _ = _split(symbol)
            amt = self.free.get(base, 0.0) + self.used.get(base, 0.0)
            if amt <= 1e-12:
                continue
            last = self.last_price.get(symbol, entry)
            out.append(Position(self.name, symbol, amt, entry, (last - entry) * amt))
        return out

    async def balance(self):
        clean = lambda d: {k: v for k, v in d.items() if abs(v) > 1e-12}  # noqa: E731
        return Balance(self.name, clean(self.free), clean(self.used))

    def equity(self, quote: str = "USDT") -> float:
        total = self.free.get(quote, 0.0) + self.used.get(quote, 0.0)
        for symbol in self.cost_basis:
            base, _ = _split(symbol)
            amt = self.free.get(base, 0.0) + self.used.get(base, 0.0)
            total += amt * self.last_price.get(symbol, self.cost_basis[symbol])
        return total
