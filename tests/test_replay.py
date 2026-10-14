"""Replay backtest: the server side (candles plus trades for a chosen month) and the page."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, validate_spec
from aitrader.learn.replay import replay
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

from test_learn import EVERY_UP_BAR, fill_store
from test_strategies import synthetic

PW = "correct horse battery"
HOUR = 3_600_000
START = int(datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2025, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)


def make_store(path):
    df = synthetic(n=2000)
    df["ts"] = df["ts"] + START - 1000 * HOUR  # ~42 days of warm-up before March 2025
    store = CandleStore(str(path))
    fill_store(store, df)
    return store


def test_replay_returns_month_and_trades(tmp_path):
    store = make_store(tmp_path / "c.db")
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    daily = Entry(id="d", kind="rule", name="daily", spec=validate_spec(dict(EVERY_UP_BAR, name="d", timeframes=["1d"])))
    res = replay([up, daily], store, "bybit", "BTC/USDT", "1h", START, END)
    assert res["candles"][0][0] == START and res["candles"][-1][0] < END and len(res["candles"]) == 31 * 24
    assert [s["id"] for s in res["strategies"]] == ["up"]  # the daily strategy doesn't trade 1h
    assert res["trades"] and all(START <= t["entry_ts"] < END for t in res["trades"])
    assert all(t["exit_ts"] >= t["entry_ts"] and t["strategy"] == "up" for t in res["trades"])
    only = replay([up, daily], store, "bybit", "BTC/USDT", "1h", START, END, only="d")
    assert only["trades"] == [] and only["candles"]
    empty = replay([up], store, "bybit", "ETH/USDT", "1h", START, END)
    assert empty["candles"] == [] and empty["trades"] == []
    store.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_store(tmp_path / "x.db").close()
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    import aitrader.web.replay_pages as rp
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    monkeypatch.setattr(rp, "known_entries", lambda *a, **k: [up])
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT", "ETH/USDT"]),
                     Auth("admin", PW, "k"), runner, Paths(report=str(tmp_path / "none.json")))
    return TestClient(app), runner


def test_replay_api_and_page(client):
    c, runner = client
    assert c.get("/replay", follow_redirects=False).status_code == 303
    assert c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03").status_code == 401
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/replay").text
    assert 'id="replay-form"' in page and "up_bar" in page and 'data-speed="2"' in page
    assert '/static/replay.js' in page and c.get("/static/replay.js").headers["content-type"].startswith("text/javascript")
    r = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&months=1").json()
    assert len(r["candles"]) == 31 * 24 and r["trades"] and r["strategies"][0]["name"] == "up_bar"
    assert c.get("/api/replay?symbol=DOGE/USDT&tf=1h&month=2025-03").status_code == 400
    assert c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2999-01").status_code == 400
    assert c.get("/api/replay?symbol=ETH/USDT&tf=1h&month=2025-03").json()["candles"] == []
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/replay/download", data={"csrf": "x"}).status_code == 403
    d = c.post("/replay/download", data={"csrf": csrf, "symbol": "ETH/USDT", "tf": "1h", "month": "2025-03",
                                         "months": "1"}, follow_redirects=False)
    assert d.status_code == 303 and "month=2025-03" in d.headers["location"]
    job = [j for j in runner.list() if j["meta"].get("kind") == "download"][0]
    assert job["steps"] == [["download", "--since", "2025-02-16", "--until", "2025-04-01", "--exchange", "bybit",
                             "--symbol", "ETH/USDT", "--timeframe", "1h"]]
    assert d.headers["location"].endswith("&download=" + job["id"])
    runner.wait_idle()
    p = c.get(f"/api/replay/download/{job['id']}").json()
    assert p["status"] == "done" and p["pct"] == 100 and p["have"] == 0 and p["want"] == 44 * 24
    assert c.get("/api/replay/download/nosuchjob").status_code == 404
    assert 'id="replay-progress"' in c.get("/replay").text


def test_download_status_counts_saved_candles(tmp_path):
    from aitrader.web.replay_pages import download_status

    s = download_status("running", 372, START, END, "1h")
    assert s["want"] == 744 and s["pct"] == 50
    assert download_status("running", 744, START, END, "1h")["pct"] == 99  # 100 only once the job ends
    assert download_status("done", 700, START, END, "1h")["pct"] == 100
    store = make_store(tmp_path / "c.db")
    assert store.count("bybit", "BTC/USDT", "1h", START, END) == 744
    store.close()


RSI_UP = dict(EVERY_UP_BAR, name="rsi_up", entry=[{"left": "close", "op": ">", "right": "open"},
                                                  {"left": "rsi(14)", "op": ">", "right": 50}])


def test_replay_runs_a_ranked_list_and_marks_shorts(tmp_path):
    from aitrader.learn.registry import short_twin

    store = make_store(tmp_path / "c.db")
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    rsi = Entry(id="rsi", kind="rule", name="rsi_up", spec=validate_spec(RSI_UP))
    daily = Entry(id="d", kind="rule", name="daily", spec=validate_spec(dict(EVERY_UP_BAR, name="d", timeframes=["1d"])))
    short = short_twin(up)
    # best first; the daily one can't trade 1h, so the best 2 that can are rsi and up
    res = replay([up, rsi, daily, short], store, "bybit", "BTC/USDT", "1h", START, END,
                 only=["d", "rsi", "up", short.id], top=2)
    assert [s["id"] for s in res["strategies"]] == ["rsi", "up"]
    assert res["trades"] and all(t["side"] == "buy" and t["stop"] < t["entry"] for t in res["trades"])
    shorts = replay([up, short], store, "bybit", "BTC/USDT", "1h", START, END, only=short.id)
    assert shorts["trades"] and all(t["side"] == "sell" and t["stop"] > t["entry"] for t in shorts["trades"])
    store.close()


def test_replay_page_offers_the_best_strategies_with_their_indicators(client, monkeypatch):
    import aitrader.web.replay_pages as rp

    c, _runner = client
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    rsi = Entry(id="rsi", kind="rule", name="rsi_up", spec=validate_spec(RSI_UP))
    monkeypatch.setattr(rp, "known_entries", lambda *a, **k: [up, rsi])
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/replay").text
    for n in (10, 20, 30):
        assert f'<option value="top:{n}">Top {n} strategies (best ranked)</option>' in page
    assert 'data-study="rsi"' in page and 'data-study="trades"' not in page
    assert page.index("/static/chartstudies.js") < page.index("/static/replay.js")
    assert 'id="legend"' in page and 'id="read"' in page
    r = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&strategy=top:10").json()
    assert r["top"] == 10 and {s["id"] for s in r["strategies"]} == {"up", "rsi"}
    assert r["indicators"] == {"ema": 1, "rsi": 1}  # up_bar uses sma(20), drawn with the EMAs
    assert [s["indicators"] for s in r["strategies"] if s["id"] == "rsi"] == [["rsi"]]
    st = r["studies"]
    n = len(r["candles"])
    assert len(st["overlays"]["ema20"]) == n and len(st["panels"]["rsi"]) == n and len(st["patterns"]) == n
    assert st["overlays"]["ema200"][0] is not None  # warmed up on the candles before the month
    # levels, Fibonacci and the volume profile would come from the whole month: the page works them out
    assert not {"levels", "fib", "profile"} & set(st)
    one = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&strategy=rsi").json()
    assert [s["id"] for s in one["strategies"]] == ["rsi"] and one["top"] == 0
    assert c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&strategy=top:7").json()["top"] == 0


def test_replay_studies_match_the_markets_formulas(tmp_path):
    """The replay's indicator at a candle equals the indicator worked out from candles up to it."""
    from aitrader.web import studies
    from aitrader.web.replay_pages import replay_studies

    store = make_store(tmp_path / "c.db")
    res = replay([], store, "bybit", "BTC/USDT", "1h", START, END)
    st = replay_studies(store, "bybit", res)
    rows = store.load("bybit", "BTC/USDT", "1h", start=START - 300 * HOUR, end=END)
    k = 100  # the 101st shown candle, from only the candles up to it
    upto = [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in rows if c.ts <= res["candles"][k][0]]
    alone = studies.compute(upto, tail=1)
    assert st["overlays"]["ema50"][k] == pytest.approx(alone["overlays"]["ema50"][0])
    assert st["panels"]["rsi"][k] == pytest.approx(alone["panels"]["rsi"][0])
    store.close()
