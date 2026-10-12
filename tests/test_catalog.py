"""Phase 4: the master prompt's 40 strategies, each behind the same interface (entry, exit,
invalidation, stop loss, eligible regimes, no-trade conditions)."""

import math

import numpy as np
import pandas as pd
import pytest

from aitrader.market.regime import REGIMES
from aitrader.models import TIMEFRAME_MS, Side
from aitrader.strategies import STRATEGIES
from aitrader.strategies.catalog import CATALOG, CATEGORIES, by_number, tradeable
from aitrader.strategies.master import MASTER_STRATEGIES, MlClassifier
from aitrader.strategies.mirror import ShortMirror
from aitrader.strategies.quant import (BasisCarry, CrossExchangeArbitrage, PairsTrading, adf_tstat,
                                       engle_granger, ou_half_life)

from test_strategies import DATA, STRATEGIES as _S, synthetic, with_funding


def data_for(name, tf=None):
    """The shared synthetic market, spaced at the strategy's own timeframe."""
    kw = dict(DATA.get(name, {}))
    kw.setdefault("tf_ms", TIMEFRAME_MS[tf or _S[name].spec.timeframes[0]])
    df = synthetic(**kw)
    return with_funding(df) if _S[name].needs_context else df

TRADES = [e.name for e in tradeable()]
MASTER = [cls.spec.name for cls in MASTER_STRATEGIES]


def test_catalog_lists_all_forty_in_five_categories():
    assert [e.number for e in CATALOG] == list(range(1, 41))
    assert len({e.name for e in CATALOG}) == 40
    for i, cat in enumerate(CATEGORIES):
        assert {e.category for e in CATALOG[i * 8:(i + 1) * 8]} == {cat}
    status = [e.status for e in CATALOG]
    assert status.count("trades") == 37 and status.count("research only") == 2
    assert by_number(36).status == "not possible" and by_number(36).cls is CrossExchangeArbitrage
    for e in tradeable():
        assert STRATEGIES[e.name] is e.cls  # every trading one can be backtested and paper traded


@pytest.mark.parametrize("name", [e.name for e in CATALOG])
def test_every_strategy_states_its_rules_and_regimes(name):
    e = next(x for x in CATALOG if x.name == name)
    sp = e.cls.spec
    for f in ("setup", "entry", "stop", "target", "filter", "size"):
        assert getattr(sp, f), f"{name} missing {f}"
    assert set(e.cls.eligible_regimes) <= set(REGIMES)
    if e.status == "trades":
        assert e.cls.eligible_regimes and sp.regime and sp.decisions
    else:
        assert e.cls.eligible_regimes == ()


def _signal_bar(df, sig, tf):
    return int(np.flatnonzero(df["ts"].to_numpy(np.int64) + TIMEFRAME_MS[tf] == sig.ts)[0])


