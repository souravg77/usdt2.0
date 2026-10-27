"""Futures AI's Perpetual / Futures (dated) switch: Bybit's dated USDT futures, paper only."""

import asyncio

from aitrader.data.perp import bybit_name, expiry_ms, is_dated, perp_symbol
from aitrader.learn import wallet
from aitrader.market import gainers, perp_limits

H = 3_600_000
NOW = 1_761_800_000_000  # 2025-10-30 05:53 UTC


def market(sym, base, kind, expiry=None, lev=100):
    return {"symbol": sym, "base": base, "quote": "USDT", "settle": "USDT", "linear": True, "active": True,
            "swap": kind == "swap", "future": kind == "future", "expiry": expiry,
            "limits": {"leverage": {"max": lev}}}


MARKETS = {
    "BTC/USDT:USDT": market("BTC/USDT:USDT", "BTC", "swap"),
    "SOL/USDT:USDT": market("SOL/USDT:USDT", "SOL", "swap", lev=50),
    "BTC/USDT:USDT-251031": market("BTC/USDT:USDT-251031", "BTC", "future", expiry_ms("BTC/USDT:USDT-251031"), 25),
    "BTC/USDT:USDT-251226": market("BTC/USDT:USDT-251226", "BTC", "future", expiry_ms("BTC/USDT:USDT-251226"), 25),
    "ETH/USDT:USDT-251226": market("ETH/USDT:USDT-251226", "ETH", "future", expiry_ms("ETH/USDT:USDT-251226"), 25),
}
TICKERS = {
    "BTC/USDT:USDT": {"percentage": 1.0, "quoteVolume": 5e9, "last": 80000, "bid": 80000, "ask": 80001},
    "SOL/USDT:USDT": {"percentage": 4.0, "quoteVolume": 9e8, "last": 200, "bid": 200, "ask": 200.01},
    "BTC/USDT:USDT-251031": {"percentage": 0.9, "quoteVolume": 7e5, "last": 80000, "bid": 80000, "ask": 80002},
    "BTC/USDT:USDT-251226": {"percentage": 0.8, "quoteVolume": 2e6, "last": 80900, "bid": 80900, "ask": 80903},
    "ETH/USDT:USDT-251226": {"percentage": 2.5, "quoteVolume": 3e5, "last": 4000, "bid": 4000, "ask": 4000.2},
}


class FakeBybit:
    async def load_markets(self):
        return MARKETS

    async def fetch_tickers(self):
        return TICKERS

    async def fetch_ohlcv(self, sym, tf, limit=26):
        return []


def test_dated_names_and_expiry():
    assert is_dated("BTC/USDT:USDT-261030") and not is_dated("BTC/USDT") and not is_dated("BTC/USDT:USDT")
    assert bybit_name("BTC/USDT:USDT-261030") == "BTCUSDT-30OCT26" and bybit_name("PEPE/USDT") == "PEPE/USDT"
    assert expiry_ms("BTC/USDT:USDT-261030") == 1_793_347_200_000  # 2026-10-30 08:00 UTC
    assert expiry_ms("BTC/USDT") is None
    assert perp_symbol("BTC/USDT:USDT-261030") == "BTC/USDT:USDT-261030"  # its own candles, never the perp's


def test_each_contract_type_scans_only_its_own_contracts():
    perps = gainers.pick(MARKETS, TICKERS, top=10, min_turnover=1e6, now_ms=NOW)
    assert [p["symbol"] for p in perps] == ["SOL/USDT", "BTC/USDT"]
    dated = gainers.pick(MARKETS, TICKERS, top=10, contract="dated", now_ms=NOW)
    # ETH-26DEC first (biggest rise), then BTC-26DEC; BTC-31OCT expires within 48 hours: left out
    assert [p["symbol"] for p in dated] == ["ETH/USDT:USDT-251226", "BTC/USDT:USDT-251226"]
    assert dated[0]["expiry"] == expiry_ms("ETH/USDT:USDT-251226")
    assert perp_limits.from_markets(MARKETS) == {"BTC/USDT": 100.0, "SOL/USDT": 50.0, "BTC/USDT:USDT-251031": 25.0,
                                                 "BTC/USDT:USDT-251226": 25.0, "ETH/USDT:USDT-251226": 25.0}


