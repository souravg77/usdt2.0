"""Binance and HTX adapters built on ccxt. Orders go through the live gate unless on a testnet."""

from __future__ import annotations

import logging

import ccxt.async_support as ccxt_async

from ..config import ExchangeKeys, Mode, Settings
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
    supports_testnet: bool = False

    def __init__(
        self,
        settings: Settings,
        keys: ExchangeKeys | None = None,
        testnet: bool = False,
        client=None,
        throttle: Throttle | None = None,
    ):
        if testnet and not self.supports_testnet:
            raise ValueError(f"{self.name} has no usable testnet; use the paper simulator")
        self.settings = settings
        self.testnet = testnet
        self.throttle = throttle or Throttle(DEFAULT_RATES.get(self.name, 5.0))
        if client is None:
            keys = keys or ExchangeKeys()
            client = getattr(ccxt_async, self.ccxt_id)(
                {
                    "apiKey": keys.api_key or None,
                    "secret": keys.secret or None,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                }
            )
            if testnet:
                client.set_sandbox_mode(True)
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
        if self.testnet:
            if self.settings.mode is not Mode.TESTNET:
                raise RuntimeError("testnet adapter used outside AITRADER_MODE=testnet")
            return
        assert_live_allowed(self.settings)

    async def place_order(self, symbol, side, amount, type=OrderType.MARKET, price=None, reason=""):
        self._check_order_allowed()
        await self.throttle.acquire()
        log.info("ORDER %s %s %s %s @ %s reason=%s", self.name, side.value, amount, symbol, price, reason)
        raw = await self.client.create_order(symbol, type.value, side.value, amount, price)
        order = self._to_order(raw, symbol)
        order.reason = reason
        return order

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


class BinanceAdapter(CcxtAdapter):
    name = "binance"
    ccxt_id = "binance"
    supports_testnet = True


class HtxAdapter(CcxtAdapter):
    name = "htx"
    ccxt_id = "htx"
    supports_testnet = False  # HTX has no reliable spot testnet; it stays on PaperExchange


ADAPTERS = {"binance": BinanceAdapter, "htx": HtxAdapter}


def make_adapter(name: str, settings: Settings, testnet: bool = False) -> CcxtAdapter:
    cls = ADAPTERS[name]
    key_name = f"{name}_testnet" if testnet else name
    return cls(settings, keys=settings.keys.get(key_name), testnet=testnet)
