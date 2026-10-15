"""The squeeze scanner: Futures AI pairs whose Bollinger Bands were tight and whose volume just jumped."""

import asyncio

from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.market import pairs, squeeze
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE
from test_gainers import MARKETS, PW, TICKERS

H = 3_600_000
NOW = 1_760_000_000_000 // H * H  # on the hour


def candles(quiet_from: int, spike: float, n: int = 200, forming: bool = True) -> list:
    """Hourly candles ending with the last closed hour: wide swings, then quiet from ``quiet_from``
    bars before the end, with a last closed volume of ``spike``. Plus a forming candle that must be
    ignored."""
    out = []
    for i in range(n):
        ts = NOW - (n - i) * H
        quiet = i >= n - quiet_from
        c = 100 + (0.05 if quiet else 3.0) * (1 if i % 2 else -1)
        v = 1000 + (i % 5) * 20
        out.append([ts, c, c + 0.1, c - 0.1, c, v])
    out[-1][5] = spike
    if forming:
        out.append([NOW, 100, 130, 90, 125, 1e9])  # still forming: huge, ignored
    return out


def test_measure_finds_a_squeeze_waking_up_on_closed_candles_only():
    m = squeeze.measure(candles(30, 5000), NOW)
    assert m["squeezed"] and m["waking"] and m["width_pct"] <= squeeze.WIDTH_PCT and m["vol_z"] > 2
    assert not squeeze.measure(candles(30, 1050), NOW)["waking"]  # quiet, but no volume jump
    wide = squeeze.measure(candles(0, 5000), NOW)
    assert wide["waking"] and not wide["squeezed"]  # volume, but no squeeze
    assert squeeze.measure(candles(30, 5000, n=100), NOW) is None  # too few hours to judge
    assert squeeze.rank([{**squeeze.measure(candles(30, 5000), NOW), "symbol": "A"},
                         {**squeeze.measure(candles(30, 9000), NOW), "symbol": "B"},
                         {**wide, "symbol": "C"}])[0]["symbol"] == "B"


def test_candidates_take_the_most_traded_perps_up_or_down():
    got = [r["symbol"] for r in squeeze.candidates(MARKETS, TICKERS)]
    # risers and fallers alike, by turnover; no perp-only, stablecoin, thin or closed markets
    assert got == ["BTC/USDT", "PEPE/USDT", "DOWN/USDT", "WIF/USDT"]


class FakePerps:
    async def load_markets(self):
        return MARKETS

    async def fetch_tickers(self):
        return TICKERS

    async def fetch_ohlcv(self, contract, tf, limit=0):
        assert tf == "1h" and limit >= squeeze.WEEK + squeeze.BB_N
        if contract.startswith("BTC"):
            raise RuntimeError("timeout")  # one coin failing doesn't stop the scan
        return candles(30, 5000) if contract.startswith(("DOWN", "WIF")) else candles(0, 5000)


def test_refresh_switch_and_futures_pairs(tmp_path):
    path = str(tmp_path / "squeeze.json")
    env = {"AITRADER_SQUEEZE_FILE": path, "AITRADER_GAINERS_FILE": str(tmp_path / "g.json"),
           "AITRADER_SPOT_GAINERS_FILE": str(tmp_path / "gs.json")}
    assert squeeze.symbols(path) == [] and not squeeze.due(path)
    squeeze.switch(True, top=1, path=path)
    assert squeeze.due(path, now_ms=NOW)
    data = asyncio.run(squeeze.refresh(FakePerps(), path, now_ms=NOW))
    assert data["scanned"] == 3 and [p["symbol"] for p in data["pairs"]] == ["DOWN/USDT", "WIF/USDT"]
    assert squeeze.symbols(path, now_ms=NOW + 1) == ["DOWN/USDT"]  # top 1
    # Futures AI trades them (with no gainers list); Spot AI never does
    assert pairs.configured_symbols(["ETH/USDT"], env=env, now_ms=NOW + 1, market="futures") == ["DOWN/USDT"]
    assert pairs.configured_symbols(["ETH/USDT"], env=env, now_ms=NOW + 1, market="spot") == ["ETH/USDT"]
    assert squeeze.symbols(path, now_ms=NOW + 7 * H) == []  # stale
    squeeze.switch(False, path=path)
    assert squeeze.symbols(path, now_ms=NOW + 1) == [] and not squeeze.due(path, now_ms=NOW * 2)
    assert squeeze.clamp_top(99) == squeeze.MAX_TOP and squeeze.clamp_top("x") == squeeze.DEFAULT_TOP


def test_pairs_page_squeeze_card(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    app = create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT"]), Auth("admin", PW, "k"), runner,
                     Paths(pairs=str(tmp_path / "ranking.json"), paper_state=str(tmp_path / "paper.json")))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": PW})
    assert "Squeeze scanner" not in c.get("/pairs").text  # Spot AI view: Futures only
    from aitrader.learn import wallet
    wallet.set_market(wallet.market_path(str(tmp_path / "paper.json")), True)
    assert "Trade squeezes" in c.get("/pairs").text
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/pairs/squeeze", data={"csrf": "x", "on": "1"}).status_code == 403
    r = c.post("/pairs/squeeze", data={"csrf": csrf, "on": "1", "count": "3"}, follow_redirects=False)
    assert r.status_code == 303 and "Scanning" in r.headers["location"]
    runner.wait_idle()
    [job] = [j for j in runner.list() if j["meta"].get("kind") == "squeeze"]
    assert job["steps"] == [["pairs", "squeeze", "--count", "3"]]
    import time

    squeeze.switch(True, top=3)
    now = int(time.time() * 1000)
    asyncio.run(squeeze.refresh(FakePerps(), now_ms=now // H * H))
    page = c.get("/pairs").text
    assert "Stop trading squeezes" in page and "WIF/USDT" in page and ">squeeze<" in page
    assert 'data-symbol="WIF/USDT"' in c.get("/markets").text
    c.post("/pairs/squeeze", data={"csrf": csrf, "on": "0"})
    assert not squeeze.load()["on"] and "Trade squeezes" in c.get("/pairs").text
