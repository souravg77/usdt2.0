from aitrader.models import OrderBook
from aitrader.strategies import liquidity_check


def book(bid, ask, size=1.0, levels=5, step=0.0005):
    bids = [(bid * (1 - i * step), size) for i in range(levels)]
    asks = [(ask * (1 + i * step), size) for i in range(levels)]
    return OrderBook("binance", "BTC/USDT", 0, bids, asks)


def test_liquid_book_passes():
    chk = liquidity_check(book(60_000, 60_006), order_quote=100)
    assert chk.ok and chk.spread < 0.001 and chk.reason == "liquid"


def test_wide_spread_fails():
    chk = liquidity_check(book(100, 101, size=1_000), order_quote=10)
    assert not chk.ok and "spread" in chk.reason


def test_thin_book_fails():
    chk = liquidity_check(book(60_000, 60_006, size=0.001), order_quote=100)
    assert not chk.ok and "depth" in chk.reason


def test_empty_book_fails():
    assert not liquidity_check(OrderBook("htx", "X/USDT", 0, [], []), 100).ok
