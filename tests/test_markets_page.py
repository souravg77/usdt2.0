"""The Markets page: live prices and candles from Bybit's public API (cached ~1s), saved
candles as the fallback, and the page that draws them."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.data.store import CandleStore
from aitrader.models import Candle
from aitrader.web import Auth, JobRunner, create_app
from aitrader.web.live import LiveMarket

PW = "correct horse battery"
NOW = 1_760_000_000_000  # ms
HOUR = 3_600_000


class FakeBybit:
    def __init__(self):
        self.calls = []
        self.down = False

    async def fetch_ohlcv(self, symbol, timeframe, limit=200):
        self.calls.append(("ohlcv", symbol, timeframe))
        if self.down:
            raise ConnectionError("no route")
        return [[NOW - (3 - i) * 60_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 7] for i in range(3)]

    async def fetch_tickers(self, symbols):
        self.calls.append(("tickers", tuple(symbols)))
        if self.down:
            raise ConnectionError("no route")
        return {"BTC/USDT": {"last": 62000.5, "percentage": 1.25, "high": 63000, "low": 61000,
                             "quoteVolume": 1.5e9}}

    async def close(self):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "aitrader.db")
    store = CandleStore(db)
    store.migrate()
    store.upsert([Candle("bybit", "BTC/USDT", "1h", NOW - (30 - i) * HOUR - HOUR, 100 + i, 101 + i,
                         99 + i, 100.5 + i, 1) for i in range(30)])
    store.close()
    settings = Settings(db_path=db, symbols=["BTC/USDT", "ETH/USDT"], timeframes=["1m", "1h", "1d"])
    clock = [0.0]
    fake = FakeBybit()
    live = LiveMarket(db, client=fake, clock=lambda: clock[0])
    app = create_app(settings, Auth("admin", PW, "k"), JobRunner(str(tmp_path / "j"), env_file="none"), live=live)
    import aitrader.web.market_pages as mp
    monkeypatch.setattr(mp.time, "time", lambda: NOW / 1000)
    return TestClient(app), fake, clock


def login(client):
    client.post("/login", data={"user": "admin", "password": PW})


def test_market_api_needs_login(world):
    client, fake, _ = world
    assert client.get("/markets", follow_redirects=False).status_code == 303
    assert client.get("/api/market/tickers").status_code == 401
    assert client.get("/api/market/candles?symbol=BTC/USDT").status_code == 401
    assert fake.calls == []


def test_markets_page_loads_the_live_chart(world):
    client, _, _ = world
    login(client)
    r = client.get("/markets?symbol=BTC/USDT&tf=1h")
    assert r.status_code == 200 and '<a href="/markets" class=active aria-current=page>Markets</a>' in r.text
    assert '<script src="/static/market.js" defer></script>' in r.text and '<canvas id="chart"' in r.text
    assert 'data-symbol="BTC/USDT" data-tf="1h"' in r.text and "129.50" in r.text  # saved price first
    assert "<noscript>" in r.text and r.text.count("<rect") == 30  # chart without JavaScript
    assert "http-equiv" not in r.text  # no full-page reloads: the script polls instead
    js = client.get("/static/market.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "POLL_MS = 1500" in js.text
    r = client.get("/markets?symbol=<script>&tf=9y")  # unknown values fall back, never echoed
    assert "<script>" not in r.text.replace('<script src="/static/market.js" defer></script>', "")


def test_market_api_reads_bybit_once_per_second(world):
    client, fake, clock = world
    login(client)
    r = client.get("/api/market/candles?symbol=BTC/USDT&tf=1m").json()
    assert r["source"] == "live" and len(r["candles"]) == 3 and r["candles"][-1][4] == 102.5
    t = client.get("/api/market/tickers").json()
    assert t["tickers"][0] == {"symbol": "BTC/USDT", "price": 62000.5, "change": 1.25, "high": 63000,
                               "low": 61000, "volume": 1.5e9}
    for _ in range(5):  # many viewers within a second: one request to Bybit
        client.get("/api/market/candles?symbol=BTC/USDT&tf=1m")
        client.get("/api/market/tickers")
    assert fake.calls.count(("ohlcv", "BTC/USDT", "1m")) == 1 and len(fake.calls) == 2
    clock[0] += 1.1
    client.get("/api/market/candles?symbol=BTC/USDT&tf=1m")
    assert fake.calls.count(("ohlcv", "BTC/USDT", "1m")) == 2
    client.get("/api/market/candles?symbol=DOGE/USDT&tf=1w")  # unknown pair/timeframe: defaults
    assert fake.calls[-1] == ("ohlcv", "BTC/USDT", "15m")


def test_market_api_falls_back_to_saved_candles(world):
    client, fake, clock = world
    login(client)
    fake.down = True
    r = client.get("/api/market/candles?symbol=BTC/USDT&tf=1h").json()
    assert r["source"] == "database" and "not reachable" in r["note"] and r["candles"][-1][4] == 129.5
    t = client.get("/api/market/tickers").json()
    assert t["source"] == "database" and t["tickers"][0]["price"] == 129.5


def test_live_market_shares_one_request(tmp_path):
    fake = FakeBybit()
    live = LiveMarket(str(tmp_path / "x.db"), client=fake, clock=lambda: 0.0)

    async def many():
        return await asyncio.gather(*[live.candles("BTC/USDT", "1m") for _ in range(10)])

    out = asyncio.run(many())
    assert len(fake.calls) == 1 and all(o is out[0] for o in out)
