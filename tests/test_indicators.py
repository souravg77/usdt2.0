import numpy as np
import pandas as pd
import pytest

from aitrader.indicators import (
    atr, bollinger, ema, ema_stack, macd, market_structure, rsi, sma, support_resistance,
    swing_points, volume_profile, fib_levels, ichimoku, parabolic_sar, relative_volume, stoch_rsi,
    vwap,
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


def test_stoch_rsi_range_and_extremes():
    rng = np.random.default_rng(1)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    s = stoch_rsi(close)
    vals = s["stoch_rsi"].dropna()
    assert len(vals) > 200 and vals.between(0, 1).all()
    # after a run of rises RSI sits at the top of its range
    up = pd.concat([close, pd.Series(close.iloc[-1] + np.cumsum(np.abs(rng.normal(1, 0.2, 30))))],
                   ignore_index=True)
    assert stoch_rsi(up)["stoch_rsi"].iloc[-1] > 0.8


def test_parabolic_sar_below_in_uptrend_above_in_downtrend():
    up = frame(np.linspace(100, 200, 60))
    sar = parabolic_sar(up)
    assert (sar["trend"].iloc[5:] == 1).all()
    assert (sar["sar"].iloc[5:] < up["low"].iloc[5:]).all()
    down = frame(np.r_[np.linspace(100, 200, 40), np.linspace(199, 120, 40)])
    sd = parabolic_sar(down)
    assert sd["trend"].iloc[-1] == -1 and sd["sar"].iloc[-1] > down["high"].iloc[-1]


def test_parabolic_sar_has_no_lookahead():
    rng = np.random.default_rng(3)
    df = frame(100 + np.cumsum(rng.normal(0, 1, 200)))
    full, part = parabolic_sar(df), parabolic_sar(df.iloc[:120])
    pd.testing.assert_frame_equal(full.iloc[:120], part)


def test_ichimoku_cloud_is_shifted_forward():
    df = frame(np.linspace(100, 300, 200))
    ich = ichimoku(df)
    assert ich["span_b"].iloc[:51 + 26].isna().all() and ich["span_b"].notna().iloc[-1]
    assert ich["above_cloud"].iloc[-1] == 1
    part = ichimoku(df.iloc[:150])
    pd.testing.assert_frame_equal(ich.iloc[:150], part)


def test_vwap_rolling_and_daily_anchor():
    df = frame([10.0, 10, 20, 20], spread=0.0, volume=np.array([1.0, 1, 3, 3]))
    v = vwap(df, n=2)
    assert np.isnan(v.iloc[0]) and v.iloc[1] == 10 and v.iloc[3] == 20
    assert vwap(df, n=4).iloc[-1] == pytest.approx((10 + 10 + 60 + 60) / 8)
    # 30 hourly bars cross midnight UTC: the anchored VWAP restarts at bar 24
    day = frame(np.r_[np.full(24, 50.0), np.full(6, 80.0)], spread=0.0)
    d = vwap(day, anchor="D")
    assert d.iloc[23] == 50 and d.iloc[24] == 80


def test_relative_volume():
    df = frame(np.ones(25), volume=np.r_[np.ones(24), 3.0])
    assert relative_volume(df, 20).iloc[-1] == pytest.approx(3.0)


def test_fib_levels():
    lv = fib_levels(100, 200)
    assert lv[0.5] == 150 and lv[0.618] == pytest.approx(138.2) and lv[0.236] == pytest.approx(176.4)