@pytest.mark.parametrize("name", TRADES)
def test_signals_respect_eligible_regimes(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    df = data_for(name)
    sigs = strat.generate(df, "bybit", "BTC/USDT", tf)
    labels = strat.market_regimes(df, tf)
    for s in sigs:
        assert labels.iloc[_signal_bar(df, s, tf)] in strat.eligible_regimes


@pytest.mark.parametrize("name", TRADES)
def test_entry_exit_stop_and_invalidation(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    df = data_for(name)
    sigs = strat.generate(df, "bybit", "BTC/USDT", tf)
    assert sigs, name
    s = sigs[-1]
    i = _signal_bar(df, s, tf)
    upto = df.iloc[:i + 1]
    # entry: the newest bar's signal, and nothing on the bar before it
    got = strat.entry_signal(upto, "bybit", "BTC/USDT", tf)
    assert got is not None and got.ts == s.ts and got.entry == pytest.approx(s.entry)
    assert strat.no_trade_conditions(upto, tf) == []  # it just traded there
    # stop loss: below the entry, above zero
    stop = strat.calculate_stop_loss(s.entry, upto)
    assert 0 < stop < s.entry
    # exit: a bar through the stop exits as "stop", one through the target as "target"
    nxt = upto.copy()
    nxt.iloc[-1, nxt.columns.get_loc("low")] = s.stop * 0.99
    assert strat.exit_signal(nxt, s) == "stop"
    nxt = upto.copy()
    nxt.iloc[-1, nxt.columns.get_loc("low")] = s.stop * 1.001 + 0.5 * (s.entry - s.stop)
    nxt.iloc[-1, nxt.columns.get_loc("high")] = s.target * 1.01
    assert strat.exit_signal(nxt, s) == "target"
    # invalidation: a close through the stop
    nxt = upto.copy()
    nxt.iloc[-1, nxt.columns.get_loc("close")] = s.stop * 0.99
    assert strat.invalidation_condition(nxt, s)


@pytest.mark.parametrize("name", MASTER)
def test_new_strategies_skip_bars_after_missing_candles(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    df = data_for(name)
    step = int(df["ts"].iloc[1] - df["ts"].iloc[0])
    sigs = [s for s in strat.generate(df, "bybit", "BTC/USDT", tf) if s.ts > df["ts"].iloc[200]]
    assert sigs
    hole = int((sigs[0].ts - df["ts"].iloc[0]) // step) - 10
    gapped = df.drop(df.index[hole])
    after = [s for s in strat.generate(gapped, "bybit", "BTC/USDT", tf)
             if df["ts"].iloc[hole] < s.ts <= df["ts"].iloc[hole] + 100 * step]
    assert after == []
    assert any("missing" in r for r in strat.no_trade_conditions(gapped.iloc[:hole + 5], tf))


def test_a_regime_outside_the_list_drops_the_signal():
    strat = STRATEGIES["donchian_trend"]()
    df = data_for("donchian_trend")
    sigs = strat.generate(df, "bybit", "BTC/USDT", "4h")
    labels = strat.market_regimes(df, "4h")
    strat.eligible_regimes = ("range",)
    narrowed = strat.generate(df, "bybit", "BTC/USDT", "4h")
    assert len(narrowed) < len(sigs)
    assert all(labels.iloc[_signal_bar(df, s, "4h")] == "range" for s in narrowed)
    i = int(np.flatnonzero(labels.to_numpy() == "trend-up")[-1])
    assert any("regime trend-up" in r for r in strat.no_trade_conditions(df.iloc[:i + 1], "4h"))


def test_short_twins_flip_regimes_and_stops():
    long = STRATEGIES["ema_crossover"]()
    twin = ShortMirror(long)
    assert set(twin.eligible_regimes) == {"trend-down", "uncertain"}
    df = data_for("ema_crossover")
    stop = twin.calculate_stop_loss(100.0, df.iloc[:400])
    assert stop > 100.0
    sigs = twin.generate(df, "bybit", "BTC/USDT", "1h")
    labels = long.market_regimes(df, "1h")
    assert sigs and all(s.side is Side.SELL and labels.iloc[_signal_bar(df, s, "1h")] in twin.eligible_regimes
                        for s in sigs)


def test_opening_range_uses_the_session_window():
    strat = STRATEGIES["opening_range_breakout"](session_start="13:30", range_minutes=30)
    df = data_for("opening_range_breakout")
    sigs = strat.generate(df, "bybit", "BTC/USDT", "5m")
    for s in sigs:
        open_ts = s.ts - TIMEFRAME_MS["5m"]
        minutes = ((open_ts - 13.5 * 3_600_000) % 86_400_000) / 60_000
        assert 30 <= minutes < 360
    assert strat.generate(df, "bybit", "BTC/USDT", "1h") == []  # the bar is longer than the range


def test_ml_classifier_trains_only_on_known_outcomes():
    ml = MlClassifier()
    df = data_for("ml_classifier")
    y = ml.labels(df)
    assert y.iloc[-ml.params["horizon"]:].isna().all() and y.iloc[:-ml.params["horizon"]].notna().any()
    full = ml.probabilities(df)
    assert full.iloc[:ml.params["min_train"]].isna().all() and full.notna().any()
    cut = 1100
    part = ml.probabilities(df.iloc[:cut])
    pd.testing.assert_series_equal(full.iloc[:cut], part)  # later bars change nothing earlier


# ---- research only -------------------------------------------------------------------------------


def test_research_only_strategies_never_trade():
    df = with_funding(synthetic())
    for cls in (BasisCarry, CrossExchangeArbitrage, PairsTrading):
        s = cls()
        assert s.generate(df, "bybit", "BTC/USDT", "1h") == []
        assert s.entry_signal(df, "bybit", "BTC/USDT", "1h") is None
        assert s.no_trade_conditions(df, "1h")
        assert cls.spec.name not in STRATEGIES


def test_basis_carry_counts_every_cost():
    b = BasisCarry()
    costs = 2 * 0.001 + 2 * 0.00055 + 4 * 5 / 10_000
    assert b.costs() == pytest.approx(costs)
    r = b.evaluate(0.0001, hold_hours=168)
    assert r["payments"] == 21 and r["net"] == pytest.approx(21 * 0.0001 - costs) and not r["worth_it"]
    assert b.evaluate(0.0005, hold_hours=168)["worth_it"]
    opp = b.opportunities(with_funding(synthetic()))
    assert {"funding_avg", "net", "worth_it"} <= set(opp.columns)


def _pair(n=900, seed=3, coint=True):
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 0.01, n))
    noise = np.zeros(n)
    for i in range(1, n):
        noise[i] = 0.9 * noise[i - 1] + rng.normal(0, 0.005)
    y = 0.5 + 1.2 * x + noise if coint else np.cumsum(rng.normal(0, 0.01, n))
    ts = np.arange(n) * 3_600_000
    return (pd.DataFrame({"ts": ts, "close": 100 * np.exp(y)}), pd.DataFrame({"ts": ts, "close": 50 * np.exp(x)}))


def test_engle_granger_and_half_life():
    a, b = _pair()
    eg = engle_granger(a["close"], b["close"])
    assert eg["cointegrated"] and eg["beta"] == pytest.approx(1.2, abs=0.1)
    a2, b2 = _pair(seed=5, coint=False)
    assert not engle_granger(a2["close"], b2["close"])["cointegrated"]
    rng = np.random.default_rng(0)
    s = np.zeros(3000)
    for i in range(1, 3000):
        s[i] = 0.9 * s[i - 1] + rng.normal()
    assert ou_half_life(pd.Series(s)) == pytest.approx(math.log(2) / 0.1, rel=0.25)
    assert adf_tstat(s) < -5 and math.isinf(ou_half_life(pd.Series(np.cumsum(np.ones(100)))))


def test_pairs_trading_measures_trades_after_costs():
    a, b = _pair()
    res = PairsTrading().analyse(a, b)
    assert res["tradeable"] and res["trades"]
    for t in res["trades"]:
        assert t["net"] == pytest.approx(t["gross"] - res["cost_per_trade"])
        assert t["open_ts"] >= int(a["ts"].iloc[500])  # only after the formation window
    a2, b2 = _pair(seed=5, coint=False)
    assert not PairsTrading().analyse(a2, b2)["tradeable"]
