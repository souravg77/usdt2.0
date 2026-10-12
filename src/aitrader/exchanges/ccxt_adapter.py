"""The Bybit spot adapter, built on ccxt. Every order goes through the live lock."""

from __future__ import annotations

import asyncio
import logging

import ccxt.async_support as ccxt_async

from ..config import ExchangeKeys, Settings
from ..models import (
    Balance,
    Candle,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from ..safety import assert_live_allowed
from ..throttle import DEFAULT_RATES, Throttle
from .base import ExchangeAdapter

log = logging.getLogger(__name__)

_STATUS = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


class CcxtAdapter(ExchangeAdapter):
    """Generic ccxt-backed adapter. ``client`` may be injected (tests use a fake)."""

    ccxt_id: str = ""
    client_id_param: str = "clientOrderId"  # ccxt's unified name; exchanges may override
    find_id_param: str = "clientOrderId"  # the name fetch_order reads to look an order up by it
    ccxt_options: dict = {}

    def __init__(
        self,
        settings: Settings,
        keys: ExchangeKeys | None = None,
        client=None,
        throttle: Throttle | None = None,
    ):
        self.settings = settings
        self.throttle = throttle or Throttle(DEFAULT_RATES.get(self.name, 5.0))
        if client is None:
            keys = keys or ExchangeKeys()
            client = getattr(ccxt_async, self.ccxt_id)(
                {
                    "apiKey": keys.api_key or None,
                    "secret": keys.secret or None,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot", **self.ccxt_options},
                }
            )
        self.client = client

    # ---- market data -------------------------------------------------------------------------

    async def get_candles(self, symbol, timeframe, since=None, limit=500):
        await self.throttle.acquire()
        rows = await self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        return [
            Candle(
                exchange=self.name,
                symbol=symbol,
                timeframe=timeframe,
                ts=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5] or 0.0),
            )
            for r in sorted(rows, key=lambda r: r[0])
        ]

    async def get_orderbook(self, symbol, depth=20):
        await self.throttle.acquire()
        ob = await self.client.fetch_order_book(symbol, limit=depth)
        return OrderBook(
            exchange=self.name,
            symbol=symbol,
            ts=int(ob.get("timestamp") or 0),
            bids=[(float(p), float(a)) for p, a, *_ in ob.get("bids", [])],
            asks=[(float(p), float(a)) for p, a, *_ in ob.get("asks", [])],
        )

    # ---- trading -----------------------------------------------------------------------------

    def _check_order_allowed(self) -> None:
        assert_live_allowed(self.settings)

    async def place_order(self, symbol, side, amount, type=OrderType.MARKET, price=None, reason="",
                          client_id=None):
        self._check_order_allowed()
        await self.throttle.acquire()
        amount = await self._precise(symbol, amount)
        log.info("ORDER %s %s %s %s @ %s id=%s reason=%s", self.name, side.value, amount, symbol,
                 price, client_id, reason)
        if client_id:
            raw = await self.client.create_order(symbol, type.value, side.value, amount, price,
                                                 {self.client_id_param: client_id})
        else:
            raw = await self.client.create_order(symbol, type.value, side.value, amount, price)
        order = self._to_order(raw, symbol)
        order.reason = reason
        return order

    async def find_order(self, client_id, symbol):
        await self.throttle.acquire()
        try:
            raw = await self.client.fetch_order(None, symbol, {self.find_id_param: client_id})
        except Exception as exc:
            # Only "no such order" means it never arrived. Anything else (a bad request, a network
            # error) must not be read as "not placed": that would buy or sell twice.
            if type(exc).__name__ == "OrderNotFound":
                return None
            raise
        return self._to_order(raw, symbol) if raw else None

    async def _precise(self, symbol: str, amount: float) -> float:
        """Round the amount to the exchange's step size (needs market info, loaded once)."""
        if not hasattr(self.client, "amount_to_precision"):
            return amount
        if not getattr(self.client, "markets", None):
            await self.client.load_markets()
        return float(self.client.amount_to_precision(symbol, amount))

    async def sellable(self, symbol: str, amount: float, price: float) -> float:
        """The amount the exchange would accept in a sell order: rounded to its step size, or 0
        when that is below its minimum amount or minimum order value (dust)."""
        amount = await self._precise(symbol, amount)
        market = (getattr(self.client, "markets", None) or {}).get(symbol) or {}
        limits = market.get("limits") or {}
        min_amount = (limits.get("amount") or {}).get("min") or 0.0
        min_cost = (limits.get("cost") or {}).get("min") or 0.0
        if amount <= 0 or amount < min_amount or amount * price < min_cost:
            return 0.0
        return amount

    async def cancel_order(self, order_id, symbol):
        self._check_order_allowed()
        await self.throttle.acquire()
        raw = await self.client.cancel_order(order_id, symbol)
        return self._to_order(raw, symbol)

    async def balance(self):
        await self.throttle.acquire()
        raw = await self.client.fetch_balance()
        free = {k: float(v) for k, v in (raw.get("free") or {}).items() if v}
        used = {k: float(v) for k, v in (raw.get("used") or {}).items() if v}
        return Balance(exchange=self.name, free=free, used=used)

    async def positions(self):
        # Spot has no positions endpoint: report non-quote holdings. Entry price is unknown here;
        # the trade journal (later phases) owns cost basis.
        bal = await self.balance()
        out = []
        for asset in set(bal.free) | set(bal.used):
            if asset in {"USDT", "USDC", "FDUSD", "BUSD"}:
                continue
            amt = bal.total(asset)
            if amt > 0:
                out.append(Position(self.name, f"{asset}/USDT", amt, 0.0))
        return out

    async def close(self):
        await self.client.close()

    def _to_order(self, raw: dict, symbol: str) -> Order:
        fee = raw.get("fee") or {}
        return Order(
            id=str(raw.get("id")),
            exchange=self.name,
            symbol=raw.get("symbol") or symbol,
            side=Side(raw.get("side") or "buy"),
            type=OrderType(raw.get("type") or "market"),
            amount=float(raw.get("amount") or 0.0),
            price=raw.get("price"),
            status=_STATUS.get(raw.get("status") or "open", OrderStatus.OPEN),
            filled=float(raw.get("filled") or 0.0),
            average=raw.get("average"),
            fee=float(fee.get("cost") or 0.0),
            fee_currency=fee.get("currency"),
            ts=int(raw.get("timestamp") or 0),
        )


