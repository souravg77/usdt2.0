import numpy as np
import pandas as pd
import pytest

from aitrader.strategies import STRATEGIES, BuyTheDip, MomentumBreakout, TrendPullback


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


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_signals_are_well_formed(name):
    strat = STRATEGIES[name]()
    tf = strat.spec.timeframes[0]
    sigs = strat.generate(synthetic(**DATA.get(name, {})), "bybit", "BTC/USDT", tf)
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
    df = synthetic(**DATA.get(name, {}))
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
