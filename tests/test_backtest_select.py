"""The Backtest page: pick strategies, pairs and a period, run ``learn backtest-select``, see the result."""

import json
import os

from aitrader.data.store import CandleStore
from aitrader.learn import Entry, Registry, select, validate_spec
from aitrader.learn.registry import short_twin

from test_learn import EVERY_UP_BAR, fill_store
from test_nexus import _web
from test_strategies import synthetic

H = 3_600_000
DAY = 86_400_000


def _entries(reg_path: str):
    reg = Registry(reg_path)
    up = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), source="t")
    reg.add(up)
    up.set_status("paper", "test")
    other = Entry(id="other", kind="rule", name="other", spec=validate_spec({**EVERY_UP_BAR, "name": "other"}), source="t")
    reg.add(other)
    sh = short_twin(up)
    reg.add(sh)
    sh.set_status("paper", "test")
    reg.save()
    return reg, up, other, sh


def test_only_trades_opening_inside_the_period_count(tmp_path):
    df = synthetic(n=3000)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    fill_store(store, synthetic(n=3000, seed=9), sym="ETH/USDT")
    reg, up, _o, _s = _entries(str(tmp_path / "reg.json"))
    end = int(df.ts.iloc[-1]) + H
    start = end - 30 * DAY
    rep = select.run_selected([up], store, "bybit", ["BTC/USDT", "ETH/USDT", "SOL/USDT"], start, end,
                              select.BacktestConfig())
    row = rep["strategies"][0]
    assert row["trades"] > 0 and rep["total"]["trades"] == row["trades"]
    assert set(row["by_pair"]) <= {"BTC/USDT 1h", "ETH/USDT 1h"} and "SOL/USDT 1h" in row["no_data"]
    assert sum(v["trades"] for v in row["by_pair"].values()) == row["trades"]
    whole = select.run_selected([up], store, "bybit", ["BTC/USDT", "ETH/USDT"], 0, end, select.BacktestConfig())
    assert whole["strategies"][0]["trades"] > row["trades"]  # a longer period, more trades
    assert "ALL TOGETHER" in select.text({**rep, "engine": "Spot AI"})
    store.close()


def test_the_period_is_the_dates_given_or_the_last_months():
    now = 1_000 * DAY
    assert select.period(6, now_ms=now) == (now - int(6 * 30.44 * DAY), now)
    s, e = select.period(6, "2026-05-01", "2026-05-31")
    assert e - s == 31 * DAY
    import pytest

    with pytest.raises(ValueError):
        select.period(0, "2026-06-01", "2026-05-01")


