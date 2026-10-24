"""ml_boosted: the machine-learning strategy that finds its own entries."""

import numpy as np
import pandas as pd

from aitrader.learn.paper import WINDOW_BARS, history_start, window_start
from aitrader.strategies import STRATEGIES
from aitrader.strategies import ml
from aitrader.strategies.mirror import ShortMirror
from test_strategies import synthetic

HOUR = 3_600_000


def test_predictions_are_walk_forward_and_never_change_later():
    s = ml.MlBoosted()
    df = synthetic(n=3000, seed=1)
    full = s.probabilities(df, "1h")
    p = s.params
    assert full.iloc[:p["min_train"] + p["horizon"]].isna().all() and full.notna().sum() > 1000
    for cut in (1400, 2222):  # one cut mid-block: the newest model only knows what was known then
        part = ml.MlBoosted().probabilities(df.iloc[:cut], "1h")
        pd.testing.assert_series_equal(full.iloc[:cut], part)


def test_a_model_only_learns_from_bars_whose_outcome_was_known(monkeypatch):
    s = ml.MlBoosted()
    df = synthetic(n=2000, seed=2)
    seen = []
    real = s._model

    def spy(X, y, key):
        seen.append(len(y))
        return real(X, y, key)

    monkeypatch.setattr(s, "_model", spy)
    ml._MODELS.clear()
    ml._PREDICTIONS.clear()
    s.probabilities(df, "1h")
    p = s.params
    # the model for the block starting at bar 1000 learns from bars < 1000 - horizon at most
    assert seen and max(seen) <= p["train_bars"]
    labels = ml.barrier_labels(df, p["horizon"], p["up_atr"], p["down_atr"])
    known = labels[:-p["horizon"]]
    assert np.isnan(labels[-p["horizon"]:]).all() and set(np.unique(known[np.isfinite(known)])) == {0.0, 1.0}


def test_a_new_candle_reuses_the_trained_models(monkeypatch):
    """The paper trader asks on every candle; the models are trained once per retrain step."""
    df = synthetic(n=2800, seed=3)
    ml._MODELS.clear()
    ml._PREDICTIONS.clear()
    ml.MlBoosted().generate(df.iloc[:2510], "bybit", "BTC/USDT", "1h")
    fits = []
    from sklearn.ensemble import HistGradientBoostingClassifier

    real = HistGradientBoostingClassifier.fit
    monkeypatch.setattr(HistGradientBoostingClassifier, "fit",
                        lambda self, X, y: fits.append(len(y)) or real(self, X, y))
    for n in (2511, 2512, 2530):  # a fresh copy of the strategy each time, as the paper trader builds
        ml.MlBoosted().generate(df.iloc[:n], "bybit", "BTC/USDT", "1h")
    assert fits == []
    ml.MlBoosted().generate(df.iloc[:2760], "bybit", "BTC/USDT", "1h")  # bar 2750: a new block
    assert len(fits) == 1


def test_signals_carry_the_models_chance_and_fixed_risk():
    df = synthetic(n=3000, seed=1)
    sigs = STRATEGIES["ml_boosted"]().generate(df, "bybit", "BTC/USDT", "1h")
    assert sigs
    for sig in sigs:
        assert 0.5 <= sig.features["ml_chance"] <= 1
        assert sig.r_multiple_target == 2.0 and sig.trailing == "time_stop=24"
    shorts = ShortMirror(STRATEGIES["ml_boosted"]()).generate(df, "bybit", "BTC/USDT", "1h")
    assert shorts and all(x.stop > x.entry > x.target for x in shorts)


def test_paper_loads_enough_candles_on_a_fixed_grid():
    s = ml.MlBoosted()
    now = 1_760_000_000_000
    start = window_start(s, "1h", now)
    assert start % (250 * HOUR) == 0 and now - start >= s.history_bars * HOUR
    # the same start for the next candles, so the same bars (and models) are used
    assert window_start(s, "1h", now + HOUR) == start or (now + HOUR - start) // HOUR > s.history_bars + 250
    twin = ShortMirror(ml.MlBoosted())
    assert window_start(twin, "4h", now) == window_start(s, "4h", now)
    plain = STRATEGIES["ema_crossover"]()
    assert window_start(plain, "1h", now) == now - WINDOW_BARS * HOUR
    # the collector keeps enough 1h and 4h history for it; other timeframes are unchanged
    assert now - history_start("1h", now) >= (s.history_bars + 250) * HOUR
    assert history_start("15m", now) == now - 300 * 900_000


# ---- ml_ensemble: Asgari & Khasteh (2021) ------------------------------------------------------


def test_ensemble_reads_the_papers_60_candles_and_labels_rises_above_the_fee():
    s = ml.MlEnsemble()
    df = synthetic(n=900, seed=4)
    assert ml.paper_rows(df).shape[1] == 19  # OHLCV + the paper's Appendix A indicators
    X, ok = s._features(df)
    assert X.shape == (900, 60 * 19) and not ok[:59].any() and ok[-1]
    # row t holds candles t-59..t, newest last: nothing from after t
    np.testing.assert_allclose(X[500, -19:], ml.paper_rows(df).iloc[500].to_numpy(float))
    s._tf = "1h"
    y = s._labels(df)
    c = df["close"].to_numpy(float)
    assert s._horizon("1h") == 4 and s._horizon("4h") == 1  # the paper's next 4 hours
    assert y[100] == float(c[104] / c[100] - 1 > 0.0015) and np.isnan(y[-4:]).all()


def test_ensemble_is_walk_forward_and_exits_when_the_models_turn():
    from aitrader.backtest.engine import CUSTOM_EXITS

    df = synthetic(n=1500, seed=5)
    full = ml.MlEnsemble().probabilities(df, "4h")
    part = ml.MlEnsemble().probabilities(df.iloc[:1100], "4h")
    pd.testing.assert_series_equal(full.iloc[:1100], part)  # later candles never change earlier answers
    sigs = STRATEGIES["ml_ensemble"]().generate(df, "bybit", "BTC/USDT", "4h")
    assert sigs and all(x.stop < x.entry < x.target for x in sigs)
    exit_rule, tstop = sigs[0].trailing.split("|")
    assert exit_rule in CUSTOM_EXITS and tstop == "time_stop=12"
    # the exit fires on candles where the models no longer expect a rise above the fees
    down = CUSTOM_EXITS[exit_rule](df)
    assert down.equals(full < 0.5)
    shorts = ShortMirror(STRATEGIES["ml_ensemble"]()).generate(df, "bybit", "BTC/USDT", "4h")
    assert shorts and all(x.stop > x.entry > x.target for x in shorts)
    assert ml.MlEnsemble in ml.ML_STRATEGIES
