"""ml_cusum_barrier: Grądzki, Wójcik & Lessmann (2025), CUSUM-filter events and triple-barrier labels."""

import numpy as np
import pandas as pd

from aitrader.strategies import STRATEGIES, ml
from aitrader.strategies.mirror import ShortMirror
from test_strategies import synthetic

HOUR = 3_600_000


def test_cusum_filter_matches_the_papers_worked_example():
    # Fig. 8 of the paper: prices a minute apart, 2 % filter -> events at 00:03 and 00:12
    prices = [60000, 59500, 60200, 60700, 60500, 61100, 60900, 61200, 61000, 61500, 60900, 61600, 62000,
              62200, 61700]
    ev = ml.cusum_events(pd.Series(prices, dtype=float), 0.02)
    assert list(np.flatnonzero(ev["event"])) == [3, 12]
    assert round(ev["up"].iloc[3], 4) == 0.0201 and round(ev["down"].iloc[1], 4) == -0.0083


def test_triple_barrier_labels_take_the_first_barrier_then_the_time_barrier():
    ts = np.arange(8) * HOUR
    close = [100, 101, 106, 99, 100, 100, 100.1, 100.1]
    df = pd.DataFrame({"ts": ts, "open": close, "close": close, "volume": 1.0,
                       "high": [c + 0.5 for c in close], "low": [c - 0.5 for c in close]})
    df.loc[3, "low"] = 94  # bar 3 falls 5 % below bar 1's close... and bar 2 rose 5 % first
    y = ml.triple_barrier_labels(df, 3, 0.05)
    assert y[0] == 1.0 and y[1] == 1.0  # +5 % (105) reached at bar 2 before -5 %
    assert y[2] == 0.0  # from 106: -5 % (100.7) hit at bar 3
    assert y[3] == 1.0  # no barrier in 3 bars: 99 -> 100.1 is a rise above the 0.15 % fee
    assert y[4] == 0.0  # 100 -> 100.1 is a rise smaller than the fee
    assert np.isnan(y[-3:]).all()


def test_it_learns_and_trades_only_on_events_walk_forward():
    df = synthetic(n=3600, seed=4)
    s = ml.MlCusumBarrier()
    full = s.probabilities(df, "1h")
    ev = ml.cusum_events(df["close"], s.params["cusum"])["event"].to_numpy()
    assert full.notna().sum() > 100 and not full[~ev].notna().any()  # answers only on event candles
    for cut in (2600, 3111):
        pd.testing.assert_series_equal(full.iloc[:cut], ml.MlCusumBarrier().probabilities(df.iloc[:cut], "1h"))
    sigs = s.generate(df, "bybit", "BTC/USDT", "1h")
    ts = df["ts"].to_numpy()
    assert sigs and all(ev[np.searchsorted(ts, g.ts) - 1] for g in sigs)  # entry at the open after the event
    g = sigs[0]
    assert abs(g.stop / g.entry - 0.95) < 1e-9 and abs(g.target / g.entry - 1.05) < 1e-9
    assert g.trailing == "time_stop=72" and g.features["ml_chance"] >= 0.7


def test_it_is_registered_with_a_short_twin_and_a_4h_time_barrier():
    assert STRATEGIES["ml_cusum_barrier"] is ml.MlCusumBarrier
    s = ml.MlCusumBarrier()
    assert s._horizon("4h") == 18 and s._horizon("1h") == 72
    short = ShortMirror(ml.MlCusumBarrier())
    assert short.history_bars == ml.MlCusumBarrier.history_bars
