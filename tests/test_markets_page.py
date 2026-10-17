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

    async def fetch_tickers(self, symbols=None):
        self.calls.append(("tickers", symbols))
        if self.down:
            raise ConnectionError("no route")
        if symbols is not None and "ETH/USDT" in symbols:
            # like ccxt: one pair the exchange doesn't list fails the whole list
            raise ValueError("bybit does not have market symbol ETH/USDT")
        return {"BTC/USDT": {"last": 62000.5, "percentage": 1.25, "high": 63000, "low": 61000,
                             "quoteVolume": 1.5e9},
                "SHIB/USDT": {"last": 0.00001}}

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
    assert "FETCH_MS" in js.text and "STUCK_MS" in js.text  # a request that never answers can't stop updates
    r = client.get("/markets?symbol=<script>&tf=9y")  # unknown values fall back, never echoed
    assert "<script>" not in r.text.replace('<script src="/static/market.js" defer></script>', "")


def test_market_api_reads_bybit_once_per_second(world):
    client, fake, clock = world
    login(client)
    r = client.get("/api/market/candles?symbol=BTC/USDT&tf=1m").json()
    assert r["source"] == "live" and len(r["candles"]) == 3 and r["candles"][-1][4] == 102.5
    t = client.get("/api/market/tickers").json()
    # every spot ticker in one call, ours picked out; a pair Bybit doesn't list doesn't stop the rest
    assert t["source"] == "live" and t["tickers"] == [{"symbol": "BTC/USDT", "price": 62000.5, "change": 1.25,
                                                       "high": 63000, "low": 61000, "volume": 1.5e9}]
    assert ("tickers", None) in fake.calls
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


def test_market_studies_line_up_with_the_candles_and_show_paper_trades(world, monkeypatch):
    import aitrader.web.market_pages as mp
    from aitrader.web.studies import compute

    client, fake, clock = world
    assert client.get("/api/market/studies?symbol=BTC/USDT").status_code == 401
    login(client)
    r = client.get("/api/market/studies?symbol=BTC/USDT&tf=1m").json()
    assert r["ts"] == [c[0] for c in client.get("/api/market/candles?symbol=BTC/USDT&tf=1m").json()["candles"]]
    assert set(r["overlays"]) >= {"ema20", "ema200", "bb_upper", "vwap", "supertrend", "span_a", "psar"}
    assert set(r["panels"]) >= {"rsi", "macd", "macd_hist", "adx", "stoch_k"}
    assert all(len(v) == len(r["ts"]) for v in [*r["overlays"].values(), *r["panels"].values()])
    assert r["overlays"]["ema20"] == [None, None, None]  # warming up: no made-up values
    assert r["trades"] == {"open": [], "closed": []}  # no paper trader yet

    calls = []
    monkeypatch.setattr(mp, "compute", lambda rows, tail=0: calls.append(1) or compute(rows, tail))
    client.get("/api/market/studies?symbol=BTC/USDT&tf=1h")
    client.get("/api/market/studies?symbol=BTC/USDT&tf=1h")
    assert len(calls) == 1  # recomputed only when a candle changes

    # same formulas as the strategies, on enough candles to warm up
    rows = [[i * 60_000, 100 + i % 7, 102 + i % 7, 98 + i % 7, 101 + i % 5, 10] for i in range(300)]
    s = compute(rows, tail=50)
    assert len(s["overlays"]["ema200"]) == 50 and s["overlays"]["ema200"][-1] is not None
    assert 0 <= s["panels"]["rsi"][-1] <= 100 and s["levels"]


def test_chart_has_every_indicator_the_strategies_use():
    """ATR, CCI, Williams %R, Keltner, Fibonacci, volume profile and candle patterns are on the
    chart too, from the same functions the strategies call."""
    import pandas as pd

    from aitrader import indicators as ind
    from aitrader.indicators.results import fibonacci_result
    from aitrader.web.studies import compute

    rows = [[i * 60_000, 100 + i % 7, 102 + i % 7, 98 + i % 7, 101 + i % 5, 10 + i % 3] for i in range(300)]
    s = compute(rows, tail=50)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).astype(float)
    assert s["panels"]["atr"][-1] == round(float(ind.atr(df).iloc[-1]), 10)
    assert s["panels"]["cci"][-1] == round(float(ind.cci(df).iloc[-1]), 2)
    assert -100 <= s["panels"]["willr"][-1] <= 0
    assert s["overlays"]["kc_lower"][-1] < s["overlays"]["kc_mid"][-1] < s["overlays"]["kc_upper"][-1]
    assert len(s["patterns"]) == 50 and set("".join(p for p in s["patterns"] if p)) <= set("EHDIN")
    # the indicators the strategies use that the chart lacked: SMA, Donchian, Force index, OBV, MFI, CMF, ROC
    assert s["overlays"]["sma20"][-1] == round(float(df["close"].rolling(20).mean().iloc[-1]), 10)
    assert s["overlays"]["dc_lower"][-1] <= s["overlays"]["dc_upper"][-1]
    assert s["panels"]["fi"][-1] == round(float(ind.force_index(df).iloc[-1]), 10)
    assert 0 <= s["panels"]["mfi"][-1] <= 100 and -1 <= s["panels"]["cmf"][-1] <= 1
    assert s["panels"]["obv"][-1] is not None and s["panels"]["roc"][-1] is not None
    fib = fibonacci_result(df)
    assert s["fib"]["high"] == fib.features["swing_high"] and s["fib"]["leg"] == fib.metadata["leg"]
    assert [lv["ratio"] for lv in s["fib"]["levels"]] == [0.236, 0.382, 0.5, 0.618, 0.786]
    prof = s["profile"]
    assert len(prof["bins"]) == 24 and prof["val"] <= prof["poc"] <= prof["vah"]
    assert sum(b["volume"] for b in prof["bins"]) == df["volume"].tail(100).sum()  # the last 100 candles
    assert compute(rows[:1])["fib"] is None