def test_the_command_saves_a_report_and_never_moves_a_strategy(tmp_path, monkeypatch):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AITRADER_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("AITRADER_SYMBOLS", "BTC/USDT")
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=3000)
    fill_store(store, df)
    store.close()
    _entries(str(tmp_path / "reg.json"))
    out = tmp_path / "r.json"
    end = (int(df.ts.iloc[-1]) + H) // 1000
    import time as _t

    day = _t.strftime("%Y-%m-%d", _t.gmtime(end))
    assert main(["learn", "--registry", str(tmp_path / "reg.json"), "backtest-select", "--ids", "up,nope,other",
                 "--start", "1970-03-01", "--end", day, "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert [r["id"] for r in rep["strategies"]] == ["up", "other"] and rep["market"] == "spot"
    assert rep["strategies"][0]["trades"] > 0
    reg = Registry(str(tmp_path / "reg.json"))
    assert reg.get("other").status != "paper" and reg.get("up").status == "paper"  # nothing moved
    assert not reg.get("other").backtest  # its own test result untouched


def test_the_page_lists_the_engines_strategies_and_queues_a_backtest(tmp_path, monkeypatch):
    client, csrf, paths, submitted = _web(tmp_path, monkeypatch)
    _entries(paths.registry)
    page = client.get("/backtest").text
    assert "Backtest: Spot AI" in page and 'href="/backtest"' in page
    assert 'value="up"' in page and 'value="other"' in page
    assert f'value="{short_twin(Entry(id="up", kind="rule", name="x")).id}"' not in page  # Spot AI never shorts
    assert page.index('value="up"') < page.index('value="other"')  # paper trading first
    assert client.post("/backtest/run", data={"csrf": "x", "ids": "up"}).status_code == 403
    r = client.post("/backtest/run", data={"csrf": csrf, "pairs": "BTC/USDT"}, follow_redirects=True)
    assert "Tick at least one strategy" in r.text and not submitted
    r = client.post("/backtest/run", data={"csrf": csrf, "ids": ["up", "ghost"], "pairs": "NOPE/USDT"},
                    follow_redirects=True)
    assert "Tick at least one pair" in r.text and not submitted
    r = client.post("/backtest/run", data={"csrf": csrf, "ids": "up", "pairs": "BTC/USDT", "start": "May 1"},
                    follow_redirects=True)
    assert "Dates must look like" in r.text and not submitted
    client.post("/backtest/run", data={"csrf": csrf, "ids": ["up", "ghost"], "pairs": ["BTC/USDT", "ETH/USDT"],
                                       "months": "12"})
    steps, meta = submitted[-1]
    assert meta == {"kind": "backtest", "market": "spot"}
    step = steps[0]
    assert step[:8] == ["learn", "backtest-select", "--market", "spot", "--ids", "up", "--pairs", "BTC/USDT,ETH/USDT"]
    assert step[8:10] == ["--months", "12"] and step[10] == "--out" and step[11].endswith(".json")
    client.post("/backtest/run", data={"csrf": csrf, "all_paper": "1", "pairs": "BTC/USDT", "months": "7",
                                       "start": "2026-05-01", "end": "2026-05-31"})
    step = submitted[-1][0][0]
    assert step[5] == "up" and step[8:10] == ["--months", "6"]  # an unknown choice falls back to 6
    assert step[-4:] == ["--start", "2026-05-01", "--end", "2026-05-31"]


def test_the_page_shows_the_newest_result_and_ignores_odd_run_names(tmp_path, monkeypatch):
    client, _csrf, paths, _sub = _web(tmp_path, monkeypatch)
    _entries(paths.registry)
    runs = os.path.join(os.path.dirname(paths.report), "selected")
    os.makedirs(runs, exist_ok=True)
    m = {"trades": 40, "win_rate": 0.45, "expectancy_r": 0.12, "total_r": 4.8, "profit_factor": 1.3,
         "max_drawdown_r": 3.0, "t_stat": 1.1}
    rep = {"engine": "Spot AI", "market": "spot", "made": 0, "start": 0, "end": 30 * DAY, "pairs": ["BTC/USDT"],
           "strategies": [{"id": "up", "status": "paper", "timeframes": ["1h"], **m,
                           "by_pair": {"BTC/USDT 1h": m}, "no_data": []},
                          {"id": "bad", "status": "paper", "error": "KeyError: x"}],
           "total": m}
    select.save(rep, os.path.join(runs, "spot-20261115-100000.json"))
    page = client.get("/backtest").text
    assert "Result: Spot AI" in page and "+0.120R" in page and "All together" in page and "could not run" in page
    assert "by pair" in page
    assert "Result:" not in client.get("/backtest?run=../../reg.json").text
    assert "Result: Spot AI" in client.get("/backtest?run=spot-20261115-100000.json").text


def test_futures_view_tests_on_the_perpetuals_with_shorts(tmp_path, monkeypatch):
    from aitrader.data.perp import futures_db_path
    from aitrader.learn import wallet

    client, csrf, paths, submitted = _web(tmp_path, monkeypatch)
    _r, up, _o, sh = _entries(paths.registry)
    wallet.set_market(wallet.market_path(paths.paper_state), True)
    fstore = CandleStore(futures_db_path(str(tmp_path / "c.db")))
    fill_store(fstore, synthetic(n=400), sym="ZEC/USDT")
    fstore.close()
    page = client.get("/backtest").text
    assert "Backtest: Futures AI" in page and f'value="{sh.id}"' in page and 'value="ZEC/USDT"' in page
    client.post("/backtest/run", data={"csrf": csrf, "ids": [sh.id], "pairs": ["ZEC/USDT", "BTC/USDT"]})
    step = submitted[-1][0][0]
    assert step[3] == "futures" and step[5] == sh.id and step[7] == "ZEC/USDT"  # only pairs it has candles for
    assert 'href="/backtest?ids=' in client.get(f"/arena?ids={up.id}").text


def _report(market="spot"):
    m = {"trades": 12, "win_rate": 0.5, "expectancy_r": 0.2, "total_r": 2.4, "profit_factor": 1.5,
         "max_drawdown_r": 2.0, "t_stat": 0.8}
    return {"engine": "Spot AI", "market": market, "made": 0, "start": 0, "end": 30 * DAY, "pairs": ["BTC/USDT"],
            "strategies": [{"id": "up", "status": "paper", "timeframes": ["1h"], **m,
                            "by_pair": {"BTC/USDT 1h": m, "ETH/USDT 1h": m}, "no_data": []},
                           {"id": "other", "status": "candidate", "timeframes": ["4h"], **m,
                            "by_pair": {"BTC/USDT 4h": m}, "no_data": []}],
            "total": m}


def _one_h_pick(reg_path, market="spot"):
    from aitrader.learn import picks

    path = picks.picks_path(reg_path, market)
    picks.save(path, {"1h": {"at": 1, "market": market, "picked": ["kept"],
                             "results": [{"id": "kept", "name": "kept", "trades": 40, "win_rate": 50.0,
                                          "total_r": 5.0, "quarters_up": 2, "quarters_down": 1}]}})
    return path


def test_adding_backtested_strategies_to_win_keeps_the_others_and_needs_a_list(tmp_path):
    from aitrader.learn import picks

    reg, up, other, sh = _entries(str(tmp_path / "reg.json"))
    path = _one_h_pick(str(tmp_path / "reg.json"))
    out = picks.add_backtested(reg, _report(), ["up", "other", "nope"], path, "spot", by="admin")
    assert out["added"] == ["up 1h"]
    assert any(r.startswith("other 4h: no WIN strategies on 4h") for r in out["refused"])
    assert any(r.startswith("nope:") for r in out["refused"])
    reg2 = Registry(str(tmp_path / "reg.json"))
    c = reg2.get("win1h-up")
    assert c.status == "paper" and c.spec["pick"] == "1h" and "from a backtest" in c.history[-1]["why"]
    cur = picks.load(path)["1h"]
    assert cur["picked"] == ["kept", "up"]  # the WIN strategy already there stays
    row = next(r for r in cur["results"] if r["id"] == "up")
    assert row["trades"] == 24 and row["total_r"] == 4.8 and row["from_backtest"] and row["added_by"] == "admin"
    assert picks.why_not(row) == "only 24 trades (needs 30)"  # the Trading page's tick list shows why
    assert "4h" not in picks.load(path)  # no list was started
    # a copy can't be added again, and Spot AI never takes a short
    assert picks.can_add(reg2.get("win1h-up"), "1h", picks.load(path), "spot") == "already a WIN copy or a twin"
    assert "not a strategy this engine trades" == picks.can_add(sh, "1h", picks.load(path), "spot")
    again = picks.add_backtested(reg2, _report(), ["up"], path, "spot")
    assert again["added"] == ["up 1h"] and picks.load(path)["1h"]["picked"] == ["kept", "up"]


def test_the_result_adds_ticked_strategies_to_win_as_a_job(tmp_path, monkeypatch):
    client, csrf, paths, submitted = _web(tmp_path, monkeypatch)
    _entries(paths.registry)
    runs = os.path.join(os.path.dirname(paths.report), "selected")
    os.makedirs(runs, exist_ok=True)
    run = os.path.join(runs, "spot-20261115-100000.json")
    select.save(_report(), run)
    page = client.get("/backtest").text
    assert "Add the ticked ones to the WIN strategies" in page and "no WIN strategies on any timeframe yet" in page
    data = {"csrf": csrf, "run": "spot-20261115-100000.json", "engine": "spot", "ids": ["up", "other"]}
    r = client.post("/backtest/add-win", data=data, follow_redirects=True)
    assert "Nothing added" in r.text and "no WIN strategies on 1h yet" in r.text and not submitted
    _one_h_pick(paths.registry)
    assert "has WIN strategies on 1h" in client.get("/backtest").text
    assert client.post("/backtest/add-win", data={**data, "csrf": "x"}).status_code == 403
    r = client.post("/backtest/add-win", data=data, follow_redirects=True)
    assert "Adding to Spot AI" in r.text and "up 1h" in r.text and "Not added: other 4h" in r.text
    steps, meta = submitted[-1]
    assert meta["kind"] == "pick" and meta["market"] == "spot"
    assert steps[0][:3] == ["learn", "win-add", "--report"] and steps[0][3] == run
    assert steps[0][4:8] == ["--ids", "up,other", "--market", "spot"]
    r = client.post("/backtest/add-win", data={**data, "run": "../../reg.json"}, follow_redirects=True)
    assert "one of Spot AI" in r.text and len(submitted) == 1


def test_the_win_add_command_adds_from_a_report(tmp_path, monkeypatch):
    from aitrader.cli import main
    from aitrader.learn import picks

    monkeypatch.chdir(tmp_path)
    reg_path = str(tmp_path / "reg.json")
    _entries(reg_path)
    path = _one_h_pick(reg_path, "futures")
    rep = tmp_path / "rep.json"
    select.save(_report("futures"), str(rep))
    assert main(["learn", "--registry", reg_path, "win-add", "--report", str(rep), "--ids", "up",
                 "--market", "spot"]) == 1  # a Futures report never feeds Spot AI
    assert main(["learn", "--registry", reg_path, "win-add", "--report", str(rep), "--ids", "up",
                 "--market", "futures", "--by", "admin"]) == 0
    assert Registry(reg_path).get("fwin1h-up").status == "paper"
    assert picks.load(path)["1h"]["picked"] == ["kept", "up"]


def test_the_report_keeps_the_trades_and_says_where_the_candles_start(tmp_path):
    df = synthetic(n=3000)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    fill_store(store, synthetic(n=400, seed=9), sym="ETH/USDT")  # only the newest 400 hours stored
    reg, up, _o, _s = _entries(str(tmp_path / "reg.json"))
    end = int(df.ts.iloc[-1]) + H
    rep = select.run_selected([up], store, "bybit", ["BTC/USDT", "ETH/USDT"], end - 60 * DAY, end,
                              select.BacktestConfig())
    store.close()
    row = rep["strategies"][0]
    assert len(row["trade_list"]) == row["trades"] and row["trade_list"][0]["side"] == "long"
    assert [t["exit_ts"] for t in row["trade_list"]] == sorted(t["exit_ts"] for t in row["trade_list"])
    assert "BTC/USDT 1h" not in row["tested_from"]  # its candles start long before the period
    late = row["tested_from"].get("ETH/USDT 1h")
    assert late is None or late > end - 60 * DAY
    pts = select.curve(row["trade_list"])
    assert abs(pts[-1][1] - row["total_r"]) < 0.01
    csv_text = select.trades_csv(rep)
    assert csv_text.splitlines()[0].startswith("strategy,pair,timeframe,side,opened_utc")
    assert len(csv_text.strip().splitlines()) == row["trades"] + 1
    assert "ALL TOGETHER" in select.summary_csv(rep) and "BTC/USDT 1h" in select.summary_csv(rep)


def test_the_result_shows_a_chart_the_trades_and_downloads(tmp_path, monkeypatch):
    from aitrader.web import backtest_chart

    client, _csrf, paths, _sub = _web(tmp_path, monkeypatch)
    _entries(paths.registry)
    runs = os.path.join(os.path.dirname(paths.report), "selected")
    os.makedirs(runs, exist_ok=True)
    t = {"symbol": "BTC/USDT", "timeframe": "1h", "side": "long", "entry": 100.0, "exit": 102.0, "stop": 99.0,
         "target": 102.0, "exit_reason": "target"}
    trades = [{**t, "entry_ts": 2 * DAY, "exit_ts": 3 * DAY, "r": 2.0},
              {**t, "entry_ts": 5 * DAY, "exit_ts": 6 * DAY, "r": -1.0, "exit_reason": "stop"}]
    rep = {**_report(), "strategies": [{**_report()["strategies"][0], "trade_list": trades,
                                        "tested_from": {"ETH/USDT 1h": 10 * DAY}}]}
    select.save(rep, os.path.join(runs, "spot-20261115-100000.json"))
    page = client.get("/backtest").text
    assert "Running total in R" in page and "<svg" in page and "up: +1.0R over 2 trades" in page
    assert "Not the whole period" in page and "1970-01-11" in page
    assert "Every trade" in page and "target" in page and "-1.00R" in page
    assert "what=trades.csv" in page and "what=chart.svg" in page
    r = client.get("/backtest/download?run=spot-20261115-100000.json&what=trades.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.text.splitlines()[1].startswith("up,BTC/USDT,1h,long,1970-01-03 00:00")
    svg = client.get("/backtest/download?run=spot-20261115-100000.json&what=chart.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml") and 'fill="#ffffff"' in svg.text
    assert "var(--" not in svg.text  # a file on its own: fixed colours
    assert client.get("/backtest/download?run=spot-20261115-100000.json&what=summary.csv").text.startswith("strategy,pair")
    assert json.loads(client.get("/backtest/download?run=spot-20261115-100000.json&what=report.json").text)["market"] == "spot"
    assert client.get("/backtest/download?run=../../reg.json&what=report.json").status_code == 404
    assert client.get("/backtest/download?run=spot-20261115-100000.json&what=x").status_code == 404
    assert backtest_chart.svg({**rep, "strategies": []}) == ""


def test_win_strategies_added_from_a_backtest_can_be_removed(tmp_path, monkeypatch):
    from aitrader.cli import main
    from aitrader.learn import picks

    client, csrf, paths, submitted = _web(tmp_path, monkeypatch)
    reg, *_ = _entries(paths.registry)
    path = _one_h_pick(paths.registry)
    assert "you added from a backtest" not in client.get("/backtest").text
    picks.add_backtested(reg, _report(), ["up"], path, "spot", by="admin")
    page = client.get("/backtest").text
    assert "Spot AI WIN strategies you added from a backtest (1)" in page and 'value="up:1h"' in page
    assert 'value="kept:1h"' not in page  # only ones added from a backtest can be removed here
    r = client.post("/backtest/remove-win", data={"csrf": csrf, "engine": "spot", "items": ["kept:1h"]},
                    follow_redirects=True)
    assert "Tick at least one" in r.text and not submitted
    r = client.post("/backtest/remove-win", data={"csrf": csrf, "engine": "spot", "items": ["up:1h", "kept:1h"]},
                    follow_redirects=True)
    assert "Removing from Spot AI" in r.text
    steps, meta = submitted[-1]
    assert steps[0] == ["learn", "win-remove", "--items", "up:1h", "--market", "spot", "--by", "admin"]
    assert meta["kind"] == "pick"
    monkeypatch.chdir(tmp_path)
    assert main(["learn", "--registry", paths.registry, "win-remove", "--items", "up:1h,kept:1h"]) == 0
    cur = picks.load(path)["1h"]
    assert cur["picked"] == ["kept"] and [r["id"] for r in cur["results"]] == ["kept"]
    assert Registry(paths.registry).get("win1h-up").status == "retired"
    assert "you added from a backtest" not in client.get("/backtest").text
