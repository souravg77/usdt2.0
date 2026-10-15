"""Timeframe picks: keep only the steady winners on one timeframe; the rest stop trading it."""

from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.learn import Entry, validate_spec
from aitrader.learn import picks
from aitrader.learn.keep import keep_top
from aitrader.learn.registry import Registry, make_strategy, short_twin
from aitrader.learn.replay import replay
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

from test_learn import EVERY_UP_BAR
from test_replay import END, START, make_store

UP = Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))


def result(i, trades=20, total=5.0, up=3, down=1):
    return {"id": i, "name": i, "trades": trades, "win_rate": 50.0, "total_r": total,
            "quarters_up": up, "quarters_down": down, "quarters": {}}


def test_winners_need_a_positive_total_enough_trades_and_steady_quarters():
    rs = [result("good"), result("few", trades=5), result("losing", total=-1.0),
          result("one_lucky_quarter", up=1, down=3), result("even", up=2, down=2)]
    assert [r["id"] for r in picks.winners(rs)] == ["good", "even"]


def test_evaluate_counts_trades_by_quarter(tmp_path):
    store = make_store(tmp_path / "c.db")
    daily = Entry(id="d", kind="rule", name="d", spec=validate_spec(dict(EVERY_UP_BAR, name="d", timeframes=["1d"])))
    res = picks.evaluate([UP, daily], store, "bybit", ["BTC/USDT", "ETH/USDT"], "1h")
    assert [r["id"] for r in res] == ["up"]  # the daily one doesn't trade 1h
    r = res[0]
    assert r["trades"] > 0 and abs(sum(r["quarters"].values()) - r["total_r"]) < 0.05
    assert r["quarters_up"] + r["quarters_down"] == len(r["quarters"])
    store.close()


def test_a_pick_copies_the_winners_and_takes_the_timeframe_from_the_rest(tmp_path):
    reg = Registry(str(tmp_path / "registry.json"))
    other = Entry(id="other", kind="rule", name="other",
                  spec=validate_spec(dict(EVERY_UP_BAR, name="other", timeframes=["1h", "4h"])))
    for e in (UP, other):
        reg.add(e)
        e.set_status("paper", "test")
    short = short_twin(other)
    path = picks.picks_path(reg.path)
    out = picks.apply(reg, [UP, other, short], "4h", [result("other"), result(short.id), result("up", total=-2)],
                      path, info={"pairs": ["BTC/USDT"]})
    assert out["picked"] == ["other", short.id] and out["added"] == ["win4h-other", f"win4h-{short.id}"]
    copy, short_copy = reg.entries["win4h-other"], reg.entries[f"win4h-{short.id}"]
    assert copy.name == "WIN 4h: other" and short_copy.name == f"WIN 4h: {short.name}"
    assert copy.status == "paper" and make_strategy(copy).spec.timeframes == ("4h",)
    assert make_strategy(short_copy).spec.timeframes == ("4h",)
    tf_picks = picks.load(path)
    assert tf_picks["4h"]["picked"] == ["other", short.id] and tf_picks["4h"]["pairs"] == ["BTC/USDT"]
    # the original keeps its 1h trading but not 4h; the copy trades 4h only
    assert picks.allowed(other, ["1h", "4h"], tf_picks) == ["1h"]
    assert picks.allowed(copy, ["4h"], tf_picks) == ["4h"]
    assert picks.allowed(other, ["1h", "4h"], {}) == ["1h", "4h"]
    # keep-top leaves the pick copies alone
    keep_top(reg, 10)
    assert copy.status == "paper"
    # a new pick without "other" stops its copy; undo stops the rest
    again = picks.apply(Registry(reg.path), [UP, other, short], "4h", [result(short.id)], path)
    assert again["dropped"] == ["win4h-other"]
    reg = Registry(reg.path)
    assert picks.clear(reg, "4h", path) == [f"win4h-{short.id}"]
    assert picks.load(path) == {}


