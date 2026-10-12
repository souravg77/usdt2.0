import pytest

from aitrader.config import LIVE_CONFIRM_PHRASE, Mode
from aitrader.exchanges import BybitAdapter, make_adapter
from aitrader.models import OrderType, Side
from aitrader.safety import LiveTradingLocked

# ccxt returns candles as [ts, o, h, l, c, v]; a missing volume becomes 0.0.
BYBIT_RAW = [[60_000, 1.5, 2, 1, 1.8, None], [0, 1, 2, 0.5, 1.5, 10]]


async def test_candles_normalised_and_sorted(settings, fake_ccxt):
    bc = await BybitAdapter(settings, client=fake_ccxt(BYBIT_RAW)).get_candles("BTC/USDT", "1m")
    assert [c.ts for c in bc] == [0, 60_000] and bc[1].volume == 0.0
    assert {c.exchange for c in bc} == {"bybit"}


async def test_orderbook_normalised(settings, fake_ccxt):
    ob = await BybitAdapter(settings, client=fake_ccxt()).get_orderbook("BTC/USDT")
    assert ob.best_bid == 100.0 and ob.best_ask == 101.0


async def test_live_orders_locked_by_default(settings, fake_ccxt):
    client = fake_ccxt()
    a = BybitAdapter(settings, client=client)
    with pytest.raises(LiveTradingLocked):
        await a.place_order("BTC/USDT", Side.BUY, 0.01)
    with pytest.raises(LiveTradingLocked):
        await a.cancel_order("1", "BTC/USDT")
    assert client.orders == []


async def test_live_mode_alone_does_not_unlock(settings, fake_ccxt):
    settings.mode = Mode.LIVE
    settings.live_unlocked = True  # but no confirmation phrase
    a = BybitAdapter(settings, client=fake_ccxt())
    with pytest.raises(LiveTradingLocked):
        await a.place_order("BTC/USDT", Side.BUY, 0.01)


async def test_live_unlocked_only_with_all_conditions(settings, fake_ccxt):
    settings.mode = Mode.LIVE
    settings.live_unlocked = True
    settings.live_confirm = LIVE_CONFIRM_PHRASE
    client = fake_ccxt()
    order = await BybitAdapter(settings, client=client).place_order(
        "BTC/USDT", Side.BUY, 0.01, OrderType.MARKET, reason="test"
    )
    assert order.filled == 0.01 and order.reason == "test" and len(client.orders) == 1


def test_adapters_use_the_real_exchange_and_the_real_key(settings):
    import dataclasses

    from aitrader.config import ExchangeKeys

    settings = dataclasses.replace(settings, keys={"bybit": ExchangeKeys("r" * 18, "s" * 18)})
    real = make_adapter("bybit", settings)
    assert "api-demo." not in real.client.urls["api"]["private"] and real.client.apiKey == "r" * 18
    assert real.client.options["enableUnifiedAccount"] is True
    with pytest.raises(TypeError):
        BybitAdapter(settings, testnet=True)  # demo trading is gone


def _unlocked(settings):
    settings.mode = Mode.LIVE
    settings.live_unlocked = True
    settings.live_confirm = LIVE_CONFIRM_PHRASE
    return settings


async def test_bybit_orders_are_spot_unleveraged_in_coins(settings, fake_ccxt):
    client = fake_ccxt()
    await BybitAdapter(_unlocked(settings), client=client).place_order(
        "BTC/USDT", Side.BUY, 0.01, client_id="ait" + "0" * 29)
    assert client.params == {"isLeverage": 0, "marketUnit": "baseCoin", "orderLinkId": "ait" + "0" * 29}


async def test_bybit_resting_stop_is_a_spot_conditional_sell(settings, fake_ccxt):
    client = fake_ccxt()
    a = BybitAdapter(_unlocked(settings), client=client)
    await a.place_stop("BTC/USDT", 0.01, 95.0, "aitstop")
    assert client.orders[-1][:4] == ("BTC/USDT", "market", "sell", 0.01)
    assert client.params == {"isLeverage": 0, "marketUnit": "baseCoin", "triggerPrice": 95.0,
                             "orderLinkId": "aitstop"}
    assert await a.cancel_stop("aitstop", "BTC/USDT") is True
    assert client.cancelled == (None, "BTC/USDT", {"trigger": True, "orderLinkId": "aitstop"})

    class OrderNotFound(Exception):
        pass

    async def gone(*args):
        raise OrderNotFound()

    client.cancel_order = gone
    assert await a.cancel_stop("aitstop", "BTC/USDT") is False  # it fired or was cancelled
    settings.live_unlocked = False
    with pytest.raises(LiveTradingLocked):  # the live lock covers stops too
        await BybitAdapter(settings, client=fake_ccxt()).place_stop("BTC/USDT", 0.01, 95.0, "x")


async def test_find_order_reads_open_then_history(settings):
    class Client:
        markets = {"BTC/USDT": {}}

        def __init__(self):
            self.calls = []

        def market(self, symbol):
            return {"id": "BTCUSDT", "symbol": symbol}

        async def private_get_v5_order_realtime(self, req):
            self.calls.append(("open", req))
            return {"result": {"list": []}}

        async def private_get_v5_order_history(self, req):
            self.calls.append(("history", req))
            return {"result": {"list": [{"orderLinkId": req["orderLinkId"]}]}}

        def parse_order(self, raw, market):
            return {"id": "9", "symbol": market["symbol"], "side": "sell", "type": "market", "amount": 1,
                    "status": "closed", "filled": 1, "average": 100.0, "fee": {"cost": 0.1, "currency": "USDT"}}

    c = Client()
    o = await BybitAdapter(settings, client=c).find_order("abc", "BTC/USDT")
    assert o.filled == 1 and [k for k, _ in c.calls] == ["open", "history"]
    assert c.calls[0][1] == {"category": "spot", "symbol": "BTCUSDT", "orderLinkId": "abc"}


async def test_balance_and_positions(settings, fake_ccxt):
    a = BybitAdapter(settings, client=fake_ccxt())
    bal = await a.balance()
    assert bal.total("USDT") == 50.0
    pos = await a.positions()
    assert [(p.symbol, p.amount) for p in pos] == [("BTC/USDT", 0.1)]


def test_bybit_fee_read_from_cum_exec_fee(settings):
    import ccxt

    raw = {"symbol": "BTCUSDT", "orderType": "Market", "orderId": "1", "avgPrice": "60000",
           "orderStatus": "Filled", "qty": "0.001", "side": "Buy", "cumExecQty": "0.001",
           "cumExecValue": "60", "cumExecFee": "0.000001", "createdTime": "1", "category": "spot"}
    market = {"id": "BTCUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "spot": True,
              "swap": False, "option": False, "type": "spot", "contract": False, "precision": {}}
    client = ccxt.bybit()
    o = BybitAdapter(settings, client=client)._to_order(client.parse_order(raw, market), "BTC/USDT")
    assert (o.filled, o.fee, o.fee_currency) == (0.001, 0.000001, "BTC")  # a buy pays in the coin
