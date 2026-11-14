"""Strategy factory, evolution, luck correction and stress test (NEXUS upgrades)."""

import random

import pytest

from aitrader.backtest import BacktestConfig
from aitrader.backtest.engine import Trade
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, Gate, Registry, evaluate, luck, validate_spec
from aitrader.learn import evolve, factory
from aitrader.learn.registry import short_twin
from aitrader.learn.stress import harsh, stress_test

from test_learn import EVERY_UP_BAR, LAX, fill_store
from test_strategies import synthetic


def test_luck_bar_rises_with_families_and_never_drops_below_two():
    assert luck.required_t(0) == luck.required_t(1) == 2.0
    assert luck.required_t(10) == pytest.approx(2.576, abs=1e-3)
    assert luck.required_t(1000) == pytest.approx(3.891, abs=1e-3)
    ts = [luck.required_t(n) for n in (1, 10, 100, 1000, 10000)]
    assert ts == sorted(ts)


def test_factory_writes_valid_rule_strategies_with_lineage():
    reg = Registry("unused/registry.json")
    made = factory.generate(reg, 40, seed=5, timeframes=["1h", "4h"])
    assert len(made) == 40 and all(e.status == "research" and e.kind == "rule" for e in made)
    for e in made:
        validate_spec(e.spec)  # the safe rule language accepts every one
        lin = e.spec["lineage"]
        assert lin["generation"] == 0 and lin["parents"] == [] and lin["family"]
        assert e.spec["timeframes"][0] in ("1h", "4h")
        assert e.spec["stop"]["type"] in ("atr", "lowest_low")
        assert lin["trials"] == luck.families(reg.entries.values())
    # the same seed repeats; a second run adds only new ones
    again = factory.generate(Registry("unused/r2.json"), 40, seed=5, timeframes=["1h", "4h"])
    assert [e.id for e in again] == [e.id for e in made]
    more = factory.generate(reg, 10, seed=6)
    assert not {e.id for e in more} & {e.id for e in made}


def test_factory_caps_copies_of_one_family():
    reg = Registry("unused/registry.json")
    factory.generate(reg, 300, seed=1, timeframes=["1h"], max_per_family=1)
    fams = [luck.family_of(e.spec) for e in reg.entries.values()]
    assert len(fams) == len(set(fams))


def test_short_twins_count_as_their_own_family():
    reg = Registry("unused/registry.json")
    made = factory.generate(reg, 5, seed=2, shorts=True)
    shorts = [e for e in made if e.kind == "short"]
    assert len(shorts) == 5
    for s in shorts:
        assert luck.is_machine_made(s.spec) and luck.family_of(s.spec).endswith(":short")
    assert luck.families(reg.entries.values()) == 10


def test_mutation_and_crossover_always_give_valid_strategies():
    reg = Registry("unused/registry.json")
    pool = factory.generate(reg, 60, seed=3)
    rng = random.Random(9)
    for _ in range(400):
        a, b = (rng.choice(pool).spec["lineage"]["genome"] for _ in range(2))
        for g in (evolve.mutate(rng, a), evolve.crossover(rng, a, b)):
            spec = factory.to_spec(g, {"generation": 1})
            assert spec["entry"] and spec["lineage"]["family"] == factory.family(g)


def _tested(e: Entry, ins: float, oos: float, n: int = 100) -> Entry:
    e.backtest = {"in_sample": {"trades": n, "expectancy_r": ins},
                  "out_of_sample": {"trades": n, "expectancy_r": oos}}
    return e


def test_evolution_picks_parents_on_in_sample_results_only():
    reg = Registry("unused/registry.json")
    a, b, c, d = factory.generate(reg, 4, seed=4)
    _tested(a, 0.30, -0.5)
    _tested(b, 0.20, -0.5)
    _tested(c, -0.10, 2.0)  # great out of sample, but that must not make it a parent
    _tested(d, 0.50, 0.1, n=10)  # too few trades to judge
    ids = [e.id for e, _f in evolve.parents(reg)]
    assert ids == [a.id, b.id]
    kids = evolve.breed(reg, 12, seed=1)
    assert kids and all(k.status == "research" for k in kids)
    for k in kids:
        lin = k.spec["lineage"]
        assert lin["generation"] == 1 and set(lin["parents"]) <= {a.id, b.id}
        assert lin["op"] in ("mutation", "crossover")
    tree = evolve.family_tree(reg, kids[0].id)
    assert kids[0].id in tree[0] and len(tree) >= 2
    assert evolve.breed(Registry("unused/empty.json"), 5) == []  # nothing to breed from


def _trade(r, sym="BTC/USDT", tf="1h", reason="stop", ts=0):
    return Trade(strategy="s", symbol=sym, timeframe=tf, signal_ts=ts, entry_ts=ts, entry=100, stop=99,
                 target=None, exit_ts=ts, exit=100, exit_reason=reason, r=r)


