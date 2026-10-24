"""The fundamental overlay: Bybit funding, open interest and premium index as ML inputs."""

import time

import numpy as np
import pandas as pd
import pytest

from aitrader.data import context as ctx_mod
from aitrader.data.context import (
    DAY_MS,
    HOUR_MS,
    ContextStore,
    attach_context,
    collect_context,
    fetch_premium_index,
    mirror_context,
)
from aitrader.strategies import STRATEGIES
from aitrader.strategies.mirror import flip
from aitrader.strategies.ml import FUND_COLUMNS, MlBoostedFund, MlEnsembleFund, fund_frame
from test_context import NOW, FakeUsdm, fake_urlopen


class FakePremium(FakeUsdm):
    """Adds Bybit's premium-index klines (1h, [open time, o, h, l, close, volume])."""

    def __init__(self, start, end, **kw):
        super().__init__(start, end, **kw)
        self.prem = [[t, 0, 0, 0, 0.0002, 0] for t in range(start, end + 1, HOUR_MS)]

    async def fetch_premium_index_ohlcv(self, symbol, timeframe="1h", since=None, limit=None):
        self.calls.append(("premium", symbol, since))
        return [r for r in self.prem if since is None or r[0] >= since][:limit]


@pytest.fixture
def ctx(tmp_path):
    c = ContextStore(str(tmp_path / "c.db"))
    yield c
    c.close()


async def test_premium_index_is_stamped_at_the_hours_close():
    client = FakePremium(NOW - 3 * HOUR_MS, NOW)
    rows = await fetch_premium_index(client, "BTC/USDT", NOW - 3 * HOUR_MS, NOW)
    # the hour that opened at NOW has not finished: left out; the others carry their close time
    assert rows == [(NOW - 2 * HOUR_MS, 0.0002), (NOW - HOUR_MS, 0.0002), (NOW, 0.0002)]
    assert client.calls[0][1] == "BTC/USDT:USDT"


async def test_backfill_fetches_years_of_open_interest(ctx):
    client = FakePremium(NOW - 100 * DAY_MS, NOW)
    added = await collect_context(ctx, ["BTC/USDT"], client=client, urlopen=fake_urlopen(), now_ms=NOW)
    assert added["open_interest"] == 29 * 24 + 1  # the 15-minute top-up keeps a month
    assert added["premium_index"] > 0
    more = await collect_context(ctx, ["BTC/USDT"], client=client, urlopen=fake_urlopen(), now_ms=NOW,
                                 backfill_days=90, history=True)
    assert more["open_interest"] == 90 * 24 + 1  # backfill goes back as far as asked
    first = ctx.load("open_interest", "bybit", "BTC/USDT")[0][0]
    assert first == NOW - 90 * DAY_MS


async def test_futures_database_gets_a_copy(tmp_path, ctx):
    client = FakePremium(NOW - 2 * DAY_MS, NOW)
    await collect_context(ctx, ["BTC/USDT"], client=client, urlopen=fake_urlopen(), now_ms=NOW)
    fut = ContextStore(str(tmp_path / "f.db"))
    n = mirror_context(ctx, fut)
    assert n > 0 and mirror_context(ctx, fut) == 0  # only new rows the second time
    assert fut.load("premium_index", "bybit", "BTC/USDT") == ctx.load("premium_index", "bybit", "BTC/USDT")
    assert not fut.load("btc_dominance")  # the market-wide snapshots stay with Spot AI
    fut.close()


def test_cli_backfill(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AITRADER_DB_PATH", str(tmp_path / "cli.db"))
    now = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    monkeypatch.setattr(ctx_mod, "make_client", lambda: FakePremium(now - 50 * DAY_MS, now))
    monkeypatch.setattr(ctx_mod.urllib.request, "urlopen", fake_urlopen())
    assert main(["--env-file", "none", "context", "backfill", "--symbol", "BTC/USDT"]) == 0
    out = capsys.readouterr().out
    assert "1100 days" in out and "copied to Futures AI's database" in out
    fut = ContextStore(str(tmp_path / "cli_futures.db"))
    assert len(fut.load("open_interest", "bybit", "BTC/USDT")) == 50 * 24 + 1
    fut.close()


def test_attach_context_adds_the_premium_index(ctx):
    ts = np.arange(5) * HOUR_MS + NOW - 10 * HOUR_MS
    df = pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    ctx.upsert([("bybit", "BTC/USDT", "premium_index", int(ts[2]) + HOUR_MS, 0.001)])
    out = attach_context(df, ctx, "bybit", "BTC/USDT", "1h")
    assert np.isnan(out["premium_index"].iloc[1]) and out["premium_index"].iloc[2] == 0.001


def frame(n=1400, seed=0, fund=True):
    rng = np.random.default_rng(seed)
    ts = np.arange(n) * HOUR_MS + 1_600_000_000_000
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({"ts": ts, "open": c, "high": c * 1.005, "low": c * 0.995, "close": c,
                       "volume": rng.uniform(1, 2, n)})
    if fund:
        df["funding_rate"] = rng.normal(0.0001, 0.0001, n)
        df["open_interest"] = np.where(np.arange(n) < 300, np.nan, rng.uniform(1e6, 2e6, n))
        df["premium_index"] = rng.normal(0, 0.0005, n)
    return df


def test_fund_frame_reads_only_the_past():
    df = frame(400)
    full = fund_frame(df, "1h")
    part = fund_frame(df.iloc[:300], "1h")
    assert list(full.columns) == list(FUND_COLUMNS)
    pd.testing.assert_frame_equal(full.iloc[:300], part)
    assert full["funding"].iloc[-1] == pytest.approx(df["funding_rate"].iloc[-1] * 1000)
    assert np.isnan(full["oi_chg1d"].iloc[310]) and np.isfinite(full["oi_chg1d"].iloc[330])


def test_registered_and_need_context():
    for name in ("ml_boosted_fund", "ml_ensemble_fund"):
        s = STRATEGIES[name]()
        assert s.needs_context and s.params["threshold"] == 0.7
    assert "Fundamental overlay" in MlEnsembleFund.spec.setup


@pytest.mark.parametrize("cls", [MlBoostedFund, MlEnsembleFund])
def test_trades_with_fundamentals_and_never_without(cls):
    s = cls(threshold=0.55)
    assert s.generate(frame(), "bybit", "X/USDT", "1h")
    assert not cls(threshold=0.55).generate(frame(fund=False), "bybit", "X/USDT", "1h")


def test_models_only_train_on_candles_that_had_funding():
    df = frame()
    df.loc[:599, ["funding_rate", "premium_index"]] = np.nan  # the first 600 candles have none
    p = MlBoostedFund(threshold=0.55).probabilities(df, "1h")
    assert p.iloc[:600].isna().all()
    # the later models have more than the 400 candles with data they need, so they predict
    assert p.iloc[1300:].notna().any()


def test_a_short_reads_the_premium_the_other_way():
    df = frame(50)
    f = flip(df)
    assert (f["premium_index"] == -df["premium_index"]).all()
    assert (f["open_interest"].fillna(0) == df["open_interest"].fillna(0)).all()


def test_own_exit_rule_name():
    assert MlEnsembleFund().exit_key().startswith("ml_ensemble_fund_down:")
    assert STRATEGIES["ml_ensemble"]().exit_key().startswith("ml_ensemble_down:")
