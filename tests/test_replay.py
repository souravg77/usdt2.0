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