def test_stress_scenarios_catch_a_strategy_carried_by_one_pump():
    trades = [_trade(-1.0, ts=i) for i in range(40)] + [_trade(0.9, reason="target", ts=100 + i) for i in range(40)]
    trades.append(_trade(30.0, sym="PUMP/USDT", reason="target", ts=999))  # one lucky pump
    res = stress_test([], trades, BacktestConfig(), Gate(), {"losing_streak_p95": 6})
    sc = res["scenarios"]
    assert not sc["no_best_trades"]["passed"] and not sc["no_best_market"]["passed"]
    assert sc["stop_gaps"]["passed"]  # 10 of 40 stops gap 0.5R further: 21R left over 81 trades
    assert sc["stop_gaps"]["expectancy_r"] == pytest.approx(21 / 81, abs=1e-3)
    assert sc["losing_streak"]["passed"]
    assert not res["passed"] and any("best" in w for w in res["failed"])
    long_streak = stress_test([], trades, BacktestConfig(), Gate(), {"losing_streak_p95": 20})
    assert not long_streak["scenarios"]["losing_streak"]["passed"]


def test_harsh_costs_multiply_every_cost():
    c = harsh(BacktestConfig(), 2, 3, 3)
    base = BacktestConfig()
    assert c.taker_fee == 2 * base.taker_fee and c.perp_taker_fee == 2 * base.perp_taker_fee
    assert c.slippage_bps == 3 * base.slippage_bps and c.perp_funding_8h == 3 * base.perp_funding_8h
    assert c.max_cost_r == 0  # the same trades run; only what they pay changes


def _machine_entry() -> Entry:
    spec = validate_spec(EVERY_UP_BAR)
    spec["lineage"] = {"family": "test-family", "generation": 0, "parents": [], "op": "random", "trials": 1}
    return Entry(id="m", kind="rule", name="up_bar", spec=spec)


def test_gate_raises_the_luck_bar_for_machine_made_strategies_only(tmp_path):
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, synthetic())
    lax_no_stress = Gate(**{**LAX.__dict__, "stress": False})
    book = Entry(id="b", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    res = evaluate(book, store, ["bybit"], ["BTC/USDT"], gate=Gate(**{**lax_no_stress.__dict__, "machine_trials": 1000}))
    assert book.status == "paper" and "luck" not in res
    m = _machine_entry()
    res = evaluate(m, store, ["bybit"], ["BTC/USDT"], gate=Gate(**{**lax_no_stress.__dict__, "machine_trials": 1000}))
    assert res["luck"]["families_tried"] == 1000 and res["luck"]["min_t_oos"] == pytest.approx(3.891, abs=1e-3)
    assert m.status == "rejected" and "could be luck" in m.history[-1]["why"]


def test_stress_test_runs_before_the_holdout_for_machine_made_strategies(tmp_path):
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, synthetic())
    let_luck_through = {**LAX.__dict__, "luck_alpha": 0.999}
    m = _machine_entry()
    res = evaluate(m, store, ["bybit"], ["BTC/USDT"], gate=Gate(**let_luck_through))
    assert set(res["stress"]["scenarios"]) == {"harsh_costs", "stop_gaps", "no_best_trades", "no_best_market",
                                               "losing_streak"}
    assert res["stress"]["scenarios"]["harsh_costs"]["trades"] == res["out_of_sample"]["trades"]
    assert res["stress"]["scenarios"]["harsh_costs"]["expectancy_r"] < res["out_of_sample"]["expectancy_r"]
    if res["stress"]["failed"]:  # a failed stress test rejects before the holdout is looked at
        assert m.status == "rejected" and not res["holdout"]["evaluated"]
        assert "stress test" in m.history[-1]["why"]
    # with every scenario impossible to fail, it goes on to the holdout as usual
    m2 = _machine_entry()
    easy = Gate(**{**let_luck_through, "stress_cost_mult": 0, "stress_slip_mult": 0, "stress_funding_mult": 0,
                   "stress_gap_extra_r": 0, "stress_top_share": 0, "stress_max_losing_streak": 10_000})
    res2 = evaluate(m2, store, ["bybit"], ["BTC/USDT"], gate=easy)
    assert "stress" in res2 and res2["holdout"]["evaluated"] == res2["stress"]["passed"]


