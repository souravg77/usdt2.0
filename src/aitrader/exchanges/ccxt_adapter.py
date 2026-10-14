"""The Bybit spot adapter, built on ccxt. Every order goes through the live lock."""

from __future__ import annotations

import asyncio
import logging
import time

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
    TIMEFRAME_MS,
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
        if since is None or timeframe not in TIMEFRAME_MS:
            rows = await self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        else:
            # Bybit answers a request with only a start with its newest bars, so an old month came
            # back empty: give each page its end too (start + limit bars)
            until = since + limit * TIMEFRAME_MS[timeframe] - 1
            rows = await self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit,
                                                 params={"until": until})
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


class PerpNotReady(RuntimeError):
    """The account isn't set up for the agent's futures trades (the message says what to change)."""


class BybitAdapter(CcxtAdapter):
    """Bybit spot and USDT perpetuals (V5 API) on the real exchange. Market data needs no key;
    orders need the real-account key and the live lock.

    A symbol with a settle currency (``BTC/USDT:USDT``) is the USDT perpetual; the agent only
    sells those short (``trade.futures``), one-way mode, closing with reduce-only orders. The
    account must be in Isolated Margin mode: the agent checks it and never changes it itself,
    because on a Unified Trading Account that setting covers the whole account.
    """

    name = "bybit"
    ccxt_id = "bybit"
    client_id_param = "orderLinkId"  # up to 36 letters, digits, - and _; ours are 32
    # Bybit accounts are Unified Trading Accounts; saying so skips ccxt's account-type lookup,
    # which needs key permissions a trade-only key doesn't have
    ccxt_options = {"enableUnifiedAccount": True, "enableUnifiedMargin": False}

    supports_perps = True

    @staticmethod
    def is_perp(symbol: str) -> bool:
        return ":" in symbol

    async def place_order(self, symbol, side, amount, type=OrderType.MARKET, price=None, reason="",
                          client_id=None, reduce_only=False):
        """Spot: never borrowed (isLeverage 0), a market order's amount in coins (marketUnit
        baseCoin). Perpetual: one-way mode (positionIdx 0), ``reduce_only`` for closing orders.
        Bybit answers a new order with its id only, so the order is read back once to learn the
        fill."""
        self._check_order_allowed()
        await self.throttle.acquire()
        amount = await self._precise(symbol, amount)
        log.info("ORDER %s %s %s %s @ %s id=%s reason=%s", self.name, side.value, amount, symbol,
                 price, client_id, reason)
        if self.is_perp(symbol):
            params = {"positionIdx": 0}
            if reduce_only:
                params["reduceOnly"] = True
        else:
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

    async def place_stop(self, symbol, amount, trigger_price, client_id, reason="", side=Side.SELL):
        """A resting stop-loss on Bybit that waits on the exchange, so the position is protected
        even while this server is down. Spot: a conditional order (orderFilter StopOrder) selling
        ``amount`` at market once the last price falls to ``trigger_price``. Perpetual short
        (``side`` BUY): a reduce-only conditional buy once the mark price rises to it."""
        self._check_order_allowed()
        await self.throttle.acquire()
        amount = await self._precise(symbol, amount)
        if hasattr(self.client, "price_to_precision"):
            trigger_price = float(self.client.price_to_precision(symbol, trigger_price))
        log.info("STOP %s %s %s %s at %s id=%s reason=%s", self.name, side.value, amount, symbol, trigger_price,
                 client_id, reason)
        if self.is_perp(symbol):
            # triggered by the mark price (Bybit's fair price, which follows the spot index), so a
            # wick on the perpetual's own book can't fire the stop; liquidation uses it too
            params = {"positionIdx": 0, "reduceOnly": True, "triggerPrice": trigger_price,
                      "triggerDirection": "ascending" if side is Side.BUY else "descending",
                      "triggerBy": "MarkPrice", self.client_id_param: client_id}
        else:
            if side is not Side.SELL:
                raise ValueError("a spot stop can only sell")
            params = {"isLeverage": 0, "marketUnit": "baseCoin", "triggerPrice": trigger_price,
                      self.client_id_param: client_id}
        raw = await self.client.create_order(symbol, "market", side.value, amount, None, params)
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
        req = {"category": "linear" if self.is_perp(symbol) else "spot", "symbol": market["id"],
               "orderLinkId": client_id}
        for fetch in (self.client.private_get_v5_order_realtime, self.client.private_get_v5_order_history):
            await self.throttle.acquire()
            rows = ((await fetch(dict(req))).get("result") or {}).get("list") or []
            if rows:
                return self._to_order(self.client.parse_order(rows[0], market), symbol)
        return None


    # ---- perpetuals --------------------------------------------------------------------------

    async def prepare_perp(self, symbol: str, leverage: float) -> None:
        """Before the first order on a perpetual: the account must be in Isolated Margin mode
        (checked, never changed here), and the symbol's leverage is set (once per process)."""
        self._check_order_allowed()
        if not hasattr(self, "_perp_ready"):
            self._perp_ready: dict[str, float] = {}
        ready = self._perp_ready
        if ready.get(symbol) == leverage:
            return
        await self.throttle.acquire()
        info = (await self.client.private_get_v5_account_info()).get("result") or {}
        if info.get("marginMode") != "ISOLATED_MARGIN":
            raise PerpNotReady("switch the Bybit account to Isolated Margin (Bybit app: Derivatives, "
                               f"margin mode) before futures trades; it is {info.get('marginMode') or 'unknown'}")
        await self.throttle.acquire()
        rows = await self.client.fetch_positions([symbol])
        idx = {str((r.get("info") or {}).get("positionIdx", "0")) for r in rows if r.get("symbol") == symbol}
        if idx - {"0"}:
            # orders here are sent with positionIdx 0, which Bybit only accepts in one-way mode
            raise PerpNotReady(f"switch {symbol} to One-Way position mode (Bybit app: Derivatives, "
                               "position mode) before futures trades; it is in Hedge mode")
        await self.throttle.acquire()
        try:
            await self.client.set_leverage(int(leverage) if leverage == int(leverage) else leverage, symbol)
        except Exception as exc:
            if "110043" not in str(exc) and "not modified" not in str(exc).lower():
                raise
        ready[symbol] = leverage

    async def perp_position_info(self, symbol: str) -> dict:
        """The open short on ``symbol`` as Bybit reports it: size (coins), entry, mark price,
        Bybit's own liquidation price, leverage, margin mode and position index. Empty when there
        is none."""
        await self.throttle.acquire()
        for r in await self.client.fetch_positions([symbol]):
            if r.get("symbol") != symbol or r.get("side") != "short" or not float(r.get("contracts") or 0):
                continue
            info = r.get("info") or {}

            def num(*vals):
                for v in vals:
                    try:
                        if v not in (None, ""):
                            return float(v)
                    except (TypeError, ValueError):
                        pass
                return None

            return {"size": float(r["contracts"]), "entry": num(r.get("entryPrice"), info.get("avgPrice")),
                    "mark": num(r.get("markPrice"), info.get("markPrice")),
                    "liq_price": num(r.get("liquidationPrice"), info.get("liqPrice")),
                    "leverage": num(r.get("leverage"), info.get("leverage")),
                    "margin_mode": r.get("marginMode"), "position_idx": int(num(info.get("positionIdx")) or 0)}
        return {}

    async def funding_paid(self, symbol: str, since_ms: int) -> float:
        """USDT the funding settlements on ``symbol`` took from the account since ``since_ms``
        (negative when they paid us). Read from the transaction log's SETTLEMENT rows: ``funding``
        is the fee (positive = paid out); older rows without it fall back to ``-change``. Bybit
        only answers for a 7-day window, so a short held longer counts its last 7 days."""
        if not getattr(self.client, "markets", None):
            await self.client.load_markets()
        await self.throttle.acquire()
        res = await self.client.private_get_v5_account_transaction_log(
            {"accountType": "UNIFIED", "category": "linear", "type": "SETTLEMENT",
             "symbol": self.client.market(symbol)["id"],
             "startTime": max(int(since_ms), int(time.time() * 1000) - 7 * 86_400_000 + 60_000)})
        rows = (res.get("result") or {}).get("list") or []
        total = 0.0
        for r in rows:
            if r.get("funding") not in (None, ""):
                total += float(r["funding"])
            else:
                total -= float(r.get("change") or 0)
        return total

    async def perp_position(self, symbol: str) -> float:
        """Size of the open short on ``symbol`` in coins (0 when there is none)."""
        await self.throttle.acquire()
        rows = await self.client.fetch_positions([symbol])
        return sum(float(r.get("contracts") or 0) for r in rows
                   if r.get("symbol") == symbol and r.get("side") == "short")


ADAPTERS = {"bybit": BybitAdapter}


def make_adapter(name: str, settings: Settings) -> CcxtAdapter:
    return ADAPTERS[name](settings, keys=settings.keys.get(name))
