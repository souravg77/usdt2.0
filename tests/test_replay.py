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
    assert job["steps"] == [["download", "--since", "2025-02-16", "--until", "2025-04-01", "--market", "spot",
                             "--exchange", "bybit",
                             "--symbol", "ETH/USDT", "--timeframe", "1h"]]
    assert d.headers["location"].endswith("&download=" + job["id"])
    runner.wait_idle()
    p = c.get(f"/api/replay/download/{job['id']}").json()
    assert p["status"] == "done" and p["pct"] == 100 and p["have"] == 0 and p["want"] == 44 * 24
    assert "first" not in p  # no prices at all: nothing to say about a listing
    # a coin listed after the period: the page says when its prices start (Shawn's JCT/USDT 30m)
    from aitrader.models import Candle

    jun = int(datetime(2025, 6, 10, tzinfo=timezone.utc).timestamp() * 1000)
    st = CandleStore("x.db")  # the client fixture runs in tmp_path
    st.upsert([Candle("bybit", "ETH/USDT", "1h", jun, 1, 2, 0.5, 1.5, 1)])
    st.close()
    p = c.get(f"/api/replay/download/{job['id']}").json()
    assert p["first"] == jun and p["first_month"] == "2025-06" and p["first_text"].startswith("2025-06-10")
    assert "from-listing" in c.get("/static/replay.js").text
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
    assert r["indicators"] == {"sma": 1, "rsi": 1}  # up_bar uses sma(20), drawn as SMA
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


def test_replay_shows_only_the_workspace_pairs(client, monkeypatch, tmp_path):
    """Shawn: Futures pairs on the Futures replay, Spot pairs on the Spot replay."""
    import json
    import time

    from aitrader.learn import wallet

    c, _runner = client
    fut = str(tmp_path / "gainers.json")
    with open(fut, "w") as fh:
        json.dump({"on": True, "generated": int(time.time() * 1000), "pairs": [{"symbol": "SOL/USDT"}]}, fh)
    monkeypatch.setenv("AITRADER_GAINERS_FILE", fut)
    monkeypatch.setenv("AITRADER_SPOT_GAINERS_FILE", str(tmp_path / "none.json"))
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/replay").text
    assert '<option value="BTC/USDT" selected>' in page and "SOL/USDT" not in page and "Spot AI workspace" in page

    wallet.set_market(wallet.market_path("data/learn/paper_state.json"), True)
    page = c.get("/replay").text
    assert '<option value="SOL/USDT" selected>' in page and "BTC/USDT" not in page and "Futures AI workspace" in page
    assert c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03").status_code == 400  # a Spot pair here