def test_switching_contract_type_drops_the_old_list_until_it_is_scanned(tmp_path):
    path = str(tmp_path / "gainers.json")
    gainers.switch(True, top=5, path=path)
    asyncio.run(gainers.refresh(FakeBybit(), path, now_ms=NOW))
    assert gainers.symbols(path, now_ms=NOW + 1) == ["SOL/USDT", "BTC/USDT"]
    gainers.switch(True, path=path, contract="dated")
    assert gainers.symbols(path, now_ms=NOW + 1) == []  # the perpetual list is not traded as dated futures
    asyncio.run(gainers.refresh(FakeBybit(), path, now_ms=NOW + 2))
    assert gainers.symbols(path, now_ms=NOW + 3) == ["ETH/USDT:USDT-251226", "BTC/USDT:USDT-251226"]
    assert gainers.load(path)["scanned_contract"] == "dated"


def test_the_futures_card_has_the_contract_switch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aitrader.config import Settings
    from aitrader.web import Auth, JobRunner, Paths, create_app
    from aitrader.web.auth import COOKIE
    from test_gainers import PW

    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    c = TestClient(create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT"]),
                              Auth("admin", PW, "k"), runner,
                              Paths(pairs=str(tmp_path / "ranking.json"), paper_state=str(tmp_path / "paper.json"))))
    c.post("/login", data={"user": "admin", "password": PW})
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), True)
    page = c.get("/pairs").text
    assert "Futures top gainers: Perpetual" in page and '<select name="contract">' in page
    assert '<option value="dated">Futures (dated)</option>' in page
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    r = c.post("/pairs/gainers", data={"csrf": csrf, "on": "1", "count": "5", "window": "24h", "contract": "dated"},
               follow_redirects=False)
    assert "dated%20futures" in r.headers["location"]
    runner.wait_idle()
    [job] = [j for j in runner.list() if j["meta"].get("kind") == "gainers"]
    assert job["steps"][0][-2:] == ["--contract", "dated"]
    gainers.switch(True, top=5, contract="dated")
    perp_limits.save(perp_limits.from_markets(MARKETS), perp_limits.DEFAULT_PATH)
    asyncio.run(gainers.refresh(FakeBybit(), now_ms=NOW))
    page = c.get("/pairs").text
    assert "Futures top gainers: Futures (dated)" in page and "paper only" in page
    assert "ETHUSDT-26DEC25" in page and "<th>Expires</th>" in page and "<td>25x</td>" in page


def test_a_paper_position_on_a_dated_contract_closes_before_it_expires(tmp_path, monkeypatch):
    from test_learn import EVERY_UP_BAR, fill_store, synthetic

    from aitrader.data.store import CandleStore
    from aitrader.learn import Entry, PaperTrader, Registry
    from aitrader.learn import paper as paper_mod
    from aitrader.learn.rules import validate_spec

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=700)
    fill_store(store, df)
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    state = str(tmp_path / "paper_state.json")
    wallet.set_engine(wallet.engines_path(state), "futures", False)
    trader = PaperTrader(store, str(tmp_path / "reg.json"), ["bybit"], ["BTC/USDT"], state_path=state)
    i = 600
    while not any(p["filled"] for p in trader.state["open"].values()):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
        i += 1
    now = int(df.ts.iloc[i]) + H
    # the contract expires 30 minutes from now: inside the last hour, so the trade is closed
    monkeypatch.setattr(paper_mod, "expiry_ms", lambda sym: now + 30 * 60_000)
    trader.tick(now_ms=now)
    assert trader.state["trades"][-1]["exit_reason"] == "contract expiry"
    assert not any(p["filled"] for p in trader.state["open"].values())

