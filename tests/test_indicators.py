import numpy as np
import pandas as pd
import pytest

from aitrader.indicators import (
    atr, bollinger, ema, ema_stack, macd, market_structure, rsi, sma, support_resistance,
    swing_points, volume_profile,
)


def frame(close, spread=1.0, volume=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    df = pd.DataFrame({
        "ts": np.arange(n) * 3_600_000,
        "open": np.r_[close[0], close[:-1]],
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "volume": np.ones(n) if volume is None else volume,
    })
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def test_sma_ema_basic():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).tolist()[2:] == [2, 3, 4]
    e = ema(s, 3)
    assert np.isnan(e.iloc[0]) and e.iloc[-1] == pytest.approx(4.0625)


def test_rsi_extremes():
    assert rsi(pd.Series(np.arange(1.0, 40)), 14).iloc[-1] == 100
    assert rsi(pd.Series(np.arange(40.0, 1, -1)), 14).iloc[-1] == pytest.approx(0)
    assert np.isnan(rsi(pd.Series(np.arange(1.0, 40)), 14).iloc[5])


def test_macd_positive_in_uptrend():
    m = macd(pd.Series(np.linspace(100, 200, 100)))
    assert m["macd"].iloc[-1] > 0


def test_atr_constant_range():
    df = frame(np.full(50, 100.0), spread=2.0)
    assert atr(df).iloc[-1] == pytest.approx(4.0)


def test_bollinger_width_zero_on_flat():
    assert bollinger(pd.Series(np.full(30, 5.0)))["width"].iloc[-1] == 0


def test_ema_stack_sign():
    up = pd.Series(np.linspace(1, 100, 300))
    assert ema_stack(up).iloc[-1] == 1
    assert ema_stack(up[::-1].reset_index(drop=True)).iloc[-1] == -1


def zigzag(start, steps, up=6, down=3, n=8):
    out, p = [], start
    for _ in range(steps):
        for _ in range(n):
            p += up / n
            out.append(p)
        for _ in range(n):
            p -= down / n
            out.append(p)
    return out


def test_market_structure_uptrend_and_downtrend():
    assert market_structure(frame(zigzag(100, 8), spread=0.1)).iloc[-1] == 1
    assert market_structure(frame(zigzag(200, 8, up=3, down=6), spread=0.1)).iloc[-1] == -1


def test_swing_points_have_no_lookahead():
    df = frame(zigzag(100, 5), spread=0.1)
    full = swing_points(df)
    part = swing_points(df.iloc[:40])
    pd.testing.assert_frame_equal(full.iloc[:40], part)


def test_support_resistance_clusters_touches():
    close = [100, 105, 110, 105, 100, 105, 110, 105, 100, 105, 110, 105, 100] * 2
    levels = support_resistance(frame(close, spread=0.01), left=1, right=1)
    prices = sorted(round(l["price"]) for l in levels)
    assert 100 in prices and 110 in prices


def test_volume_profile_poc():
    close = np.r_[np.full(20, 100.0), np.full(5, 120.0)]
    vp = volume_profile(frame(close, spread=0.5), bins=10)
    assert vp.loc[vp["poc"], "low"].iloc[0] <= 100 <= vp.loc[vp["poc"], "high"].iloc[0]
    assert vp["volume"].sum() == pytest.approx(25)
