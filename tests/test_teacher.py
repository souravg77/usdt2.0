"""The backtest teacher: market situations, lessons learned on early signals and checked on later
ones, the 1R profit-take, turning the lessons on, and paper trading following them."""

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from aitrader.ai.features import market_features
from aitrader.backtest import BacktestConfig
from aitrader.backtest.engine import simulate_signal
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, PaperConfig, PaperTrader, Registry, RetireRule, make_strategy, validate_spec
from aitrader.learn.teacher import (Teacher, TeacherFilter, best_for, collect, describe, lessons_from,
                                    one_r_outcome, situation, teach)
from aitrader.models import Side
from aitrader.strategies.base import Signal

from test_learn import EVERY_UP_BAR, fill_store
from test_strategies import synthetic

H = 3_600_000


def test_situation_buckets():
    assert situation({"regime": 1.0, "atr_pctile": 0.1, "rel_volume": 2.0}) == "up|low|heavy"
    assert situation({"regime": -1.0, "atr_pctile": 0.9, "rel_volume": 0.5}) == "down|high|quiet"
    assert situation({"regime": 0.0, "atr_pctile": 0.5, "rel_volume": 1.0}) == "range|normal|normal"
    assert situation({"regime": float("nan"), "atr_pctile": 0.5, "rel_volume": 1.0}) is None
    assert describe("up|low|heavy") == "uptrend, low volatility, heavy volume"


def _bars(path):
    """1h bars whose highs and lows follow ``path`` (open = previous close)."""
    rows, prev = [], path[0]
    for i, (hi, lo, close) in enumerate(path):
        rows.append({"ts": i * H, "open": prev, "high": hi, "low": lo, "close": close, "volume": 1.0})
        prev = close
    return pd.DataFrame(rows)


def _sig(side, entry, stop, target=None):
    return Signal(ts=H, strategy="s", exchange="bybit", symbol="BTC/USDT", timeframe="1h", side=side,
                  entry=entry, stop=stop, target=target)


def test_one_r_profit_take_turns_a_round_trip_into_a_winner():
    cfg = BacktestConfig(slippage_bps=0)
    # long at 100, stop 98: price reaches 102 (1R), then falls through the stop
    df = _bars([(100, 100, 100), (100, 100, 100), (102.5, 99.5, 101), (101, 97, 97.5)])
    sig = _sig(Side.BUY, 100, 98, target=106)
    t = simulate_signal(sig, df, 1, cfg, {})
    assert t.r < -1
    assert one_r_outcome(sig, df, 1, t) == pytest.approx(1 - t.cost_r)
    # the stop first, or both on one bar: no change
    df2 = _bars([(100, 100, 100), (100, 100, 100), (102.5, 97, 98)])
    t2 = simulate_signal(sig, df2, 1, cfg, {})
    assert one_r_outcome(sig, df2, 1, t2) == t2.r
    # a target closer than 1R is kept
    near = _sig(Side.BUY, 100, 98, target=101)
    t3 = simulate_signal(near, df, 1, cfg, {})
    assert one_r_outcome(near, df, 1, t3) == t3.r
    # shorts mirror it
    dfs = _bars([(100, 100, 100), (100, 100, 100), (100.5, 97.5, 99), (103, 99, 102.5)])
    short = _sig(Side.SELL, 100, 102, target=94)
    ts = simulate_signal(short, dfs, 1, cfg, {})
    assert ts.r < -1 and one_r_outcome(short, dfs, 1, ts) == pytest.approx(1 - ts.cost_r)