def test_factory_evolve_luck_and_lineage_cli(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["--env-file", "none", "learn", "factory", "--n", "6", "--seed", "1", "--shorts",
                 "--timeframes", "1h"]) == 0
    out = capsys.readouterr().out
    assert "12 new strategies at RESEARCH" in out and "out-of-sample t of at least" in out
    reg = Registry("data/learn/registry.json")
    assert len(reg.entries) == 12
    assert main(["--env-file", "none", "learn", "evolve", "--n", "3"]) == 0
    assert "evolution needs at least 2" in capsys.readouterr().out  # nothing tested yet
    longs = [e for e in reg.entries.values() if e.kind == "rule"]
    for e in longs[:3]:
        _tested(e, 0.2, 0.0)
    reg.save()
    assert main(["--env-file", "none", "learn", "evolve", "--n", "3", "--seed", "2"]) == 0
    assert "3 new strategies at RESEARCH" in capsys.readouterr().out
    assert main(["--env-file", "none", "learn", "luck"]) == 0
    assert "machine-made strategies in" in capsys.readouterr().out
    kid = next(e for e in Registry("data/learn/registry.json").entries.values()
               if e.spec.get("lineage", {}).get("generation") == 1)
    assert main(["--env-file", "none", "learn", "lineage", kid.id]) == 0
    assert "gen 1" in capsys.readouterr().out
    assert main(["--env-file", "none", "learn", "factory", "--timeframes", "1m"]) == 2


def _web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aitrader.config import Settings
    from aitrader.web.app import Paths, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    from test_web import PW, login

    monkeypatch.chdir(tmp_path)
    paths = Paths(registry=str(tmp_path / "reg.json"), events=str(tmp_path / "ev.jsonl"))
    jobs = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "x"))
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda title, steps, meta=None: submitted.append((steps, meta)) or "j1")
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "c.db")), Auth("admin", PW, "k"), jobs, paths))
    token = login(client)
    return client, Auth("admin", PW, "k").csrf(token), paths, submitted


def test_factory_page_queues_generate_and_evolve_jobs(tmp_path, monkeypatch):
    client, csrf, paths, submitted = _web(tmp_path, monkeypatch)
    page = client.get("/factory").text
    assert "Strategy Factory" in page and "Generate strategies" in page and "Needs 2 tested parents" in page
    assert 'href="/evolution"' in page and 'href="/arena"' in page  # in the menu
    assert client.post("/factory/generate", data={"csrf": "x", "n": "5"}).status_code == 403
    client.post("/factory/generate", data={"csrf": csrf, "n": "5", "timeframes": ["1h", "4h"],
                                           "shorts": "1", "test": "1"})
    assert submitted[-1] == ([["learn", "factory", "--n", "5", "--timeframes", "1h,4h", "--shorts"],
                              ["learn", "test"], ["learn", "test", "--market", "futures"]], {"kind": "factory"})
    client.post("/factory/generate", data={"csrf": csrf, "n": "9999", "timeframes": ["1m", "1h"]})
    assert submitted[-1][0] == [["learn", "factory", "--n", "200", "--timeframes", "1h"]]
    r = client.post("/factory/generate", data={"csrf": csrf, "n": "3"}, follow_redirects=True)
    assert "Pick at least one timeframe" in r.text
    client.post("/factory/evolve", data={"csrf": csrf, "n": "4", "crossover": "0.3"})
    assert submitted[-1][0] == [["learn", "evolve", "--n", "4", "--crossover", "0.3"]]


def test_evolution_lab_and_arena_show_real_registry_numbers(tmp_path, monkeypatch):
    client, _csrf, paths, _sub = _web(tmp_path, monkeypatch)
    assert "No machine-made strategies yet" in client.get("/evolution").text
    reg = Registry(paths.registry)
    a, b = factory.generate(reg, 2, seed=8)
    _tested(a, 0.3, 0.12)
    _tested(b, 0.2, -0.05)
    a.backtest.update({"luck": {"families_tried": 2, "min_t_oos": 2.24},
                       "stress": {"passed": True, "scenarios": {}, "failed": []},
                       "walk_forward": {"positive": 5, "counted": 6}, "monte_carlo": {"max_drawdown_r_p95": 7.5},
                       "holdout": {"evaluated": True, "trades": 25, "expectancy_r": 0.08}})
    a.set_status("paper", "test")
    b.backtest["failed"] = ["out-of-sample result could be luck (t=1.2, need 2.24)"]
    b.set_status("rejected", "test")
    kid = evolve.breed(reg, 1, seed=3)[0]
    reg.save()
    lab = client.get("/evolution").text
    assert "Generations" in lab and "Survivors" in lab and a.id in lab and "luck bar (t too low)" in lab
    tree = client.get(f"/evolution?id={kid.id}").text
    assert "Family tree" in tree and (a.id in tree or b.id in tree)
    arena = client.get("/arena").text  # default: the best tested by out-of-sample result, best first
    assert arena.index(a.id) < arena.index(b.id) and "+0.120R" in arena and "5/6 windows" in arena
    assert "2.24" in arena and "passed" in arena
    one = client.get(f"/arena?ids={b.id}").text
    assert b.id in one and a.id not in one.split("<table>")[1]