def test_spot_replay_leaves_out_short_strategies(client, monkeypatch, tmp_path):
    import json
    import time

    import aitrader.web.replay_pages as rp
    from aitrader.learn import wallet
    from aitrader.learn.registry import short_twin

    c, _runner = client
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    monkeypatch.setattr(rp, "known_entries", lambda *a, **k: [up, short_twin(up)])
    c.post("/login", data={"user": "admin", "password": PW})
    ids = {s["id"] for s in c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03").json()["strategies"]}
    assert "up" in ids and "short-up" not in ids
    fut = str(tmp_path / "gainers.json")  # Futures AI trades its top gainers: BTC is one today
    with open(fut, "w") as fh:
        json.dump({"on": True, "generated": int(time.time() * 1000), "pairs": [{"symbol": "BTC/USDT"}]}, fh)
    monkeypatch.setenv("AITRADER_GAINERS_FILE", fut)
    wallet.set_market(wallet.market_path("data/learn/paper_state.json"), True)
    assert c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-02").json()["candles"] == []  # perp: none yet
    make_store(tmp_path / "x_futures.db").close()  # Futures AI's own database: the perpetual's candles
    ids = {s["id"] for s in c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03").json()["strategies"]}
    assert "short-up" in ids


def test_replay_still_shows_the_strategies_when_a_pick_kept_none(client):
    """Shawn: Futures 15m showed no strategies after a pick that kept none of them."""
    from aitrader.learn import picks

    c, _ = client
    c.post("/login", data={"user": "admin", "password": PW})
    path = picks.picks_path("data/learn/registry.json")
    picks.save(path, {"1h": {"at": 1, "picked": [], "results": []}})
    r = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&months=1").json()
    assert r["none_kept"] and not r["picked"] and r["strategies"][0]["name"] == "up_bar" and r["trades"]
    picks.save(path, {"1h": {"at": 2, "picked": ["other"], "results": []}})
    r = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&months=1").json()
    assert r["picked"] and not r["none_kept"] and r["strategies"] == []  # a pick that kept others: only those


def test_replay_offers_win_only_and_three_years(client, monkeypatch):
    """Shawn: Length up to 3 years, and a Strategy choice that replays only the kept winners."""
    import aitrader.web.replay_pages as rp

    c, runner = client
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    win = Entry(id="win1h-up", kind="rule", name="WIN 1h up_bar", spec=dict(validate_spec(EVERY_UP_BAR), pick="1h"))
    monkeypatch.setattr(rp, "known_entries", lambda *a, **k: [up, win])
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/replay?tf=4h").text
    assert '<option value="36">3 years</option>' in page and '<option value="win">WIN strategies only' in page
    assert 'id="tf-pick" data-tf="4h"' in page  # the winners picker follows the chosen timeframe
    r = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&months=1&strategy=win").json()
    assert r["win_only"] and [s["id"] for s in r["strategies"]] == ["win1h-up"]
    r = c.get("/api/replay?symbol=BTC/USDT&tf=4h&month=2025-03&months=1&strategy=win").json()
    assert r["win_only"] and r["strategies"] == []  # no 4h winners yet: none, not every strategy
    r = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&months=36").json()
    assert not r["capped"] and r["end"] > START + 365 * 24 * HOUR  # 3 years of 1h fit
    r = c.get("/api/replay?symbol=BTC/USDT&tf=5m&month=2025-03&months=36").json()
    assert r["capped"] and r["end"] == START + rp.MAX_BARS * 300_000
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    c.post("/replay/download", data={"csrf": csrf, "symbol": "ETH/USDT", "tf": "5m", "month": "2025-03",
                                     "months": "36"}, follow_redirects=False)
    job = [j for j in runner.list() if j["meta"].get("kind") == "download"][0]
    assert job["steps"][0][4] == "2025-06-02"  # only what Replay can show of 3 years of 5m


def test_replay_all_pairs_totals_and_downloads_the_missing(client):
    # Shawn: "Backtesting will be done on all the pairs ... once it selects all pairs"
    c, runner = client
    assert c.get("/api/replay/all?tf=1h&month=2025-03").status_code == 401
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/replay?symbol=*").text
    assert '<option value="*" selected>All 2 pairs (totals)</option>' in page and 'id="replay-all"' in page
    assert 'id="replay-download-all"' in page and "loadAll" in c.get("/static/replay.js").text
    r = c.get("/api/replay/all?tf=1h&month=2025-03&months=1").json()
    one = c.get("/api/replay?symbol=BTC/USDT&tf=1h&month=2025-03&months=1").json()
    closed = [t for t in one["trades"] if t.get("exit_ts")]
    btc, eth = r["pairs"]
    assert btc["symbol"] == "BTC/USDT" and btc["candles"] == 31 * 24 and btc["trades"] == len(closed) > 0
    assert eth == {**eth, "symbol": "ETH/USDT", "candles": 0, "trades": 0}
    assert r["missing"] == ["ETH/USDT"] and r["later"] == []
    s = r["strategies"][0]
    assert s["name"] == "up_bar" and s["pairs"] == 1 and s["trades"] == len(closed)
    assert s["total_r"] == round(sum(t["r"] for t in closed), 2)
    assert c.get("/api/replay/all?tf=1h&month=2999-01").status_code == 400
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/replay/download-all", data={"csrf": "x"}).status_code == 403
    d = c.post("/replay/download-all", data={"csrf": csrf, "tf": "1h", "month": "2025-03", "months": "1"},
               follow_redirects=False)
    assert d.status_code == 303 and d.headers["location"].startswith("/replay?symbol=*&tf=1h&month=2025-03")
    job = [j for j in runner.list() if j["meta"].get("kind") == "download"][0]
    assert job["steps"] == [["download", "--since", "2025-02-16", "--until", "2025-04-01", "--market", "spot",
                             "--exchange", "bybit", "--timeframe", "1h", "--symbol", "BTC/USDT",
                             "--symbol", "ETH/USDT"]]


def test_replay_all_pairs_says_which_coins_were_listed_after_the_period(client):
    # Shawn's Futures 30m: 5 pairs had no prices for Oct 2025 even after the download
    from aitrader.models import Candle

    c, _runner = client
    c.post("/login", data={"user": "admin", "password": PW})
    jun = int(datetime(2025, 6, 10, tzinfo=timezone.utc).timestamp() * 1000)
    st = CandleStore("x.db")  # the client fixture runs in tmp_path
    st.upsert([Candle("bybit", "ETH/USDT", "1h", jun, 1, 2, 0.5, 1.5, 1)])
    st.close()
    r = c.get("/api/replay/all?tf=1h&month=2025-03&months=1").json()
    assert r["missing"] == [] and r["later"] == ["ETH/USDT"]
    eth = [p for p in r["pairs"] if p["symbol"] == "ETH/USDT"][0]
    assert eth["first"] == jun and eth["first_text"].startswith("2025-06-10")
    assert "listed later" in c.get("/static/replay.js").text
