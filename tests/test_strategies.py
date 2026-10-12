import numpy as np
import pandas as pd
import pytest

from aitrader.strategies import (FAMILY_STRATEGIES, STRATEGIES, BuyTheDip, MomentumBreakout,
                                 TrendPullback)


DATA = {"buy_the_dip": dict(vol=0.03, drift=0.0015),
        "cci_divergence": dict(seed=8, vol=0.01, drift=0.0)}


def synthetic(n=1500, seed=7, drift=0.0004, vol=0.01, tf_ms=3_600_000):
    rng = np.random.default_rng(seed)
    ret = drift + vol * rng.standard_normal(n)
    # add regime swings so every strategy gets something to do
    ret += 0.004 * np.sin(np.arange(n) / 40)
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, vol / 2, n)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.lognormal(3, 0.5, n)
    df = pd.DataFrame({"ts": np.arange(n) * tf_ms, "open": open_, "high": high, "low": low,
                       "close": close, "volume": volume})
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def with_funding(df):
    """Synthetic funding-rate column: very negative in some stretches, missing in others."""
    df = df.copy()
    wave = np.sin(np.arange(len(df)) / 60)
    df["funding_rate"] = np.where(wave > 0.3, -0.0005, 0.0001)
    df.loc[df.index[wave < -0.9], "funding_rate"] = np.nan
    return df


