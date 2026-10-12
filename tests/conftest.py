import pytest

from aitrader.config import Mode, Settings


class FakeCcxt:
    """Minimal stand-in for a ccxt async client, returning exchange-shaped raw data."""

    def __init__(self, ohlcv=None, book=None):
        self.ohlcv = ohlcv or []
        self.book = book or {"timestamp": 1, "bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]}
        self.orders = []
        self.closed = False

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        rows = [r for r in self.ohlcv if since is None or r[0] >= since]
        return rows[:limit] if limit else rows

    async def fetch_order_book(self, symbol, limit=None):
        return self.book

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.orders.append((symbol, type, side, amount, price))
        self.params = params or {}
        return {"id": "1", "symbol": symbol, "side": side, "type": type, "amount": amount,
                "price": price, "status": "closed", "filled": amount, "average": 101.0,
                "fee": {"cost": 0.1, "currency": "USDT"}, "timestamp": 5}

    async def cancel_order(self, order_id, symbol, params=None):
        self.cancelled = (order_id, symbol, params or {})
        return {"id": order_id, "symbol": symbol, "side": "buy", "type": "limit",
                "amount": 1, "status": "canceled"}

    async def fetch_balance(self):
        return {"free": {"USDT": 50.0, "BTC": 0.1}, "used": {"USDT": 0.0}}

    async def close(self):
        self.closed = True


@pytest.fixture
def settings():
    return Settings(mode=Mode.PAPER, paper_start_usdt=1000.0, paper_taker_fee=0.001,
                    paper_maker_fee=0.0005, paper_slippage_bps=10)


@pytest.fixture
def fake_ccxt():
    return FakeCcxt
