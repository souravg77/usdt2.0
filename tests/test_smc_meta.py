"""smc_meta_boosted (meta-labeling on the SMC setups) and the ATR barriers of ml_cusum_resnet_lstm_atr."""

import importlib.util

import numpy as np
import pandas as pd
import pytest

from aitrader.strategies import STRATEGIES, ml
from aitrader.strategies.mirror import ShortMirror
from test_strategies import synthetic

HOUR = 3_600_000


def _bars(close, low=None, high=None):
    n = len(close)
    df = pd.DataFrame({"ts": np.arange(n) * HOUR, "open": close, "close": close, "volume": 1.0,
                       "high": high if high is not None else [c + 0.5 for c in close],
                       "low": low if low is not None else [c - 0.5 for c in close]})
    return df


def test_meta_label_is_the_setups_own_trade():
    df = _bars([100, 100, 101, 104.5, 100, 99, 100, 100])
    stop = pd.Series([98.0, np.nan, 99.0, np.nan, 99.8, np.nan, np.nan, np.nan])
    y = ml.meta_labels(df, stop, 2.0, 3)
    assert y[0] == 1.0  # risk 2 -> target 104: bar 3's high 105 gets there first
    assert np.isnan(y[1])  # no setup, no label
    assert y[2] == 1.0  # risk 2 -> target 105: bar 3's high 105.0 reaches it
    assert y[4] == 0.0  # stop 99.8: bar 5's low 98.5 hits it first
    assert np.isnan(y[-3:]).all()  # outcome not known yet


def test_running_out_of_time_is_not_a_win():
    df = _bars([100, 100.5, 100.2, 100.4, 100.1])
    y = ml.meta_labels(df, pd.Series([98.0] + [np.nan] * 4), 2.0, 3)
    assert y[0] == 0.0


def test_setups_pick_the_tightest_valid_stop():
    df = synthetic(n=1500, seed=1)
    S = ml.smc_setups(df)
    fired = S[list(ml.SMC_SETUPS)].any(axis=1)
    assert fired.sum() > 50
    assert (S["stop"].notna() == fired).all()
    assert (S.loc[fired, "stop"] <= df.loc[fired, "close"] * 0.998 + 1e-12).all()


def test_it_learns_on_setups_only_and_trades_walk_forward():
    df = synthetic(n=3000, seed=4)
    params = dict(min_train=100, retrain=150)
    s = ml.SmcMetaBoosted(**params)
    full = s.probabilities(df, "1h")
    has_setup = ml.smc_setups(df)["stop"].notna().to_numpy()
    assert full.notna().sum() > 100 and not full[~has_setup].notna().any()  # answers only on setups
    for cut in (2200, 2777):  # adding later candles never changes an earlier answer
        pd.testing.assert_series_equal(full.iloc[:cut], ml.SmcMetaBoosted(**params).probabilities(df.iloc[:cut], "1h"))
    sigs = ml.SmcMetaBoosted(**params, threshold=0.55).generate(df, "bybit", "BTC/USDT", "1h")
    assert sigs
    for g in sigs:
        assert g.stop < g.entry and g.target == pytest.approx(g.entry + 2 * (g.entry - g.stop))
        assert g.trailing == "time_stop=30" and g.features["ml_chance"] >= 0.55 and g.features["smc_setups"] >= 1


def test_smc_meta_is_registered_with_a_short_twin_and_keeps_the_70_percent_bar():
    assert STRATEGIES["smc_meta_boosted"] is ml.SmcMetaBoosted
    assert ml.SmcMetaBoosted().params["threshold"] == 0.7
    assert ml.SmcMetaBoosted in ml.ML_STRATEGIES and not ml.SmcMetaBoosted.needs_context
    assert ShortMirror(ml.SmcMetaBoosted()).history_bars == ml.SmcMetaBoosted.history_bars


def test_atr_barrier_follows_volatility():
    quiet = synthetic(n=400, seed=2, vol=0.005)
    wild = synthetic(n=400, seed=2, vol=0.03)
    q, w = ml.atr_barrier(quiet, 3.0), ml.atr_barrier(wild, 3.0)
    assert np.nanmedian(w) > 3 * np.nanmedian(q)
    # with each bar's own width the triple barrier is the same rule as with a fixed one
    fixed = ml.triple_barrier_labels(wild, 18, 0.05)
    per_bar = ml.triple_barrier_labels(wild, 18, np.full(len(wild), 0.05))
    np.testing.assert_array_equal(fixed, per_bar)
    y = ml.triple_barrier_labels(wild, 18, w)
    assert np.isnan(y[:13]).all()  # no ATR yet, no label
    assert np.isfinite(y[20:-18]).all() and np.isnan(y[-18:]).all()


def test_resnet_atr_variant_trains_on_atr_barriers_and_stops_there():
    from aitrader.strategies.deep import MlCusumResnetLstm, MlCusumResnetLstmAtr

    assert STRATEGIES["ml_cusum_resnet_lstm_atr"] is MlCusumResnetLstmAtr
    s = MlCusumResnetLstmAtr()
    assert s.params["threshold"] == 0.7 and s.params["barrier_atr"] == 3.0 and s.spec.timeframes == ("4h",)
    assert MlCusumResnetLstm().params["barrier"] == 0.05  # the fixed-5 % strategy is unchanged
    df = synthetic(n=600, seed=3, vol=0.02, tf_ms=4 * HOUR)
    s._tf = "4h"
    np.testing.assert_array_equal(s._labels(df), ml.triple_barrier_labels(df, 18, ml.atr_barrier(df, 3.0)))
    from aitrader.indicators import atr

    pd.testing.assert_series_equal(s.stop_series(df), df["close"] - 3.0 * atr(df))


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="needs PyTorch (the server image installs it)")
def test_resnet_atr_variant_trades_with_a_short_twin():
    from aitrader.strategies.deep import MlCusumResnetLstmAtr

    df = synthetic(n=2500, seed=0, vol=0.015, tf_ms=4 * HOUR)
    s = MlCusumResnetLstmAtr(min_train=150, retrain=100, seeds=1, epochs=10, patience=3, threshold=0.5)
    sigs = s.generate(df, "bybit", "BTC/USDT", "4h")
    assert sigs
    for g in sigs:
        assert g.target == pytest.approx(2 * g.entry - g.stop)  # 1R: the upper barrier
    assert ShortMirror(MlCusumResnetLstmAtr()).spec.timeframes == ("4h",)
