"""Top gainers: Bybit USDT perpetuals that rose most in 24h, added to the traded pairs while on."""

import asyncio
import time

from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.market import gainers, pairs
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

PW = "correct horse battery"
NOW = 1_760_000_000_000


def perp(base, active=True):
    return {"symbol": f"{base}/USDT:USDT", "base": base, "quote": "USDT", "settle": "USDT", "swap": True,
            "linear": True, "active": active}


def spot(base):
    return {"symbol": f"{base}/USDT", "base": base, "quote": "USDT", "spot": True, "active": True}


MARKETS = {m["symbol"]: m for m in [
    perp("PEPE"), spot("PEPE"), perp("WIF"), spot("WIF"), perp("BTC"), spot("BTC"),
    perp("ONLYPERP"),  # no spot market: no candles, left out
    perp("USDC"), spot("USDC"),  # stablecoin
    perp("THIN"), spot("THIN"),  # too little turnover
    perp("DOWN"), spot("DOWN"),  # fell
    perp("OLD", active=False), spot("OLD"),
]}
TICKERS = {
    "PEPE/USDT:USDT": {"percentage": 31.5, "quoteVolume": 90e6, "last": 0.00002},
    "WIF/USDT:USDT": {"percentage": 12.0, "quoteVolume": 40e6, "last": 2.1},
    "BTC/USDT:USDT": {"percentage": 1.2, "quoteVolume": 5e9, "last": 80000},
    "ONLYPERP/USDT:USDT": {"percentage": 80.0, "quoteVolume": 50e6},
    "USDC/USDT:USDT": {"percentage": 0.1, "quoteVolume": 50e6},
    "THIN/USDT:USDT": {"percentage": 60.0, "quoteVolume": 1e6},
    "DOWN/USDT:USDT": {"percentage": -9.0, "quoteVolume": 50e6},
    "OLD/USDT:USDT": {"percentage": 50.0, "quoteVolume": 50e6},
}


class FakePerps:
    async def load_markets(self):
        return MARKETS

    async def fetch_tickers(self):
        return TICKERS


def test_pick_keeps_liquid_perp_risers_that_also_trade_on_spot():
    got = gainers.pick(MARKETS, TICKERS, top=5)
    assert [g["symbol"] for g in got] == ["PEPE/USDT", "WIF/USDT", "BTC/USDT"]
    assert got[0]["change"] == 31.5
    assert [g["symbol"] for g in gainers.pick(MARKETS, TICKERS, top=3)][:2] == ["PEPE/USDT", "WIF/USDT"]


def test_switch_refresh_and_configured_symbols(tmp_path):
    path = str(tmp_path / "gainers.json")
    env = {"AITRADER_GAINERS_FILE": path}
    assert gainers.symbols(path) == [] and not gainers.due(path)
    gainers.switch(True, top=3, path=path)
    assert gainers.due(path, now_ms=NOW)  # on, no list yet
    asyncio.run(gainers.refresh(FakePerps(), path, now_ms=NOW))
    assert gainers.symbols(path, now_ms=NOW + 1) == ["PEPE/USDT", "WIF/USDT", "BTC/USDT"]
    assert not gainers.due(path, now_ms=NOW + 1) and gainers.due(path, now_ms=NOW + gainers.REFRESH_MS)
    fixed = ["BTC/USDT", "ETH/USDT"]
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 1) == ["BTC/USDT", "ETH/USDT", "PEPE/USDT", "WIF/USDT"]
    # a list the collector stopped refreshing isn't traded
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 7 * 3_600_000) == fixed
    gainers.switch(False, path=path)
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 1) == fixed and not gainers.due(path, now_ms=NOW * 2)
    assert gainers.clamp_top(100) == gainers.MAX_TOP and gainers.clamp_top("x") == gainers.DEFAULT_TOP


def test_pairs_page_button_and_markets_badge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    app = create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT", "ETH/USDT"]),
                     Auth("admin", PW, "k"), runner,
                     Paths(pairs=str(tmp_path / "ranking.json"), paper_state=str(tmp_path / "paper.json")))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/pairs").text
    assert "Top gainers (Bybit futures)" in page and "Trade the top gainers" in page
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/pairs/gainers", data={"csrf": "x", "on": "1"}).status_code == 403
    r = c.post("/pairs/gainers", data={"csrf": csrf, "on": "1", "count": "5"}, follow_redirects=False)
    assert r.status_code == 303 and "Fetching" in r.headers["location"]
    runner.wait_idle()
    [job] = [j for j in runner.list() if j["meta"].get("kind") == "gainers"]
    assert job["steps"] == [["pairs", "gainers", "--count", "5"]]
    # the list arrived: it is shown, traded and marked on the Markets page
    gainers.switch(True, top=5)
    asyncio.run(gainers.refresh(FakePerps(), now_ms=int(time.time() * 1000)))
    page = c.get("/pairs").text
    assert "PEPE/USDT" in page and "+31.50%" in page and "Stop trading the top gainers" in page
    page = c.get("/markets").text
    assert 'data-symbol="PEPE/USDT"' in page and "top gainer" in page
    c.post("/pairs/gainers", data={"csrf": csrf, "on": "0"})
    assert not gainers.load()["on"] and 'data-symbol="PEPE/USDT"' not in c.get("/markets").text