def _frame(n=200, seed=0):
    """Strategy A wins in uptrends and loses in high-volatility downtrends; strategy B's quick
    round trips would have been winners with a 1R take; C has too few signals to judge."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        up = i % 2 == 0
        r = rng.normal(0.4, 0.3) if up else rng.normal(-0.5, 0.3)
        rows.append({"strategy": "A", "signal_ts": i, "situation": "up|normal|normal" if up else "down|high|heavy",
                     "r": r, "r_1r": r})
        rows.append({"strategy": "B", "signal_ts": i, "situation": "range|low|quiet",
                     "r": -0.3 if i % 3 else 1.5, "r_1r": 0.9 if i % 3 else 0.9})
    rows += [{"strategy": "C", "signal_ts": i, "situation": "up|low|quiet", "r": 2.0, "r_1r": 2.0} for i in range(5)]
    return pd.DataFrame(rows)


def test_lessons_trade_avoid_unproven_and_the_1r_take():
    les = lessons_from(_frame())
    assert les["A"]["up|normal|normal"]["verdict"] == "trade"
    assert les["A"]["down|high|heavy"]["verdict"] == "avoid"
    assert les["C"]["up|low|quiet"]["verdict"] == "unproven"  # 5 lucky trades prove nothing
    assert les["B"]["range|low|quiet"]["exit_1r"] and not les["A"]["up|normal|normal"]["exit_1r"]
    assert les["B"]["range|low|quiet"]["with_1r"]["win_rate"] == 1.0
    assert [b["strategy"] for b in best_for(les, "up|normal|normal")] == ["A"]


def test_teach_learns_early_and_checks_on_later_signals():
    res = teach(_frame(), now=1_760_000_000)
    assert res["learn_signals"] + res["check_signals"] == res["signals"]
    w, f = res["check"]["without"], res["check"]["with"]
    assert res["passed"] and f["expectancy_r"] > w["expectancy_r"] and f["win_rate"] > w["win_rate"]
    assert f["trades"] < w["trades"]  # the avoided signals are gone
    a_up = res["lessons"]["A"]["up|normal|normal"]
    assert a_up["check"]["trades"] > 0 and a_up["check"]["expectancy_r"] > 0
    # a lesson that falls apart later doesn't pass
    flip = _frame()
    late = flip["signal_ts"] >= 140
    flip.loc[late & (flip["strategy"] == "A"), "r"] *= -1
    flip.loc[late & (flip["strategy"] == "A"), "r_1r"] *= -1
    flip.loc[late & (flip["strategy"] == "B"), ["r", "r_1r"]] = [0.2, -1.0]
    bad = teach(flip)
    assert not bad["passed"] and "did not beat" in bad["why"]
    small = teach(_frame(n=20))
    assert not small["passed"] and "fewer than" in small["why"]


def test_turning_on_needs_a_passed_check(tmp_path):
    t = Teacher(str(tmp_path / "t"))
    with pytest.raises(ValueError, match="no lessons"):
        t.turn_on("Shawn")
    t.save_candidate(teach(_frame(n=20)))
    with pytest.raises(ValueError, match="can't be turned on"):
        t.turn_on("Shawn")
    assert not t.active
    good = teach(_frame())
    t.save_candidate(good)
    with pytest.raises(ValueError):
        t.turn_on("")
    t.turn_on("Shawn")
    assert t.active["version"] == good["version"] and t.active["turned_on_by"] == "Shawn"
    t.turn_off("Shawn")
    assert t.active == {}


def _active(tmp_path, lessons):
    t = Teacher(str(tmp_path / "t"))
    t._save("active.json", {"version": "t1", "lessons": lessons})
    return TeacherFilter(str(tmp_path / "t"))


def test_filter_skips_avoid_and_adds_the_1r_take(tmp_path):
    df = synthetic(n=700)
    df["volume"] *= 1e5
    key = situation(market_features(df).iloc[-1].to_dict())
    assert key
    close = float(df["close"].iloc[-1])
    sig = _sig(Side.BUY, close, close * 0.98, target=close * 1.1)
    off = TeacherFilter(str(tmp_path / "none"))
    assert off.check(sig, "A", df) == {"take": True, "signal": sig, "situation": None, "verdict": None,
                                        "version": None}
    lesson = {"verdict": "avoid", "exit_1r": False}
    f = _active(tmp_path / "a", {"A": {key: lesson}})
    out = f.check(sig, "A", df)
    assert not out["take"] and out["situation"] == key and out["verdict"] == "avoid"
    assert f.check(sig, "B", df)["take"]  # nothing learned about B: taken as before
    f = _active(tmp_path / "b", {"A": {key: {"verdict": "trade", "exit_1r": True}}})
    out = f.check(sig, "A", df)
    assert out["take"] and out["signal"].target == pytest.approx(close * 1.02)
    short = _sig(Side.SELL, close, close * 1.02, target=close * 0.9)
    assert f.check(short, "A", df)["signal"].target == pytest.approx(close * 0.98)


def test_collect_tags_every_backtest_signal(tmp_path):
    df = synthetic(n=900)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    strat = make_strategy(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR)))
    said = []
    frame = collect(store, [("up", strat)], ["bybit"], ["BTC/USDT"], progress=said.append)
    store.close()
    assert said == ["[1/1] up (0 signals so far)"]  # the job page shows each strategy as it starts
    assert len(frame) > 20
    assert frame["situation"].map(lambda k: len(k.split("|")) == 3).all()
    assert frame["signal_ts"].max() < df["ts"].iloc[int(len(df) * 0.8)] + H  # the final 20% is never used
    changed = frame[frame["r_1r"] != frame["r"]]
    assert len(changed) and (changed["r_1r"] < 1).all()  # a 1R take, less costs, wherever it fired
    assert not frame[["r", "r_1r"]].map(math.isnan).any().any()


def test_paper_trading_follows_the_lessons(tmp_path):
    df = synthetic(n=800)
    keys = {situation(r) for r in market_features(df).to_dict("records")} - {None}

    def run(root, lessons):
        store = CandleStore(str(root / "c.db"))
        fill_store(store, df)
        reg_path = str(root / "reg.json")
        reg = Registry(reg_path)
        reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
        reg.save()
        tf = _active(root, lessons) if lessons is not None else None
        trader = PaperTrader(store, reg_path, ["bybit"], ["BTC/USDT"],
                             PaperConfig(retire=RetireRule(min_trades=10_000, max_drawdown=1e9)),
                             state_path=str(root / "s.json"), teacher=tf)
        opened = filtered = 0
        for i in range(600, len(df)):
            ev = trader.tick(now_ms=int(df.ts.iloc[i]) + H)
            opened, filtered = opened + len(ev["opened"]), filtered + len(ev["filtered"])
        return trader, opened, filtered

    _, base, _ = run(tmp_path / "a", None)
    _, opened, filtered = run(tmp_path / "b", {"up": {k: {"verdict": "avoid", "exit_1r": False} for k in keys}})
    assert base > 5 and opened == 0 and filtered >= base
    trader, opened, _ = run(tmp_path / "c", {"up": {k: {"verdict": "trade", "exit_1r": True} for k in keys}})
    assert opened >= base  # 1R takes close trades sooner, so more signals find the strategy free
    assert any(t.get("exit_reason") == "target" for t in trader.state["trades"])
    assert all(p.get("teacher") == "t1" for p in trader.state["open"].values())


def test_teacher_page_and_buttons(tmp_path, monkeypatch):
    from aitrader.config import Settings
    from aitrader.web.app import Paths, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    from test_web import PW, login

    monkeypatch.chdir(tmp_path)
    paths = Paths(teacher=str(tmp_path / "t"), events=str(tmp_path / "ev.jsonl"))
    jobs = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "x"))
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda title, steps, meta=None: submitted.append(steps) or "j1")
    app = create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT"]), Auth("admin", PW, "k"),
                     jobs, paths)
    client = TestClient(app)
    assert client.get("/teacher", follow_redirects=False).headers["location"] == "/login"
    token = login(client)
    csrf = Auth("admin", PW, "k").csrf(token)
    page = client.get("/teacher").text
    assert "OFF" in page and "Teach from the backtests" in page and "never" in page
    r = client.post("/teacher/teach", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and submitted == [[["learn", "teach", "--dir", str(tmp_path / "t"), "--market", "spot"]]]
    assert client.post("/teacher/on", data={"csrf": "bad"}).status_code == 403
    Teacher(paths.teacher).save_candidate(teach(_frame(n=20)))
    r = client.post("/teacher/on", data={"csrf": csrf}, follow_redirects=True)
    assert "Not turned on" in r.text and not Teacher(paths.teacher).active
    Teacher(paths.teacher).save_candidate(teach(_frame()))
    page = client.get("/teacher").text
    assert "passed the check" in page and "Turn on" in page and "Best lessons" in page
    client.post("/teacher/on", data={"csrf": csrf})
    assert Teacher(paths.teacher).active["turned_on_by"] == "admin"
    assert "<b class=\"good\">ON</b>" in client.get("/teacher").text
    client.post("/teacher/off", data={"csrf": csrf})
    assert not Teacher(paths.teacher).active

    # Futures AI has its own lessons, in <teacher>_futures: Spot AI's are not touched
    Teacher(paths.teacher).save_candidate(teach(_frame()))
    client.post("/teacher/on", data={"csrf": csrf})
    futures_root = str(tmp_path / "t_futures")
    client.post("/teacher/teach", data={"csrf": csrf, "market": "futures"})
    assert submitted[-1] == [["learn", "teach", "--dir", str(tmp_path / "t"), "--market", "futures"]]
    Teacher(futures_root).save_candidate(teach(_frame()))
    client.post("/teacher/on", data={"csrf": csrf, "market": "futures"})
    client.post("/teacher/off", data={"csrf": csrf, "market": "spot"})
    assert Teacher(futures_root).active and not Teacher(paths.teacher).active
