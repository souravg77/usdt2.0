"""The strategy improver: which settings it may move, the search on older data, the check on
newer data, the full gate, and the Teacher page section."""

import json

import pytest
from fastapi.testclient import TestClient

from aitrader.backtest import BacktestConfig
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, Registry, make_strategy, validate_spec
from aitrader.learn import improve as imp
from aitrader.learn.registry import short_twin

from test_learn import EVERY_UP_BAR, fill_store
from test_strategies import synthetic


def _rule(eid="up", status="paper"):
    return Entry(id=eid, kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status=status)


def test_tunables_leave_safety_settings_alone():
    t = imp.tunables(Entry(id="b", kind="builtin", name="trend_pullback"))
    assert {"fast", "slow", "adx_min", "stop_atr", "target_r"} <= set(t)
    assert not set(t) & imp.FIXED
    assert imp.tunables(_rule()) == {"stop.pct": 0.02, "target.r": 1.5, "exit.time_stop_bars": 12}
    assert imp.tunables(short_twin(Entry(id="b", kind="builtin", name="trend_pullback"))) == t


def test_settings_reach_the_strategy():
    b = imp.with_settings(Entry(id="b", kind="builtin", name="trend_pullback"), {"fast": 15, "target_r": 3.0})
    s = make_strategy(b)
    assert s.params["fast"] == 15 and s.params["target_r"] == 3.0
    r = imp.with_settings(_rule(), {"target.r": 2.5, "stop.pct": 0.03})
    assert make_strategy(r).rule["target"]["r"] == 2.5 and r.spec["stop"]["pct"] == 0.03
    assert _rule().spec["target"]["r"] == 1.5  # the original is untouched
    sh = imp.with_settings(short_twin(Entry(id="b", kind="builtin", name="trend_pullback")), {"fast": 15})
    assert make_strategy(sh).base.params["fast"] == 15


class FakeMarkets:
    """Results from a formula: in sample peaks at target 3R; ``oos`` decides the newer part."""

    store = None

    def __init__(self, oos):
        self.oos, self.calls = oos, 0

    def run(self, entry, part, cfg):
        self.calls += 1
        t = imp.tunables(entry)
        exp = 0.1 - 0.05 * abs(t["target.r"] - 3.0) if part == 0 else self.oos(t)
        return {"trades": 100, "expectancy_r": exp, "win_rate": 0.5, "profit_factor": 1.2, "max_drawdown_r": 5.0}


def test_search_climbs_toward_the_best_setting_within_its_budget():
    m = FakeMarkets(lambda t: 0.0)
    found = imp.search(_rule(), m, BacktestConfig())
    assert found["settings"]["target.r"] > 2.5 and found["best"]["expectancy_r"] > found["start"]["expectancy_r"]
    assert found["tried"] <= imp.MAX_EVALS
    few = imp.search(_rule(), FakeMarkets(lambda t: 0.0), BacktestConfig(), max_evals=3)
    assert few["tried"] <= 3


def test_too_few_trades_never_win():
    assert imp._score({"trades": 5, "expectancy_r": 3.0}) == float("-inf")
    assert imp._score({"trades": 30, "expectancy_r": 0.5}) == pytest.approx(0.5 * 30 / 50)


def test_a_change_that_fails_on_newer_data_is_not_kept():
    reg = Registry("/nonexistent/reg.json")
    e = _rule()
    reg.add(e)
    out = imp.improve_one(e, reg, FakeMarkets(lambda t: 0.05), BacktestConfig(), ["bybit"], ["BTC/USDT"])
    assert "not on the newer data" in out["outcome"] and out["new_id"] is None
    assert out["changes"]["target.r"][0] == 1.5 and e.status == "paper" and len(reg.entries) == 1