def test_replay_on_a_picked_timeframe_shows_only_the_kept_strategies(tmp_path):
    store = make_store(tmp_path / "c.db")
    tf_picks = {"1h": {"picked": []}}
    res = replay([UP], store, "bybit", "BTC/USDT", "1h", START, END, tf_picks=tf_picks)
    assert res["strategies"] == [] and res["trades"] == []
    kept = picks.pick_copy(UP, "1h")
    res = replay([UP, kept], store, "bybit", "BTC/USDT", "1h", START, END, tf_picks=tf_picks)
    assert [s["id"] for s in res["strategies"]] == ["win1h-up"] and res["trades"]
    assert res["strategies"][0]["name"] == "WIN 1h: up_bar"
    store.close()


def test_replay_page_starts_and_undoes_a_pick(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_store(tmp_path / "x.db").close()
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["true"])
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT"]), Auth("admin", "correct horse battery", "k"),
                     runner, Paths(report=str(tmp_path / "none.json"), registry=str(tmp_path / "registry.json")))
    c = TestClient(app)
    c.post("/login", data={"user": "admin", "password": "correct horse battery"})
    assert 'id="tf-pick"' in c.get("/replay").text
    csrf = Auth("admin", "correct horse battery", "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/replay/pick", data={"csrf": "x", "tf": "4h"}).status_code == 403
    r = c.post("/replay/pick", data={"csrf": csrf, "tf": "4h"}, follow_redirects=False)
    assert r.status_code == 303 and "tf=4h" in r.headers["location"]
    runner.wait_idle()
    job = [j for j in runner.list() if j["meta"].get("kind") == "pick"][0]
    assert job["steps"][0][:2] == ["download", "--since"] and job["steps"][0][-2:] == ["--timeframe", "4h"]
    assert job["steps"][1] == ["learn", "pick-timeframe", "4h", "--years", "2"]
    # a saved pick is listed with an undo button
    picks.save(picks.picks_path(str(tmp_path / "registry.json")),
               {"4h": {"at": 1, "picked": ["up"], "pairs": ["BTC/USDT"], "results": [result("up")]}})
    page = c.get("/replay?tf=4h").text
    assert "4h pick on: 1 strategies kept" in page and "Undo the 4h pick" in page
    c.post("/replay/pick", data={"csrf": csrf, "tf": "4h", "undo": "1"})
    runner.wait_idle()
    assert ["learn", "pick-timeframe", "4h", "--clear"] in [j["steps"][0] for j in runner.list()]


def test_cli_pick_timeframe_and_undo(tmp_path, monkeypatch, capsys):
    import time

    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    make_store(tmp_path / "data" / "aitrader.db").close()
    # the synthetic candles are from 2025: test enough years back to reach them
    years = str(round((time.time() * 1000 - START) / (365.25 * 86_400_000) + 1, 1))
    reg = Registry("data/learn/registry.json")
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    args = ["--env-file", "none", "learn", "--symbol", "BTC/USDT", "pick-timeframe", "1h", "--years", years,
            "--min-trades", "1"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "1h: tested" in out
    tf_picks = picks.load("data/learn/timeframe_picks.json")
    assert "1h" in tf_picks and tf_picks["1h"]["results"]
    assert main(["--env-file", "none", "learn", "pick-timeframe", "1h", "--clear"]) == 0
    assert picks.load("data/learn/timeframe_picks.json") == {}


def test_a_kept_builtin_strategy_still_builds_under_its_win_name():
    from aitrader.strategies import STRATEGIES

    name = sorted(STRATEGIES)[0]
    orig = Entry(id=f"builtin-{name}", kind="builtin", name=name)
    c = picks.pick_copy(orig, "4h")
    assert c.name == f"WIN 4h: {name}" and make_strategy(c).spec.timeframes == ("4h",)
    # a copy saved before the name was kept in its spec still builds
    old = Entry(id=c.id, kind="builtin", name=c.name, spec={k: v for k, v in c.spec.items() if k != "builtin"})
    assert make_strategy(old).spec.timeframes == ("4h",)


def test_a_short_twin_of_a_win_copy_is_not_picked_again(tmp_path):
    store = make_store(tmp_path / "c.db")
    copy = picks.pick_copy(UP, "1h")
    twin_of_copy = short_twin(copy)
    assert picks.is_copy(twin_of_copy) and not picks.is_copy(short_twin(UP))
    res = picks.evaluate([UP, copy, twin_of_copy], store, "bybit", ["BTC/USDT"], "1h")
    assert [r["id"] for r in res] == ["up"]
    store.close()
