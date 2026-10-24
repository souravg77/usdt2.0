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
