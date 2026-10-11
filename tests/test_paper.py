import pytest

from aitrader.exchanges import BinanceAdapter, PaperExchange
from aitrader.models import OrderStatus, OrderType, Side


@pytest.fixture
def paper(settings, fake_ccxt):
    market = BinanceAdapter(settings, client=fake_ccxt())  # bid 100 / ask 101
    return PaperExchange(market, settings)


async def test_open_and_close_position(paper):
    buy = await paper.place_order("BTC/USDT", Side.BUY, 2, reason="test")
    assert buy.status is OrderStatus.FILLED
    assert buy.average == pytest.approx(101 * 1.001)  # ask + 10bps slippage
    assert buy.fee == pytest.approx(101 * 1.001 * 2 * 0.001)
    [pos] = await paper.positions()
    assert pos.amount == 2 and pos.entry_price == pytest.approx(101.101)

    sell = await paper.place_order("BTC/USDT", Side.SELL, 2)
    assert sell.average == pytest.approx(100 * 0.999)
    assert await paper.positions() == []
    expected = 1000 - 2 * 101.101 * 1.001 + 2 * 99.9 * 0.999
    assert paper.free["USDT"] == pytest.approx(expected)
    assert paper.equity() < 1000  # spread, slippage and fees all cost money


async def test_cannot_sell_what_you_do_not_hold(paper):
    order = await paper.place_order("BTC/USDT", Side.SELL, 1)
    assert order.status is OrderStatus.REJECTED


async def test_cannot_overspend(paper):
    order = await paper.place_order("BTC/USDT", Side.BUY, 100)
    assert order.status is OrderStatus.REJECTED
    assert paper.free["USDT"] == 1000


async def test_limit_order_reserves_fills_and_cancels(paper):
    o = await paper.place_order("ETH/USDT", Side.BUY, 1, OrderType.LIMIT, price=90)
    assert o.status is OrderStatus.OPEN
    assert paper.free["USDT"] == pytest.approx(1000 - 90 * 1.0005)
    assert paper.on_price("ETH/USDT", 95) == []
    [filled] = paper.on_price("ETH/USDT", 89.5)
    assert filled.average == 90 and filled.fee == pytest.approx(90 * 0.0005)
    assert paper.free["ETH"] == 1
    assert paper.free["USDT"] == pytest.approx(1000 - 90 - 90 * 0.0005)

    o2 = await paper.place_order("ETH/USDT", Side.SELL, 1, OrderType.LIMIT, price=120)
    assert paper.free["ETH"] == 0
    await paper.cancel_order(o2.id, "ETH/USDT")
    assert o2.status is OrderStatus.CANCELED and paper.free["ETH"] == 1


async def test_averages_entry_price(paper):
    paper.on_price("SOL/USDT", 10)
    await paper.place_order("SOL/USDT", Side.BUY, 1, OrderType.LIMIT, price=10)
    paper.on_price("SOL/USDT", 10)
    await paper.place_order("SOL/USDT", Side.BUY, 1, OrderType.LIMIT, price=20)
    paper.on_price("SOL/USDT", 20)
    [pos] = await paper.positions()
    assert pos.amount == 2 and pos.entry_price == pytest.approx(15)
