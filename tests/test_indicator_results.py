"""Phase 3: the 20 standard indicators share one output (IndicatorResult): direction,
confidence, validity and warm-up, on closed candles only."""

import math

import numpy as np
import pandas as pd
import pytest

from aitrader.indicators import donchian, value_area, vwap_bands
from aitrader.indicators.results import INDICATORS, IndicatorResult, closed_only, evaluate_all, funding_result

from test_indicators import frame

HOUR = 3_600_000


def trend(n=400, drift=0.004, noise=0.004, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    df = frame(close, spread=0.0)
    df["high"], df["low"] = close * 1.003, close * 0.997
    df["volume"] = rng.uniform(5, 10, n) * np.where(np.diff(np.r_[close[0], close]) > 0, 1.5, 1.0)
    return df


def test_all_twenty_return_the_standard_shape():
    assert len(INDICATORS) == 20
    df = trend()
    df["funding_rate"] = 0.0001
    res = evaluate_all(df)
    assert set(res) == set(INDICATORS)
    for name, r in res.items():
        assert isinstance(r, IndicatorResult) and r.name == name
        assert r.direction in (-1, 0, 1) and 0.0 <= r.confidence <= 1.0
        assert r.is_valid, name
        assert r.warmup_period >= 1 and isinstance(r.metadata, dict)
        assert r.features and all(isinstance(v, float) for v in r.features.values())
        assert r.as_dict()["name"] == name


def test_trend_followers_agree_on_a_clear_trend():
    up, down = evaluate_all(trend()), evaluate_all(trend(drift=-0.004))
    for name in ("ema", "sma", "adx", "supertrend", "ichimoku", "obv", "fibonacci"):
        assert up[name].direction == 1, name
        assert down[name].direction == -1, name
    assert up["macd"].direction in (0, 1) and down["macd"].direction in (0, -1)  # never against the line
    assert up["sma"].confidence > 0.5 and up["adx"].metadata["trending"]
    assert up["atr"].direction == 0  # volatility has no side


def test_overbought_is_a_zone_not_a_sell_signal():
    r = evaluate_all(trend(drift=0.01, noise=0.002))["rsi"]
    assert r.metadata["zone"] == "overbought" and r.direction == 1


def test_warmup_and_missing_candles_make_results_invalid():
    df = trend()
    short = evaluate_all(df.iloc[:30])
    for name in ("ema", "sma", "macd", "ichimoku", "atr", "volume_profile", "fibonacci"):
        assert not short[name].is_valid and short[name].direction == 0 and short[name].confidence == 0
    gap = df.drop(df.index[-5])  # one candle missing four bars ago
    res = evaluate_all(gap)
    for name in ("ema", "rsi", "macd", "bollinger", "donchian", "supertrend"):
        assert not res[name].is_valid, name
    assert res["funding"].direction == 0  # no funding column: unknown, not neutral-valid
    assert not res["funding"].is_valid


def test_closed_only_drops_the_forming_candle():
    df = trend(10)
    now = int(df["ts"].iloc[-1]) + HOUR // 2  # the last candle is half way through
    assert len(closed_only(df, now, HOUR)) == 9
    assert len(closed_only(df, now + HOUR, HOUR)) == 10
    full = evaluate_all(trend())
    again = evaluate_all(closed_only(pd.concat([trend(), trend(401).iloc[-1:].assign(ts=400 * HOUR)]),
                                     400 * HOUR + 10, HOUR))
    assert full["rsi"] == again["rsi"]  # the unclosed bar never reaches a decision


def test_donchian_excludes_the_current_bar_and_flags_breakouts():
    df = frame(np.r_[np.full(25, 100.0), 110.0])
    ch = donchian(df, 20)
    assert ch["upper"].iloc[-1] == 101.0  # previous 20 bars only
    r = INDICATORS["donchian"](df)
    assert r.direction == 1 and r.confidence > 0 and r.is_valid


def test_vwap_bands_reset_each_day():
    df = trend(60)
    vb = vwap_bands(df, anchor="D")
    assert vb["bars"].iloc[0] == 1 and vb["bars"].iloc[24] == 1 and vb["bars"].iloc[23] == 24
    assert (vb["upper2"] >= vb["upper1"]).all() and (vb["lower1"] >= vb["lower2"]).all()
    r = INDICATORS["vwap"](df)
    assert r.metadata["anchor"] == "D" and r.is_valid


def test_value_area_holds_seventy_percent_of_volume():
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 2, 300)
    df = frame(close)
    va = value_area(df, bins=24)
    assert va["val"] < va["poc"] < va["vah"]
    typical = (df["high"] + df["low"] + df["close"]) / 3
    inside = df["volume"][(typical >= va["val"]) & (typical <= va["vah"])].sum()
    assert inside / df["volume"].sum() >= 0.70


def test_funding_extremes_are_contrarian():
    crowded_longs = funding_result(rate=0.001)
    assert crowded_longs.direction == -1 and crowded_longs.confidence == 1.0
    assert crowded_longs.features["annualized"] == pytest.approx(0.001 * 3 * 365)
    assert funding_result(rate=-0.0006).direction == 1
    assert funding_result(rate=0.0001).direction == 0 and funding_result(rate=0.0001).is_valid
    assert not funding_result().is_valid and math.isnan(funding_result().features["rate"])



def test_cli_prints_the_twenty(tmp_path, monkeypatch, capsys):
    import json
    import time

    from aitrader import cli
    from aitrader.data.store import CandleStore

    from test_market import H, fill_store, synthetic

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AITRADER_DB_PATH", str(tmp_path / "c.db"))
    assert cli.main(["--env-file", "", "indicators"]) == 1  # nothing stored yet
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=600)
    df["ts"] += int(time.time() * 1000) - 600 * H
    fill_store(store, df)
    store.close()
    capsys.readouterr()
    assert cli.main(["--env-file", "", "indicators", "--symbol", "BTC/USDT", "--timeframe", "1h"]) == 0
    out = capsys.readouterr().out
    assert "supertrend" in out and "funding" in out and "not valid" in out  # no funding stored
    assert cli.main(["--env-file", "", "indicators", "--timeframe", "1h", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 20 and data["rsi"]["is_valid"]
