import json

import numpy as np
import pandas as pd
import pytest

from aitrader.ai import FilterModel, LiveFilter, ModelRegistry, train
from aitrader.ai.dataset import build_dataset, finish, reached_1r, signal_rows, take_sequential
from aitrader.ai.features import MARKET, NUMERIC, history_features, market_features
from aitrader.backtest import run_backtest
from aitrader.cli import main
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, PaperConfig, PaperTrader, Registry, RetireRule, validate_spec
from aitrader.strategies import STRATEGIES

from test_learn import EVERY_UP_BAR, fill_store
from test_strategies import synthetic

H = 3_600_000


def planted(n=1500, edge=True, seed=0):
    """A training set where ADX above 30 really does decide the outcome (``edge``), or where
    nothing does."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        row = {c: rng.normal() for c in NUMERIC}
        row["adx"] = rng.uniform(0, 60)
        p = (0.75 if row["adx"] > 30 else 0.25) if edge else 0.35
        hit = rng.random() < p
        ts = i * 3 * H
        row.update(strategy=f"s{i % 3}", timeframe="1h", source="backtest", exchange="x",
                   symbol=f"S{i % 5}", signal_ts=ts, entry_ts=ts, exit_ts=ts + H,
                   exit_reason="target" if hit else "stop", r=1.5 if hit else -1.0,
                   hit_1r=int(hit))
        rows.append(row)
    return finish(rows)


class Const:
    """Stand-in estimator with a fixed probability."""

    def __init__(self, p):
        self.p = p

    def predict_proba(self, x):
        return np.column_stack([1 - np.full(len(x), self.p), np.full(len(x), self.p)])


def const_model(p, threshold=0.5):
    return FilterModel(Const(p), {"strategy": [], "timeframe": []}, list(MARKET), threshold,
                       full_size_p=0.9)


def test_market_features_have_no_lookahead():
    df = synthetic(n=900)
    full = market_features(df)
    for i in (300, 650, 899):
        part = market_features(df.iloc[:i + 1]).iloc[-1]
        pd.testing.assert_series_equal(full.iloc[i], part, check_names=False)
    assert full["funding_rate"].isna().all()  # not stored yet


def test_reached_1r_stop_first():
    df = pd.DataFrame({"high": [101, 103, 106], "low": [99, 97, 100]})
    assert reached_1r(df, 0, fill=100, stop=95, risk=5) == 1  # 105 reached on bar 2, stop never
    assert reached_1r(df, 0, fill=100, stop=97, risk=3) == 0  # bar 1 touches both: stop first
    assert reached_1r(df, 0, fill=100, stop=90, risk=20) == 0  # neither


def test_history_features():
    assert history_features([]) == {"streak": 0.0, "recent_r": pytest.approx(np.nan, nan_ok=True)}
    h = history_features([1.0, -1.0, 2.0, 0.5])
    assert h["streak"] == 2 and h["recent_r"] == pytest.approx(0.625)
    assert history_features([1.0, -1.0, -1.0])["streak"] == -2


def test_taken_rows_match_the_backtest():
    df = synthetic(n=2000)
    strat = STRATEGIES["momentum_breakout"]()
    frame = finish(signal_rows(strat, "mb", df, "bybit", "BTC/USDT", "1h"))
    bt = run_backtest(strat, df, "bybit", "BTC/USDT", "1h")
    taken = frame[frame["taken"]]
    assert len(frame) > len(taken) > 5
    assert list(taken["entry_ts"]) == [t.entry_ts for t in bt]
    assert np.allclose(taken["r"], [t.r for t in bt])
    # a strategy's streak only counts trades that closed before the signal
    later = taken.iloc[-1]
    done = taken[taken["exit_ts"] < later["signal_ts"]]
    assert later["recent_r"] == pytest.approx(done["r"].tail(5).mean())


def test_take_sequential_frees_the_slot():
    f = pd.DataFrame({"strategy": "s", "exchange": "x", "symbol": "A", "timeframe": "1h",
                      "signal_ts": [0, 1, 2, 3], "entry_ts": [1, 2, 3, 4], "exit_ts": [3, 3, 4, 9]})
    assert take_sequential(f).tolist() == [True, False, False, True]
    assert take_sequential(f, [False, True, True, True]).tolist() == [False, True, False, True]


def test_take_sequential_keeps_markets_apart():
    # rows out of order, two markets and two strategies: each (strategy, market) holds its own slot
    f = pd.DataFrame({"strategy": ["s", "s", "t", "s", "s", "t"], "exchange": "x",
                      "symbol": ["A", "B", "A", "A", "B", "A"], "timeframe": "1h",
                      "signal_ts": [2, 0, 0, 0, 2, 2], "entry_ts": [3, 1, 1, 1, 3, 3],
                      "exit_ts": [9, 9, 2, 5, 9, 9]})
    assert take_sequential(f).tolist() == [False, True, True, True, False, True]
    cat = f.astype({c: "category" for c in ("strategy", "exchange", "symbol", "timeframe")})
    assert take_sequential(cat).tolist() == take_sequential(f).tolist()


def test_filter_learns_a_real_edge_and_beats_the_rules():
    model, rep = train(planted())
    t = rep["test"]
    assert rep["passed"], rep["failed"]
    assert t["filtered"]["expectancy_r"] > t["unfiltered"]["expectancy_r"] + 0.2
    assert rep["probability"]["auc"] > 0.65
    assert rep["importance"][0][0] == "adx"
    # sizing stays within the strategy's own risk
    s = model.sizes([0.0, model.threshold, 0.99])
    assert s[0] == 0 and 0.5 <= s[1] <= 1 and s[2] == 1.0


def test_filter_without_an_edge_is_rejected():
    _, rep = train(planted(edge=False, seed=1))
    assert not rep["passed"] and rep["failed"]


def test_split_is_chronological_and_purged():
    from aitrader.ai.model import TrainConfig, split

    f = planted(500)
    f.loc[f.index[250], "exit_ts"] = 10**15  # a trade that stays open into the later periods
    tr, val, test = split(f, TrainConfig())
    assert tr["signal_ts"].max() < val["signal_ts"].min() <= val["signal_ts"].max() < test["signal_ts"].min()
    assert tr["exit_ts"].max() < val["signal_ts"].min()


def test_registry_promote_and_rollback(tmp_path):
    reg = ModelRegistry(str(tmp_path / "m"))
    model, rep = train(planted())
    v1 = reg.add(model, rep)
    _, bad = train(planted(edge=False, seed=1))
    v_bad = reg.add(model, bad)
    assert reg.info(v1)["status"] == "candidate" and reg.info(v_bad)["status"] == "rejected"
    assert reg.active is None  # training never promotes
    with pytest.raises(ValueError):
        reg.promote(v_bad, "shawn")
    with pytest.raises(ValueError):
        reg.promote(v1, "")
    reg.promote(v1, "shawn")
    v2 = reg.add(model, rep)
    ModelRegistry(str(tmp_path / "m")).promote(v2, "shawn")
    reg = ModelRegistry(str(tmp_path / "m"))
    assert reg.active == v2 and reg.info(v1)["status"] == "archived"
    assert reg.rollback() == v1 and reg.active == v1
    assert reg.load(v1).threshold == model.threshold
    (tmp_path / "m" / v1 / "model.pkl").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum"):
        reg.load(v1)
    assert reg.rollback() is None  # nothing older: filter off


def _paper(tmp_path, ai_filter):
    df = synthetic(n=800)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    reg_path = str(tmp_path / "reg.json")
    reg = Registry(reg_path)
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    trader = PaperTrader(store, reg_path, ["bybit"], ["BTC/USDT"],
                         PaperConfig(retire=RetireRule(min_trades=10_000, max_drawdown=1e9)),
                         state_path=str(tmp_path / "s.json"), ai_filter=ai_filter)
    events = {"opened": 0, "filtered": 0}
    for i in range(600, len(df)):
        ev = trader.tick(now_ms=int(df.ts.iloc[i]) + H)
        events["opened"] += len(ev["opened"])
        events["filtered"] += len(ev["filtered"])
    return trader, events


def test_paper_trader_follows_the_active_model(tmp_path):
    models = ModelRegistry(str(tmp_path / "m"))
    _, rep = train(planted())
    assert rep["passed"]
    v_skip = models.add(const_model(0.2), rep)
    models.promote(v_skip, "test")
    _, ev = _paper(tmp_path / "a", LiveFilter(str(tmp_path / "m")))
    assert ev["opened"] == 0 and ev["filtered"] > 5

    v_take = models.add(const_model(0.7), rep)
    models.promote(v_take, "test")
    trader, ev = _paper(tmp_path / "b", LiveFilter(str(tmp_path / "m")))
    assert ev["filtered"] == 0 and ev["opened"] > 5
    t = trader.state["trades"][0]
    assert t["ai_prob"] == pytest.approx(0.7) and t["ai_version"] == v_take
    unfiltered, _ = _paper(tmp_path / "c", None)
    # p=0.7 sits between the threshold and full size, so positions are smaller than unfiltered
    assert t["units"] < unfiltered.state["trades"][0]["units"]


def test_cli_retrain_promote_rollback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = CandleStore("data/aitrader.db")
    for seed, sym in ((7, "BTC/USDT"), (8, "ETH/USDT")):
        fill_store(store, synthetic(n=5000, seed=seed), sym=sym)
    store.close()
    reg = Registry("data/learn/registry.json")
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    assert main(["--env-file", "none", "ai", "dataset"]) == 0
    frame = pd.read_csv("data/ai/dataset.csv")
    assert len(frame) > 200 and set(frame["strategy"]) == {"up"}
    # the final 20% of history is kept back
    last_ts = synthetic(n=5000)["ts"].iloc[int(5000 * 0.8)]
    assert frame["signal_ts"].max() <= last_ts + H
    main(["--env-file", "none", "ai", "retrain"])
    out = capsys.readouterr().out
    assert "Test period, side by side" in out and "Unfiltered rules" in out
    models = ModelRegistry("data/ai/models")
    [v] = models.versions()
    assert models.active is None
    if models.info(v)["status"] == "candidate":
        assert main(["--env-file", "none", "ai", "promote", v, "--yes", "--by", "shawn"]) == 0
        assert ModelRegistry("data/ai/models").active == v
        assert main(["--env-file", "none", "ai", "rollback"]) == 0
        assert ModelRegistry("data/ai/models").active is None
    else:
        assert main(["--env-file", "none", "ai", "promote", v, "--yes"]) == 1
    assert main(["--env-file", "none", "ai", "list"]) == 0


def test_build_dataset_skips_short_markets(tmp_path):
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, synthetic(n=100))
    strat = STRATEGIES["momentum_breakout"]()
    frame = build_dataset(store, [("mb", strat)], ["bybit"], ["BTC/USDT"])
    assert frame.empty and "hit_1r" in frame.columns


def test_the_training_set_is_built_one_market_at_a_time(tmp_path, monkeypatch):
    """Each market's candles are loaded once and let go before the next (not all kept at once), and
    the rows match running each strategy on its own."""
    import pandas as pd

    from aitrader.ai import dataset
    from aitrader.data.store import CandleStore
    from aitrader.strategies import STRATEGIES
    from test_learn import fill_store, synthetic

    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=900)
    for sym in ("BTC/USDT", "ETH/USDT"):
        fill_store(store, df, sym=sym)
    names = [n for n in STRATEGIES if "1h" in STRATEGIES[n]().spec.timeframes][:4]
    strats = [(f"builtin-{n}", STRATEGIES[n]()) for n in names]
    loads = []
    real = store.load
    monkeypatch.setattr(store, "load", lambda *a, **k: loads.append(a[:3]) or real(*a, **k))
    frame = dataset.build_dataset(store, strats, ["bybit"], ["BTC/USDT", "ETH/USDT"])
    assert sorted(loads) == sorted(set(loads))  # every market read once
    alone = pd.concat([dataset.build_dataset(store, [s], ["bybit"], ["BTC/USDT", "ETH/USDT"]) for s in strats])
    assert len(frame) > 0 and len(frame) == len(alone) and frame["strategy"].value_counts().to_dict() == \
        alone["strategy"].value_counts().to_dict()
    assert frame["atr_pct"].dtype == "float32"
    store.close()
