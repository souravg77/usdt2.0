"""The four price-action book strategies the rule language couldn't express (strategies.price_action)."""

import numpy as np
import pandas as pd
import pytest

from aitrader.learn.library import library_specs
from aitrader.models import TIMEFRAME_MS, Side
from aitrader.strategies import STRATEGIES
from aitrader.strategies.price_action import PRICE_ACTION_STRATEGIES, floor_pivots, pin_bars

NAMES = [cls.spec.name for cls in PRICE_ACTION_STRATEGIES]
H = 3_600_000


def synthetic(n, seed, vol, tf_ms):
    """Random prices with upper and lower wicks drawn separately (and sometimes long), so pin bars
    and dojis at a level occur."""
    rng = np.random.default_rng(seed)
    ret = vol * rng.standard_normal(n) + 0.004 * np.sin(np.arange(n) / 40)
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    up, dn = (np.abs(rng.normal(0, vol / 2, n)) * close * np.where(rng.random(n) < 0.1, 4, 1) for _ in range(2))
    df = pd.DataFrame({"ts": 1_700_006_400_000 + np.arange(n) * tf_ms, "open": open_,
                       "high": np.maximum(open_, close) + up, "low": np.minimum(open_, close) - dn,
                       "close": close, "volume": rng.lognormal(3, 0.5, n)})
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def _bars(rows, step=H):
    return pd.DataFrame([{"ts": i * step, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


def test_the_library_runs_them_as_built_ins_instead_of_skipping_them():
    specs, skipped = library_specs()
    assert skipped == [] and not {s["name"] for s in specs} & set(NAMES)
    assert all(STRATEGIES[n] in PRICE_ACTION_STRATEGIES for n in NAMES)


def test_floor_pivots_use_the_previous_utc_day():
    day = 86_400_000
    rows = [(100, 110, 90, 105)] * 24 + [(105, 106, 104, 105)] * 24  # day 1: H 110, L 90, C 105
    piv = floor_pivots(_bars(rows))
    assert piv["pp"].iloc[:24].isna().all()  # no day before the first
    pp = (110 + 90 + 105) / 3
    assert piv["pp"].iloc[24] == pytest.approx(pp) and piv["s1"].iloc[30] == pytest.approx(2 * pp - 110)
    assert piv["s2"].iloc[47] == pytest.approx(pp - 20) and piv["r1"].iloc[24] == pytest.approx(2 * pp - 90)
    assert day == 24 * H


def test_a_pin_bar_has_a_long_lower_tail_at_a_new_low():
    rows = [(100, 101, 99, 100)] * 10 + [(99.5, 100, 95, 99.8), (99.8, 100, 94, 95)]
    pins = pin_bars(_bars(rows))
    assert pins.iloc[10] and not pins.iloc[11]  # the second bar closes at its low: no pin
    assert not pins.iloc[:10].any()


@pytest.mark.parametrize("name", NAMES)
def test_long_only_valid_and_never_uses_later_candles(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    sigs, df = [], None
    for seed in (1, 4, 9):
        df = synthetic(n=2500, seed=seed, vol=0.012, tf_ms=TIMEFRAME_MS[tf])
        sigs += strat.generate(df, "bybit", "BTC/USDT", tf)
    assert sigs, f"{name} gave no signal on 7,500 candles"
    for s in sigs:
        assert s.side is Side.BUY and s.stop < s.entry < s.target and np.isfinite(s.target)
    full = {(s.ts, s.entry, s.stop) for s in strat.generate(df, "bybit", "BTC/USDT", tf)}
    part = df.iloc[:1800]
    cut = int(part["ts"].iloc[-1]) + TIMEFRAME_MS[tf]
    assert {(s.ts, s.entry, s.stop) for s in strat.generate(part, "bybit", "BTC/USDT", tf)} == \
        {k for k in full if k[0] <= cut}
