"""The Markets page: prices and 24h change per pair from the candle database, and a chart."""

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.data.store import CandleStore
from aitrader.models import Candle
from aitrader.web import Auth, JobRunner, create_app

PW = "correct horse battery"
NOW = 1_760_000_000_000  # ms


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "aitrader.db")
    store = CandleStore(db)
    store.migrate()
    hour = 3_600_000
    # BTC: 30 hourly candles rising from 100 to 129, the last one closed an hour ago
    store.upsert([Candle("binance", "BTC/USDT", "1h", NOW - (30 - i) * hour - hour, 100 + i, 101 + i,
                         99 + i, 100.5 + i, 1) for i in range(30)])
    store.close()
    settings = Settings(db_path=db, symbols=["BTC/USDT", "ETH/USDT"], timeframes=["1m", "1h", "1d"])
    app = create_app(settings, Auth("admin", PW, "k"), JobRunner(str(tmp_path / "j"), env_file="none"))
    import aitrader.web.market_pages as mp
    monkeypatch.setattr(mp.time, "time", lambda: NOW / 1000)
    c = TestClient(app)
    return c


def test_markets_needs_login(client):
    r = client.get("/markets", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_markets_shows_prices_and_a_chart(client):
    client.post("/login", data={"user": "admin", "password": PW})
    r = client.get("/markets")
    assert r.status_code == 200 and '<a href="/markets">Markets</a>' in r.text
    assert "129.50" in r.text  # last close
    assert "+" in r.text and "%" in r.text  # 24h change
    assert "ETH/USDT" in r.text and "no data yet" in r.text
    assert "<svg" in r.text and r.text.count("<rect") == 30  # one candle body per bar
    assert 'http-equiv="refresh" content="30"' in r.text
    r = client.get("/markets?symbol=ETH/USDT&tf=1d")
    assert "No 1d candles yet for ETH/USDT" in r.text
    r = client.get("/markets?symbol=<script>&tf=9y")  # unknown values fall back, never echoed
    assert "<script>" not in r.text and "BTC/USDT · 1h" in r.text


def test_markets_without_any_prices_points_to_the_collector(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(db_path=str(tmp_path / "x.db")), Auth("admin", PW, "k"),
                     JobRunner(str(tmp_path / "j"), env_file="none"))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": PW})
    assert "No prices yet" in c.get("/markets").text