def test_an_improvement_must_pass_the_full_gate(monkeypatch):
    oos = lambda t: 0.02 + 0.05 * (t["target.r"] > 2)  # noqa: E731

    def gate(result):
        def fake_evaluate(entry, store, exchanges, symbols, cfg=None, **kw):
            entry.backtest = {"failed": [] if result == "paper" else ["holdout lost money"]}
            entry.set_status(result, "test")
            return entry.backtest
        return fake_evaluate

    reg, e = Registry("/nonexistent/reg.json"), _rule()
    reg.add(e)
    monkeypatch.setattr(imp, "evaluate", gate("rejected"))
    out = imp.improve_one(e, reg, FakeMarkets(oos), BacktestConfig(), ["bybit"], ["BTC/USDT"])
    assert out["outcome"].startswith("failed the full backtest gate") and "holdout" in out["outcome"]
    assert len(reg.entries) == 1 and e.status == "paper"

    monkeypatch.setattr(imp, "evaluate", gate("paper"))
    out = imp.improve_one(e, reg, FakeMarkets(oos), BacktestConfig(), ["bybit"], ["BTC/USDT"])
    new = reg.get(out["new_id"])
    assert out["new_id"].startswith("up~") and new.name == "up_bar (improved)" and new.status == "paper"
    assert new.spec["target"]["r"] > 2 and e.status == "retired" and "replaced by" in e.history[-1]["why"]
    assert out["out_of_sample"]["after"]["expectancy_r"] > out["out_of_sample"]["before"]["expectancy_r"]
    again = imp.improve_one(e, reg, FakeMarkets(oos), BacktestConfig(), ["bybit"], ["BTC/USDT"])
    assert "already found" in again["outcome"]


def test_nothing_to_tune():
    e = Entry(id="x", kind="rule", name="x", spec={"stop": {"type": "expr", "expr": "low"}, "target": None})
    assert "nothing to tune" in imp.improve_one(e, Registry("/nonexistent/r.json"), FakeMarkets(lambda t: 0),
                                                BacktestConfig(), [], [])["outcome"]


def test_improve_runs_on_stored_candles_and_writes_a_report(tmp_path):
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, synthetic(n=1500))
    reg = Registry(str(tmp_path / "reg.json"))
    reg.add(_rule())
    rep = imp.improve(store, reg, [reg.get("up")], ["bybit"], ["BTC/USDT"], max_evals=6,
                      report_path=str(tmp_path / "imp.json"), clock=lambda: 1.0)
    store.close()
    r = rep["results"][0]
    assert r["strategy"] == "up" and 1 < r["tried"] <= 6 and r["outcome"]
    assert r["in_sample"]["before"]["trades"] > 0
    assert json.loads((tmp_path / "imp.json").read_text())["results"][0]["strategy"] == "up"
    assert imp.load_report(str(tmp_path / "missing.json")) == {}


def test_teacher_page_improve_button_and_results(tmp_path, monkeypatch):
    from aitrader.config import Settings
    from aitrader.web.app import Paths, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    from test_web import PW, login

    monkeypatch.chdir(tmp_path)
    paths = Paths(teacher=str(tmp_path / "t"), improve=str(tmp_path / "imp.json"), events=str(tmp_path / "ev.jsonl"))
    jobs = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "x"))
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda title, steps, meta=None: submitted.append((steps, meta)) or "j1")
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "c.db")), Auth("admin", PW, "k"), jobs, paths))
    token = login(client)
    csrf = Auth("admin", PW, "k").csrf(token)
    assert "Improve the paper strategies" in client.get("/teacher").text
    assert client.post("/teacher/improve", data={"csrf": "x"}).status_code == 403
    client.post("/teacher/improve", data={"csrf": csrf})
    assert submitted == [([["learn", "improve", "--report", paths.improve]], {"kind": "improve"})]
    (tmp_path / "imp.json").write_text(json.dumps({"at": 1_760_000_000, "improved": 1, "results": [
        {"strategy": "up", "tried": 12, "changes": {"target.r": [1.5, 2.5]}, "new_id": "up~abc123",
         "outcome": "improved: the new version passed the full gate",
         "out_of_sample": {"before": {"expectancy_r": 0.02, "win_rate": 0.4},
                           "after": {"expectancy_r": 0.09, "win_rate": 0.45}}}]}))
    page = client.get("/teacher").text
    assert "IMPROVED" in page and "target.r 1.5 → 2.5" in page and "up~abc123" in page and "+0.090R" in page
