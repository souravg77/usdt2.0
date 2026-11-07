"""ml_cusum_resnet_lstm: the paper's ResNet-LSTM on CUSUM bars (needs PyTorch)."""

import numpy as np
import pandas as pd
import pytest

from aitrader.learn.paper import history_start
from aitrader.strategies import STRATEGIES, ml
from aitrader.strategies import deep

H4 = 4 * 3_600_000
FAST = {"seeds": 1, "epochs": 15, "patience": 4}


def legs(n=6000, p=0.8, seed=0):
    """Candles in legs: a 2.5 % move (it fires the 2 % CUSUM filter), then 6 % more the same way
    with chance ``p`` (else the other way), then a quiet stretch. A model that learns should beat
    the coin-flip base rate on events it never saw."""
    rng = np.random.default_rng(seed)
    prices = [100.0]
    while len(prices) < n:
        d = rng.choice([1, -1])
        prices += [prices[-1] * (1 + d * 0.005 * (i + 1)) for i in range(5)]
        cont = d if rng.random() < p else -d
        base = prices[-1]
        prices += [base * (1 + cont * 0.005 * (i + 1)) for i in range(12)]
        base = prices[-1]
        prices += [base * (1 + rng.normal(0, 0.001)) for _ in range(int(rng.integers(3, 10)))]
    c = np.array(prices[:n])
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": np.arange(n) * H4, "open": o, "close": c, "high": np.maximum(c, o) * 1.001,
                         "low": np.minimum(c, o) * 0.999, "volume": 1000 + rng.random(n)})


def test_cusum_bars_join_the_candles_between_events():
    df = pd.DataFrame({"ts": np.arange(6), "open": [1, 2, 3, 4, 5, 6.0], "high": [2, 3, 9, 5, 6, 7.0],
                       "low": [0.5, 1, 2, 3, 1, 5.0], "close": [2, 3, 4, 5, 6, 7.0], "volume": [1.0] * 6})
    bars, at = deep.cusum_bars(df, np.array([False, True, False, False, True, False]))
    assert list(at) == [1, 4]
    assert bars.to_dict("list") == {"ts": [1, 4], "open": [1.0, 3.0], "high": [3.0, 9.0], "low": [0.5, 1.0],
                                    "close": [3.0, 6.0], "volume": [2.0, 3.0]}


def test_it_is_registered_for_4h_with_enough_history():
    s = STRATEGIES["ml_cusum_resnet_lstm"]()
    assert s.spec.timeframes == ("4h",) and s.history_align == 540
    now = 1_760_000_000_000
    assert now - history_start("4h", now) >= (s.history_bars + 540) * H4


def test_without_pytorch_it_stays_quiet(monkeypatch):
    monkeypatch.setattr(deep, "torch_available", lambda: False)
    df = legs(n=2000)
    assert deep.MlCusumResnetLstm(**FAST).generate(df, "bybit", "ETH/USDT", "4h") == []


def test_the_network_learns_walk_forward_and_never_changes_its_answers():
    pytest.importorskip("torch")
    df = legs()
    ml._MODELS.clear()
    ml._PREDICTIONS.clear()
    s = deep.MlCusumResnetLstm(**FAST)
    full = s.probabilities(df, "4h")
    y = s._labels(df)
    ev = ml.cusum_events(df["close"], 0.02)["event"].to_numpy()
    m = full.notna().to_numpy() & np.isfinite(y)
    assert m.sum() > 200 and not full[~ev].notna().any()  # answers only on event candles
    hit = ((full.to_numpy()[m] >= 0.5) == (y[m] == 1)).mean()
    assert hit > max(y[m].mean(), 1 - y[m].mean()) + 0.08  # it learned the legs, out of sample
    ml._MODELS.clear()  # retrain from scratch on a shorter chart: the same answers (seeded, walk-forward)
    ml._PREDICTIONS.clear()
    part = deep.MlCusumResnetLstm(**FAST).probabilities(df.iloc[:4700], "4h")
    np.testing.assert_allclose(part.to_numpy(), full.iloc[:4700].to_numpy(), atol=1e-6)
    sigs = s.generate(df, "bybit", "ETH/USDT", "4h")
    assert sigs and all(g.features["ml_chance"] >= 0.7 and abs(g.stop / g.entry - 0.95) < 1e-9 for g in sigs)
