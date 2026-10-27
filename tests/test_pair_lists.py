"""The admin's dropdown: which Bybit list each engine trades (gainers, losers, most traded, new
listings, or pairs the admin ticks)."""

import asyncio

from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.market import gainers, pairs
from aitrader.web import Auth, JobRunner, Paths, create_app
from test_gainers import MARKETS, NOW, PW, TICKERS, FakePerps, perp


def names(rows):
    return [r["symbol"] for r in rows]


def test_each_list_picks_its_own_pairs():
    assert names(gainers.pick(MARKETS, TICKERS, top=5, list_name="losers")) == ["DOWN/USDT"]
    assert names(gainers.pick(MARKETS, TICKERS, top=3, list_name="volume")) == ["BTC/USDT", "PEPE/USDT", "ONLYPERP/USDT"]
    day = 86_400_000
    markets = {**MARKETS, "NEWB/USDT:USDT": {**perp("NEWB"), "created": NOW - 2 * day},
               "NEWA/USDT:USDT": {**perp("NEWA"), "created": NOW - 9 * day},
               "AGED/USDT:USDT": {**perp("AGED"), "created": NOW - 90 * day}}
    tickers = {**TICKERS, "NEWB/USDT:USDT": {"percentage": -3.0, "quoteVolume": 2e6},  # under the usual floor
               "NEWA/USDT:USDT": {"percentage": 4.0, "quoteVolume": 30e6},
               "AGED/USDT:USDT": {"percentage": 4.0, "quoteVolume": 30e6}}
    assert names(gainers.pick(markets, tickers, top=5, list_name="new", now_ms=NOW)) == ["NEWB/USDT", "NEWA/USDT"]
    # everything open for trading, to tick from: no stablecoins, nothing inactive or dead
    everything = names(gainers.universe(markets, tickers))
    assert everything[0] == "BTC/USDT" and "THIN/USDT" in everything
    assert "USDC/USDT" not in everything and "OLD/USDT" not in everything


def test_losers_score_the_fall_and_are_never_pumps():
    rows = [{"symbol": "A/USDT", "change": -20.0, "change_1h": -16.0, "spread": 0.0},
            {"symbol": "B/USDT", "change": -5.0, "spread": 0.0}, {"symbol": "C/USDT", "change": 3.0}]
    got = gainers.rank(rows, "24h", "losers")
    assert names(got) == ["A/USDT", "B/USDT"] and got[0]["score"] == 20.0 and not got[0]["pump"]
    assert names(gainers.rank(rows, "24h", "gainers")) == ["C/USDT"]


def test_chosen_pairs_trade_without_a_scan(tmp_path):
    path = str(tmp_path / "g.json")
    env = {"AITRADER_GAINERS_FILE": path, "AITRADER_SPOT_GAINERS_FILE": str(tmp_path / "s.json"),
           "AITRADER_SQUEEZE_FILE": str(tmp_path / "q.json")}
    gainers.switch(True, path=path, list_name="chosen", chosen=["wif", "PEPE/USDT", "WIFUSDT"])
    assert gainers.symbols(path, now_ms=NOW) == ["WIF/USDT", "PEPE/USDT"]  # cleaned, no repeats, no scan needed
    assert pairs.configured_symbols(["BTC/USDT"], env=env, market="futures") == ["WIF/USDT", "PEPE/USDT"]
    data = asyncio.run(gainers.refresh(FakePerps(), path, now_ms=NOW))
    assert names(data["pairs"]) == ["WIF/USDT", "PEPE/USDT"]  # their numbers, in the admin's order
    assert "THIN/USDT" in data["universe"] and gainers.list_of(data) == "chosen"
    gainers.switch(True, path=path, list_name="losers")  # switching lists keeps the ticks for later
    asyncio.run(gainers.refresh(FakePerps(), path, now_ms=NOW))
    assert gainers.symbols(path, now_ms=NOW + 1) == ["DOWN/USDT"] and gainers.load(path)["chosen"] == ["WIF/USDT", "PEPE/USDT"]
    gainers.switch(False, path=path)
    assert gainers.symbols(path, now_ms=NOW + 1) == []


def test_dropdown_and_ticks_on_the_pairs_page(tmp_path, monkeypatch):
    from aitrader.learn import wallet
    from aitrader.web.auth import COOKIE

    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    submitted = []
    real = runner.submit
    monkeypatch.setattr(runner, "submit", lambda title, steps, meta=None: submitted.append(steps) or real(title, steps, meta))
    app = create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT"]), Auth("admin", PW, "k"), runner,
                     Paths(pairs=str(tmp_path / "ranking.json"), paper_state=str(tmp_path / "paper.json")))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": PW})
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), True)
    page = c.get("/pairs").text
    assert "Futures AI trades" in page and '<option value="volume">Most traded</option>' in page
    assert "loads every Bybit perpetual" in page  # nothing to tick from yet
    r = c.post("/pairs/gainers", data={"csrf": csrf, "on": "1", "market": "futures", "list": "volume", "count": "8"},
               follow_redirects=False)
    assert r.status_code == 303 and "most+traded" in r.headers["location"].replace("%20", "+")
    assert submitted[-1] == [["pairs", "gainers", "--market", "futures", "--count", "8", "--window", "24h",
                              "--list", "volume"]]
    for _ in range(200):  # let the (no-op) job finish
        if not any(j["status"] in ("queued", "running") for j in runner.list()):
            break
        import time
        time.sleep(0.05)
    path = gainers.path_for("futures")
    gainers.save({**gainers.load(path), "universe": ["BTC/USDT", "PEPE/USDT", "WIF/USDT"]}, path)
    assert 'name="pick" value="PEPE/USDT"' in c.get("/pairs").text
    c.post("/pairs/gainers", data={"csrf": csrf, "on": "1", "market": "futures", "list": "chosen",
                                   "pick": ["PEPE/USDT", "WIF/USDT"]})
    assert gainers.symbols(path) == ["PEPE/USDT", "WIF/USDT"]  # trades at once, before the scan
    assert submitted[-1][0][-4:] == ["--pick", "PEPE/USDT", "--pick", "WIF/USDT"]
    page = c.get("/pairs").text
    assert "Futures AI pair list: Pairs I choose" in page and 'value="PEPE/USDT" checked' in page


def test_cli_chooses_pairs(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)

    class Client(FakePerps):
        async def close(self):
            pass

    monkeypatch.setattr(gainers, "client_for", lambda market, exchange="bybit": Client())
    assert main(["--env-file", "none", "pairs", "gainers", "--market", "futures", "--list", "chosen",
                 "--pick", "PEPE/USDT", "--pick", "NOPE/USDT"]) == 0
    out = capsys.readouterr().out
    assert "Futures pairs you chose (2): PEPE/USDT, NOPE/USDT" in out
    assert "not open for trading on Bybit futures now (no prices will come): NOPE/USDT" in out
    assert main(["--env-file", "none", "pairs", "gainers", "--market", "futures", "--list", "losers"]) == 0
    assert "Futures top losers" in capsys.readouterr().out
