"""Top gainers: Bybit USDT perpetuals that rose most in 24h, added to the traded pairs while on."""

import asyncio
import time

import pytest

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
    # each workspace shows only its own scanner: Spot first (the default view)
    assert "Spot top gainers" in page and "Futures top gainers" not in page and "Trade the top gainers" in page
    from aitrader.learn import wallet
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), True)
    page = c.get("/pairs").text
    assert "Futures top gainers" in page and "Spot top gainers" not in page
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/pairs/gainers", data={"csrf": "x", "on": "1"}).status_code == 403
    r = c.post("/pairs/gainers", data={"csrf": csrf, "on": "1", "count": "5", "window": "4h"}, follow_redirects=False)
    assert r.status_code == 303 and "Scanning" in r.headers["location"]
    runner.wait_idle()
    [job] = [j for j in runner.list() if j["meta"].get("kind") == "gainers"]
    assert job["steps"] == [["pairs", "gainers", "--market", "futures", "--count", "5", "--window", "4h"]]
    # the list arrived: it is shown, traded and marked on the Markets page of its workspace
    gainers.switch(True, top=5)
    asyncio.run(gainers.refresh(FakePerps(), now_ms=int(time.time() * 1000)))
    page = c.get("/pairs").text
    assert "PEPE/USDT" in page and "+31.50%" in page and "Stop trading the top gainers" in page
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), False)
    assert 'data-symbol="PEPE/USDT"' not in c.get("/markets").text  # the Spot AI view: not its gainer
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), True)
    page = c.get("/markets").text
    assert 'data-symbol="PEPE/USDT"' in page and "top gainer" in page
    assert 'data-symbol="ETH/USDT"' not in page  # Futures shows only its top gainers
    # ...plus every pair Futures AI still holds a position in; Spot AI's positions stay on Spot
    import json as _json

    with open(str(tmp_path / "paper.json"), "w") as fh:
        _json.dump({"open": {"s1|bybit|DOGE/USDT|1d": {"signal": {"side": "sell"}},
                             "s1|bybit|ADA/USDT|1h|futures": {"perp": True, "signal": {"side": "buy"}},
                             "s1|bybit|ETH/USDT|1h": {"perp": False, "signal": {"side": "buy"}}}}, fh)
    page = c.get("/markets").text
    assert 'data-symbol="DOGE/USDT"' in page and 'data-symbol="ADA/USDT"' in page and "open position" in page
    assert 'data-symbol="ETH/USDT"' not in page
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), False)
    page = c.get("/markets").text
    assert 'data-symbol="ETH/USDT"' in page and 'data-symbol="DOGE/USDT"' not in page
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), True)
    c.post("/pairs/gainers", data={"csrf": csrf, "on": "0", "market": "futures"})
    assert not gainers.load()["on"] and 'data-symbol="PEPE/USDT"' not in c.get("/markets").text


SPOT_TICKERS = {
    "PEPE/USDT": {"percentage": 30.0, "quoteVolume": 80e6, "last": 0.00002, "bid": 0.0000199, "ask": 0.00002},
    "WIF/USDT": {"percentage": 12.0, "quoteVolume": 40e6, "last": 2.1, "bid": 2.099, "ask": 2.1},
    "BTC/USDT": {"percentage": 1.2, "quoteVolume": 5e9, "last": 80000, "bid": 79999, "ask": 80000},
    "ONLYPERP/USDT:USDT": {"percentage": 80.0, "quoteVolume": 50e6},
    "THIN/USDT": {"percentage": 60.0, "quoteVolume": 1e6},
}


def test_spot_scanner_scores_and_flags_pumps(tmp_path):
    got = gainers.pick(MARKETS, SPOT_TICKERS, top=5, market="spot")
    # PEPE's spread is 0.5%, over the 0.2% limit; perps and thin pairs are left out
    assert [g["symbol"] for g in got] == ["WIF/USDT", "BTC/USDT"]
    assert got[0]["spread"] == pytest.approx(0.0476, abs=1e-3)
    # 1h and 4h rise and relative volume from hourly candles
    bars = [[i, 1, 1, 1, 100.0, 10.0] for i in range(24)] + [[24, 1, 1, 1, 110.0, 10.0], [25, 1, 1, 1, 121.0, 40.0]]
    row = gainers.enrich({"symbol": "X/USDT", "change": 21.0, "spread": 0.1}, bars)
    assert row["change_1h"] == 10.0 and row["change_4h"] == 21.0 and row["relvol"] == pytest.approx(40 / 10.0)
    # score = rise over the window + 2 x (relative volume - 1), at most 6, - 5 x spread
    assert gainers.score(row, "1h") == pytest.approx(10 + 6 - 0.5)
    assert gainers.score(row, "24h") == pytest.approx(21 + 6 - 0.5)
    pump = gainers.enrich({"symbol": "P/USDT", "change": 25.0}, bars[:-1] + [[25, 1, 1, 1, 130.0, 10.0]])
    ranked = gainers.rank([row, pump], "1h")
    assert ranked[0]["symbol"] == "P/USDT" and ranked[0]["pump"] == "up 18% in an hour"

    class FakeSpot:
        async def load_markets(self):
            return MARKETS

        async def fetch_tickers(self):
            return {**SPOT_TICKERS, "PEPE/USDT": {**SPOT_TICKERS["PEPE/USDT"], "bid": 0.00002}}

        async def fetch_ohlcv(self, symbol, tf, limit=26):
            return bars[:-1] + [[25, 1, 1, 1, 130.0, 10.0]] if symbol == "PEPE/USDT" else bars

    path = str(tmp_path / "spot.json")
    gainers.switch(True, top=5, path=path, window="1h")
    data = asyncio.run(gainers.refresh(FakeSpot(), path, now_ms=NOW, market="spot"))
    assert data["window"] == "1h" and data["market"] == "spot" and data["scanned"] == 3
    assert data["pairs"][0]["symbol"] == "PEPE/USDT" and data["pairs"][0]["pump"]
    # the pump is listed, not traded; the same 1h rise ranks the tighter spread first
    assert gainers.symbols(path, now_ms=NOW + 1) == ["BTC/USDT", "WIF/USDT"]
    # each workspace gets its own scanner's gainers
    fut = str(tmp_path / "fut.json")
    gainers.switch(True, top=3, path=fut)
    asyncio.run(gainers.refresh(FakePerps(), fut, now_ms=NOW))
    env = {"AITRADER_GAINERS_FILE": fut, "AITRADER_SPOT_GAINERS_FILE": path}
    fixed = ["ETH/USDT"]
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 1, market="spot") == ["ETH/USDT", "BTC/USDT", "WIF/USDT"]
    # Futures AI trades only its gainers while that list is on (plus pairs it still holds)
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 1, market="futures") == \
        ["PEPE/USDT", "WIF/USDT", "BTC/USDT"]
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 1, market="futures", keep=["SOL/USDT"]) == \
        ["PEPE/USDT", "WIF/USDT", "BTC/USDT", "SOL/USDT"]
    gainers.switch(False, path=fut)  # off: the usual pairs again
    assert pairs.configured_symbols(fixed, env=env, now_ms=NOW + 1, market="futures") == ["ETH/USDT"]
