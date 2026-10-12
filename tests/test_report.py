"""The multi-year backtest report: per strategy, per year, and the Reports page that runs it."""

import json

from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, validate_spec
from aitrader.learn.report import YEAR_MS, backtest_report, report_text, save_report
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

from test_learn import EVERY_UP_BAR, fill_store
from test_strategies import synthetic

PW = "correct horse battery"
HOUR = 3_600_000
N = 3 * 365 * 24  # three years of hourly bars


def store_with_history(tmp_path):
    df = synthetic(n=N)
    start = 1_700_000_000_000 - (N * HOUR)
    df["ts"] = df["ts"] + start
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    return store, int(df["ts"].iloc[-1])


def test_report_covers_every_year_and_ranks_best_first(tmp_path):
    store, last = store_with_history(tmp_path)
    good = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    daily = dict(EVERY_UP_BAR, name="daily_only", timeframes=["1d"])
    no_data = Entry(id="daily", kind="rule", name="daily_only", spec=validate_spec(daily))
    builtin = Entry(id="builtin-trend_pullback", kind="builtin", name="trend_pullback")
    rep = backtest_report([no_data, good, builtin], store, ["bybit"], ["BTC/USDT"], years=5, now_ms=last)
    ids = [s["id"] for s in rep["strategies"]]
    assert set(ids) == {"up", "builtin-trend_pullback"}
    assert rep["strategies"][0]["all"]["total_r"] >= rep["strategies"][1]["all"]["total_r"]
    assert rep["skipped"] == [{"id": "daily", "why": "no price history for its timeframes 1d"}]
    up = rep["strategies"][ids.index("up")]
    assert len(up["by_year"]) >= 3 and sum(y["trades"] for y in up["by_year"].values()) == up["all"]["trades"]
    assert up["last_12_months"]["trades"] < up["all"]["trades"]
    assert up["usdt_at_1pct"] == round(up["all"]["total_r"] * 100, 2)
    assert rep["totals"]["tested"] == 2 and rep["totals"]["trades"] == sum(s["all"]["trades"]
                                                                           for s in rep["strategies"])
    # a 1-year window only sees the last year of prices
    short = backtest_report([good], store, ["bybit"], ["BTC/USDT"], years=1, now_ms=last)
    assert short["data_from"] >= last - YEAR_MS - HOUR
    path = save_report(rep, str(tmp_path / "r" / "backtest.json"))
    assert json.load(open(path))["totals"] == rep["totals"]
    assert "up" in report_text(rep) and "skipped daily" in report_text(rep)
    store.close()


def app_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    paths = Paths(report=str(tmp_path / "reports" / "backtest.json"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db")), Auth("admin", PW, "k"), runner, paths)
    return TestClient(app), runner, paths


def test_reports_page_needs_login(tmp_path, monkeypatch):
    client, _runner, _paths = app_client(tmp_path, monkeypatch)
    assert client.get("/reports", follow_redirects=False).status_code == 303
    assert client.post("/reports/run", data={"years": "5"}, follow_redirects=False).status_code == 303
    assert client.get("/reports/backtest.json", follow_redirects=False).status_code == 303


def test_run_queues_download_then_report_once(tmp_path, monkeypatch):
    client, runner, paths = app_client(tmp_path, monkeypatch)
    client.post("/login", data={"user": "admin", "password": PW})
    page = client.get("/reports").text
    assert "No report yet" in page and 'href="/reports"' in page
    csrf = Auth("admin", PW, "k").csrf(client.cookies.get(COOKIE))
    assert client.post("/reports/run", data={"years": "5", "csrf": "bad"}).status_code == 403
    r = client.post("/reports/run", data={"years": "5", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "Backtest%20started" in r.headers["location"]
    jobs = [j for j in runner.list() if j["meta"].get("kind") == "report"]
    assert len(jobs) == 1
    dl, rep = jobs[0]["steps"]
    assert dl[:2] == ["download", "--since"] and dl[3:5] == ["--exchange", "bybit"]
    assert [dl[i + 1] for i, a in enumerate(dl) if a == "--timeframe"] == ["5m", "15m", "30m", "1h", "4h", "1d"]
    assert rep == ["learn", "report", "--years", "5", "--out", paths.report]
    again = client.post("/reports/run", data={"years": "5", "csrf": csrf}, follow_redirects=False)
    assert "already%20running" in again.headers["location"]
    assert "The backtest is running" in client.get("/reports").text


def test_reports_page_shows_saved_report(tmp_path, monkeypatch):
    client, _runner, paths = app_client(tmp_path, monkeypatch)
    store, last = store_with_history(tmp_path)
    good = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    save_report(backtest_report([good], store, ["bybit"], ["BTC/USDT"], years=5, now_ms=last), paths.report)
    store.close()
    client.post("/login", data={"user": "admin", "password": PW})
    page = client.get("/reports").text
    assert "Strategies tested" in page and "up_bar" in page and "<th>2023</th>" in page
    r = client.get("/reports/backtest.json")
    assert r.status_code == 200 and r.json()["strategies"][0]["id"] == "up"