def test_paper_trades_for_one_pair(tmp_path):
    import json

    from aitrader.web.studies import paper_trades

    path = tmp_path / "paper.json"
    path.write_text(json.dumps({
        "open": {"a|bybit|BTC/USDT|1h": {"entry_id": "a", "filled": True, "entry_ts": 5, "entry": 100,
                                         "signal": {"side": "buy", "stop": 95, "target": 110}},
                 "b|bybit|BTC/USDT|1h": {"entry_id": "b", "filled": False, "signal": {"stop": 1}},
                 "c|bybit|ETH/USDT|1h": {"entry_id": "c", "filled": True, "signal": {}}},
        "trades": [{"key": "short-a|bybit|BTC/USDT|15m", "entry_id": "short-a", "side": "sell", "entry_ts": 1,
                    "entry": 100, "exit_ts": 2, "exit": 98, "r": 0.4},
                   {"key": "c|bybit|ETH/USDT|1h", "entry_id": "c", "entry_ts": 1, "exit_ts": 2, "r": 1}]}))
    out = paper_trades(str(path), "BTC/USDT")
    assert out["open"] == [{"strategy": "a", "side": "buy", "entry_ts": 5, "entry": 100, "stop": 95,
                            "target": 110, "mark_r": None}]
    assert [t["strategy"] for t in out["closed"]] == ["short-a"] and out["closed"][0]["side"] == "sell"
    assert paper_trades(str(tmp_path / "missing.json"), "BTC/USDT") == {"open": [], "closed": []}


def test_markets_page_has_indicator_switches(world):
    client, _, _ = world
    login(client)
    r = client.get("/markets?symbol=BTC/USDT&tf=1h")
    for key in ("ema", "bb", "vwap", "st", "ich", "psar", "sr", "trades", "vol", "rsi", "macd", "adx", "stoch",
                "kc", "fib", "vp", "pat", "atr", "cci", "willr", "sma", "dc", "fi", "obv", "mfi", "cmf", "roc"):
        assert f'data-study="{key}"' in r.text
    assert "place orders" in r.text


def test_chart_starts_with_the_indicators_the_strategies_use(world):
    from aitrader.learn import Entry, Registry

    client, _, _ = world
    login(client)
    r = client.get("/markets?symbol=BTC/USDT&tf=1h")
    assert 'data-study="macd" data-on="1"' in r.text and "studies-reset" not in r.text  # no strategies: defaults
    reg = Registry("data/learn/registry.json")
    reg.add(Entry(id="a", kind="builtin", name="trend_pullback", status="paper"))
    reg.add(Entry(id="b", kind="builtin", name="supertrend_trend", status="paper"))
    reg.save()
    r = client.get("/markets?symbol=BTC/USDT&tf=1h")
    for key in ("ema", "adx", "rsi", "st", "trades", "vol"):
        assert f'data-study="{key}" data-on="1"' in r.text, key
    assert 'data-study="macd" data-on="0"' in r.text and 'data-study="bb" data-on="0"' in r.text
    assert 'data-study="atr" data-on="1"' in r.text and 'data-study="cci" data-on="0"' in r.text  # stops use ATR
    assert "EMA 20/50/200 (2)" in r.text and 'id="studies-reset"' in r.text
    assert "studies-reset" in client.get("/static/market.js").text


def test_chart_scrolls_back_through_older_candles(world):
    """Dragging or swiping the chart sideways asks for the candles before the oldest one shown."""
    client, fake, _ = world
    assert client.get(f"/api/market/history?symbol=BTC/USDT&tf=1h&before={NOW}").status_code == 401
    login(client)
    page = client.get("/markets?symbol=BTC/USDT&tf=1h").text
    for act in ("back", "out", "in", "fwd", "latest"):
        assert f'data-chart="{act}"' in page
    assert "swipe it sideways" in page
    assert client.get("/api/market/history?symbol=BTC/USDT&tf=1h&before=0").status_code == 400
    asked = []

    async def ohlcv(symbol, timeframe, since=None, limit=200):
        asked.append((symbol, timeframe, since, limit))
        return [[since + i * HOUR, 1, 2, 0.5, 1.5, 3] for i in range(limit + 2)]  # a little past `before`
    fake.fetch_ohlcv = ohlcv
    before = NOW - 10 * HOUR
    r = client.get(f"/api/market/history?symbol=BTC/USDT&tf=1h&before={before}").json()
    assert r["source"] == "live" and asked == [("BTC/USDT", "1h", before - 1000 * HOUR, 1000)]
    assert len(r["candles"]) == 1000 and r["candles"][-1][0] == before - HOUR  # nothing at or after `before`
    fake.fetch_ohlcv = FakeBybit.fetch_ohlcv.__get__(fake)
    fake.down = True  # Bybit unreachable: the collector's saved candles instead
    cut = NOW - 11 * HOUR
    r = client.get(f"/api/market/history?symbol=BTC/USDT&tf=1h&before={cut}").json()
    assert r["source"] == "database" and r["candles"] and all(c[0] < cut for c in r["candles"])
    assert len(r["candles"]) == 20
