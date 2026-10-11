import pytest

from aitrader.config import LIVE_CONFIRM_PHRASE, Mode
from aitrader.exchanges import BinanceAdapter, HtxAdapter
from aitrader.models import OrderType, Side
from aitrader.safety import LiveTradingLocked

# Binance and HTX both come back through ccxt as [ts, o, h, l, c, v]; HTX may return newest first.
BINANCE_RAW = [[0, 1, 2, 0.5, 1.5, 10], [60_000, 1.5, 2, 1, 1.8, 12]]
HTX_RAW = [[60_000, 1.5, 2, 1, 1.8, 12], [0, 1, 2, 0.5, 1.5, None]]


async def test_both_exchanges_return_same_candle_format(settings, fake_ccxt):
    b = BinanceAdapter(settings, client=fake_ccxt(BINANCE_RAW))
    h = HtxAdapter(settings, client=fake_ccxt(HTX_RAW))
    bc = await b.get_candles("BTC/USDT", "1m")
    hc = await h.get_candles("BTC/USDT", "1m")
    assert [c.ts for c in bc] == [c.ts for c in hc] == [0, 60_000]
    assert [(c.open, c.close) for c in bc] == [(c.open, c.close) for c in hc]
    assert hc[0].volume == 0.0 and isinstance(hc[0].volume, float)
    assert {c.exchange for c in bc} == {"binance"} and {c.exchange for c in hc} == {"htx"}


async def test_orderbook_normalised(settings, fake_ccxt):
    ob = await BinanceAdapter(settings, client=fake_ccxt()).get_orderbook("BTC/USDT")
    assert ob.best_bid == 100.0 and ob.best_ask == 101.0


async def test_live_orders_locked_by_default(settings, fake_ccxt):
    client = fake_ccxt()
    a = BinanceAdapter(settings, client=client)
    with pytest.raises(LiveTradingLocked):
        await a.place_order("BTC/USDT", Side.BUY, 0.01)
    with pytest.raises(LiveTradingLocked):
        await a.cancel_order("1", "BTC/USDT")
    assert client.orders == []


async def test_live_mode_alone_does_not_unlock(settings, fake_ccxt):
    settings.mode = Mode.LIVE
    settings.live_unlocked = True  # but no confirmation phrase
    a = BinanceAdapter(settings, client=fake_ccxt())
    with pytest.raises(LiveTradingLocked):
        await a.place_order("BTC/USDT", Side.BUY, 0.01)


async def test_live_unlocked_only_with_all_conditions(settings, fake_ccxt):
    settings.mode = Mode.LIVE
    settings.live_unlocked = True
    settings.live_confirm = LIVE_CONFIRM_PHRASE
    client = fake_ccxt()
    order = await BinanceAdapter(settings, client=client).place_order(
        "BTC/USDT", Side.BUY, 0.01, OrderType.MARKET, reason="test"
    )
    assert order.filled == 0.01 and order.reason == "test" and len(client.orders) == 1


async def test_testnet_orders_need_testnet_mode(settings, fake_ccxt):
    client = fake_ccxt()
    a = BinanceAdapter(settings, client=client, testnet=True)
    with pytest.raises(RuntimeError):
        await a.place_order("BTC/USDT", Side.BUY, 0.01)
    settings.mode = Mode.TESTNET
    await a.place_order("BTC/USDT", Side.BUY, 0.01)
    assert len(client.orders) == 1


def test_htx_has_no_testnet(settings, fake_ccxt):
    with pytest.raises(ValueError):
        HtxAdapter(settings, client=fake_ccxt(), testnet=True)


async def test_balance_and_positions(settings, fake_ccxt):
    a = BinanceAdapter(settings, client=fake_ccxt())
    bal = await a.balance()
    assert bal.total("USDT") == 50.0
    pos = await a.positions()
    assert [(p.symbol, p.amount) for p in pos] == [("BTC/USDT", 0.1)]