def data_for(name):
    df = synthetic(**DATA.get(name, {}))
    return with_funding(df) if STRATEGIES[name].needs_context else df


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_signals_are_well_formed(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    sigs = strat.generate(data_for(name), "bybit", "BTC/USDT", tf)
    assert sigs, f"{name} produced no signals on synthetic data"
    for s in sigs:
        assert s.stop < s.entry < s.target
        if strat.params.get("target_r") is not None:
            assert s.r_multiple_target == pytest.approx(strat.params["target_r"])
        else:
            assert s.r_multiple_target >= strat.params.get("min_rr", 0)
        assert s.strategy == name


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_no_lookahead(name):
    """Signals up to bar k must not change when later bars are added."""
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    df = data_for(name)
    full = strat.generate(df, "bybit", "BTC/USDT", tf)
    cut = 1000
    part = strat.generate(df.iloc[:cut], "bybit", "BTC/USDT", tf)
    assert part
    last_close = int(df["ts"].iloc[cut])  # close time of the last bar in the cut frame
    early = [s for s in full if s.ts <= last_close]
    assert [(s.ts, round(s.entry, 9), round(s.stop, 9)) for s in early] == \
           [(s.ts, round(s.entry, 9), round(s.stop, 9)) for s in part]


def test_buy_the_dip_skips_other_symbols():
    assert BuyTheDip().generate(synthetic(), "bybit", "DOGE/USDT", "1d") == []


def test_specs_have_every_field():
    for cls in (TrendPullback, MomentumBreakout, BuyTheDip):
        sp = cls.spec
        for f in ("setup", "entry", "stop", "target", "filter", "size"):
            assert getattr(sp, f), f"{sp.name} missing {f}"
        assert sp.decisions


def test_signals_cli_writes_log(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main
    from aitrader.data.store import CandleStore
    from aitrader.models import Candle

    db = tmp_path / "c.db"
    df = synthetic(tf_ms=14_400_000)
    store = CandleStore(str(db))
    store.upsert(Candle("bybit", "BTC/USDT", "4h", int(r.ts), r.open, r.high, r.low, r.close, r.volume)
                 for r in df.itertuples())
    store.close()
    monkeypatch.setenv("AITRADER_DB_PATH", str(db))
    out = tmp_path / "sig.csv"
    assert main(["--env-file", "", "signals", "--strategy", "trend_pullback", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0].startswith("ts,strategy,exchange") and len(lines) > 1
    assert "signals over 1500 bars" in capsys.readouterr().out


def test_cci_divergence_signals_obey_the_books_rules():
    strat = STRATEGIES["cci_divergence"]()
    sigs = []
    for seed in (0, 2, 8, 12):
        sigs += strat.generate(synthetic(seed=seed, vol=0.02, drift=0.0), "bybit", "BTC/USDT", "1d")
    assert sigs
    for s in sigs:
        assert s.features["cci_a"] < -150 and s.features["cci_c"] < -130
        assert s.features["cci_c"] > s.features["cci_a"]
        assert 1.13 <= s.features["fib"] <= 1.618
        assert (s.target - s.entry) >= (s.entry - s.stop)


FAMILY_NAMES = [cls.spec.name for cls in FAMILY_STRATEGIES]


def test_family_specs_state_every_field_and_are_registered():
    for cls in FAMILY_STRATEGIES:
        sp = cls.spec
        for f in ("setup", "entry", "stop", "target", "filter", "size", "regime"):
            assert getattr(sp, f), f"{sp.name} missing {f}"
        assert sp.decisions and STRATEGIES[sp.name] is cls
        assert "missing" in sp.filter  # every one refuses to trade over missing candles


@pytest.mark.parametrize("name", FAMILY_NAMES)
def test_family_strategies_are_long_only_and_backtest(name):
    from aitrader.backtest import BacktestConfig, run_backtest

    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    df = data_for(name)
    sigs = strat.generate(df, "bybit", "BTC/USDT", tf)
    assert all(s.side.value == "buy" and s.stop < s.entry for s in sigs)
    assert run_backtest(strat, df, "bybit", "BTC/USDT", tf, BacktestConfig())


@pytest.mark.parametrize("name", FAMILY_NAMES)
def test_family_strategies_skip_bars_after_missing_candles(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    df = data_for(name)
    step = int(df["ts"].iloc[1] - df["ts"].iloc[0])
    full = [s for s in strat.generate(df, "bybit", "BTC/USDT", tf) if s.ts > df["ts"].iloc[500]]
    assert full
    hole = int((full[0].ts - df["ts"].iloc[0]) // step) - 10  # ten bars before a known signal
    gapped = df.drop(df.index[hole])
    after = [s for s in strat.generate(gapped, "bybit", "BTC/USDT", tf)
             if df["ts"].iloc[hole] < s.ts <= df["ts"].iloc[hole] + 100 * step]
    assert after == []


def test_funding_extreme_long_needs_fresh_negative_funding():
    strat = STRATEGIES["funding_extreme_long"]()
    df = synthetic()
    assert strat.needs_context and strat.generate(df, "bybit", "BTC/USDT", "1h") == []
    funded = with_funding(df)
    sigs = strat.generate(funded, "bybit", "BTC/USDT", "1h")
    assert sigs
    by_close = dict(zip(funded["ts"] + 3_600_000, funded["funding_rate"]))
    assert all(by_close[s.ts] <= -0.0003 for s in sigs)
    funded["funding_rate"] = np.nan  # stale or missing data: never a trade
    assert strat.generate(funded, "bybit", "BTC/USDT", "1h") == []


def test_mtf_alignment_uses_only_closed_higher_bars():
    strat = STRATEGIES["mtf_trend_alignment"]()
    df = synthetic(n=600, drift=0.002)
    up = strat.higher_trend(df, "1h")
    assert up.iloc[-50:].any()
    # a 4h bar can only be used from the 1h bar that completes it (its 4th bar) onwards
    for cut in range(400, 412):
        assert strat.higher_trend(df.iloc[:cut], "1h").equals(up.iloc[:cut])
    first = int(np.flatnonzero(up.to_numpy())[0])
    assert (int(df["ts"].iloc[first]) // 3_600_000 + 1) % 4 == 0
    # a 4h bar with a missing 1h candle never counts as up
    gapped = df.drop(df.index[[first - 2]])
    assert not strat.higher_trend(gapped, "1h").iloc[first - 3:first + 1].any()


def test_ensemble_needs_two_families():
    from aitrader.strategies.families import EnsembleVote

    strat = EnsembleVote()
    df = synthetic()
    fam, fresh = strat.votes(df, "bybit", "BTC/USDT", "1h")
    sigs = strat.generate(df, "bybit", "BTC/USDT", "1h")
    assert sigs
    pos = {int(t) + 3_600_000: i for i, t in enumerate(df["ts"])}
    for s in sigs:
        i = pos[s.ts]
        assert fresh.iloc[i] and fam.iloc[i].sum() >= 2 and s.features["families"] >= 2
    single = EnsembleVote(min_families=3).generate(df, "bybit", "BTC/USDT", "1h")
    assert len(single) <= len(sigs)


def test_family_exit_rules_are_registered_for_paper_restarts():
    from aitrader.backtest.engine import CUSTOM_EXITS

    df = synthetic()
    for name in ("supertrend_trend", "williams_r_reversal", "cmf_pressure"):
        sigs = STRATEGIES[name]().generate(df, "bybit", "BTC/USDT", "1h")
        keys = {part for s in sigs for part in s.trailing.split("|") if not part.startswith("time_stop")}
        assert keys and all(k in CUSTOM_EXITS for k in keys)


def test_add_builtin_registers_families_as_untested_candidates(tmp_path):
    from aitrader.cli import main
    from aitrader.learn import Registry

    reg = tmp_path / "r.json"
    assert main(["--env-file", "", "learn", "--registry", str(reg), "add-builtin"]) == 0
    entries = Registry(str(reg)).entries
    for name in FAMILY_NAMES:
        assert entries[f"builtin-{name}"].status == "candidate"