class BybitAdapter(CcxtAdapter):
    """Bybit spot (V5 API) on the real exchange. Market data needs no key; orders need the
    real-account key and the live lock."""

    name = "bybit"
    ccxt_id = "bybit"
    client_id_param = "orderLinkId"  # up to 36 letters, digits, - and _; ours are 32
    # Bybit accounts are Unified Trading Accounts; saying so skips ccxt's account-type lookup,
    # which needs key permissions a trade-only key doesn't have
    ccxt_options = {"enableUnifiedAccount": True, "enableUnifiedMargin": False}

    async def place_order(self, symbol, side, amount, type=OrderType.MARKET, price=None, reason="",
                          client_id=None):
        """Spot only, never borrowed (isLeverage 0). A market order's amount is in coins
        (marketUnit baseCoin), not USDT. Bybit answers a new order with its id only, so the order
        is read back once to learn the fill."""
        self._check_order_allowed()
        await self.throttle.acquire()
        amount = await self._precise(symbol, amount)
        log.info("ORDER %s %s %s %s @ %s id=%s reason=%s", self.name, side.value, amount, symbol,
                 price, client_id, reason)
        params = {"isLeverage": 0}
        if type is OrderType.MARKET:
            params["marketUnit"] = "baseCoin"
        if client_id:
            params[self.client_id_param] = client_id
        raw = await self.client.create_order(symbol, type.value, side.value, amount,
                                             price if type is OrderType.LIMIT else None, params)
        order = self._to_order(raw, symbol)
        if client_id and order.filled <= 0 and order.status is OrderStatus.OPEN:
            for wait in (0.3, 0.7, 1.5):
                await asyncio.sleep(wait)
                found = await self.find_order(client_id, symbol)
                if found is not None and (found.filled > 0 or found.status is not OrderStatus.OPEN):
                    order = found
                    break
        order.reason = reason
        return order

    async def place_stop(self, symbol, amount, trigger_price, client_id, reason=""):
        """A resting stop-loss on Bybit: a spot conditional order (orderFilter StopOrder) that sells
        ``amount`` at market once the last price falls to ``trigger_price``. It waits on the
        exchange, so the position is protected even while this server is down."""
        self._check_order_allowed()
        await self.throttle.acquire()
        amount = await self._precise(symbol, amount)
        if hasattr(self.client, "price_to_precision"):
            trigger_price = float(self.client.price_to_precision(symbol, trigger_price))
        log.info("STOP %s sell %s %s at %s id=%s reason=%s", self.name, amount, symbol, trigger_price,
                 client_id, reason)
        params = {"isLeverage": 0, "marketUnit": "baseCoin", "triggerPrice": trigger_price,
                  self.client_id_param: client_id}
        raw = await self.client.create_order(symbol, "market", "sell", amount, None, params)
        order = self._to_order(raw, symbol)
        order.reason = reason
        return order

    async def cancel_stop(self, client_id, symbol) -> bool:
        """Cancel the resting stop sent with ``client_id``. False when Bybit no longer has it open
        (it fired, or was cancelled); any other error is raised."""
        self._check_order_allowed()
        await self.throttle.acquire()
        try:
            await self.client.cancel_order(None, symbol, {"trigger": True, "orderLinkId": client_id})
        except Exception as exc:
            if type(exc).__name__ == "OrderNotFound":
                return False
            raise
        return True

    def _to_order(self, raw: dict, symbol: str) -> Order:
        """ccxt reads the fee from ``cumFeeDetail``; older answers only have ``cumExecFee`` and
        ``feeCurrency`` (a buy pays its fee in the coin bought, a sell in USDT)."""
        if not (raw.get("fee") or {}).get("currency"):
            info = raw.get("info") or {}
            cost = float(info.get("cumExecFee") or 0)
            if cost:
                cur = info.get("feeCurrency") or (symbol.split("/")[0] if raw.get("side") == "buy"
                                                  else symbol.split("/")[1])
                raw = {**raw, "fee": {"cost": cost, "currency": cur}}
        return super()._to_order(raw, symbol)

    async def find_order(self, client_id, symbol):
        """The order sent with ``client_id`` (open ones first, then the order history), or None
        when Bybit has no such order. Any error is raised: it never means "not placed"."""
        if not getattr(self.client, "markets", None):
            await self.client.load_markets()
        market = self.client.market(symbol)
        req = {"category": "spot", "symbol": market["id"], "orderLinkId": client_id}
        for fetch in (self.client.private_get_v5_order_realtime, self.client.private_get_v5_order_history):
            await self.throttle.acquire()
            rows = ((await fetch(dict(req))).get("result") or {}).get("list") or []
            if rows:
                return self._to_order(self.client.parse_order(rows[0], market), symbol)
        return None


ADAPTERS = {"bybit": BybitAdapter}


def make_adapter(name: str, settings: Settings) -> CcxtAdapter:
    return ADAPTERS[name](settings, keys=settings.keys.get(name))
