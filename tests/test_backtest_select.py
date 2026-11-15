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
