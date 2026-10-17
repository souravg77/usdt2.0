import numpy as np
import pandas as pd
import pytest

from aitrader.backtest import (
    BacktestConfig, metrics, results_calendar, run_backtest, sensitivity, split_periods, walk_forward,
)
from aitrader.models import Side
from aitrader.strategies import STRATEGIES
from aitrader.strategies.base import Signal, Strategy

H = 3_600_000


def bars(rows):
    """rows: list of (open, high, low, close)."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts"] = np.arange(len(df)) * H
    df["volume"] = 1.0
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


class Fixed(Strategy):
    """Emits pre-set signals at given bar indexes."""

    spec = None

    @classmethod
    def default_params(cls):
        return {"at": [0], "stop": 95.0, "target": 110.0, "trailing": None}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        return [Signal(ts=int(df.ts.iloc[i]) + H, strategy="fixed", exchange=exchange, symbol=symbol,
                       timeframe=timeframe, side=Side.BUY, entry=float(df.close.iloc[i]),
                       stop=p["stop"], target=p["target"], trailing=p["trailing"])
                for i in p["at"]]


FREE = BacktestConfig(taker_fee=0, maker_fee=0, slippage_bps=0)


def test_target_hit_gives_plus_2r_without_costs():
    df = bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 111, 99, 110)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h", FREE)
    assert t.entry == 100 and t.exit_reason == "target" and t.r == pytest.approx(2.0)


def test_stop_wins_when_bar_touches_both():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 111, 94, 100)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h", FREE)
    assert t.exit_reason == "stop" and t.r == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (90, 91, 89, 90)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h", FREE)
    assert t.exit_reason == "stop_gap" and t.r == pytest.approx(-2.0)


def test_entry_is_next_open_not_signal_close():
    df = bars([(100, 100, 100, 100), (102, 103, 101, 102), (102, 111, 101, 110)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h", FREE)
    assert t.entry == 102
    assert t.r == pytest.approx((110 - 102) / 5)  # R measured on planned risk (100 - 95)


def test_costs_reduce_result():
    df = bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 111, 99, 110)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h",
                       BacktestConfig(taker_fee=0.001, maker_fee=0.001, slippage_bps=10))
    assert t.r < 2.0 and t.cost_r > 0
    assert t.r == pytest.approx((110 - 100.1 - 100.1 * 0.001 - 110 * 0.001) / 5)


def test_time_stop_and_no_overlapping_trades():
    rows = [(100, 100.5, 99.5, 100)] * 10
    df = bars(rows)
    trades = run_backtest(Fixed(at=[0, 1, 2, 5], trailing="time_stop=3"), df, "x", "BTC/USDT", "1h", FREE)
    assert [t.exit_reason for t in trades] == ["time", "time"]
    assert trades[0].exit_ts <= trades[1].entry_ts


def test_metrics():
    class T:  # minimal trade stand-in
        def __init__(self, r):
            self.r, self.cost_r = r, 0.1

    m = metrics([T(2), T(-1), T(-1), T(2)])
    assert m["expectancy_r"] == 0.5 and m["win_rate"] == 0.5
    assert m["profit_factor"] == 2 and m["max_drawdown_r"] == 2
    assert not m["reliable"]


def synthetic(n=3000, seed=3):
    rng = np.random.default_rng(seed)
    ret = 0.0003 + 0.01 * rng.standard_normal(n) + 0.004 * np.sin(np.arange(n) / 40)
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.005, n)) * close
    df = pd.DataFrame({"ts": np.arange(n) * H, "open": open_,
                       "high": np.maximum(open_, close) + wick,
                       "low": np.minimum(open_, close) - wick, "close": close,
                       "volume": rng.lognormal(3, 0.5, n)})
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def test_real_strategy_runs_with_calendar():
    df = synthetic()
    trades = run_backtest(STRATEGIES["trend_pullback"](), df, "binance", "BTC/USDT", "1h")
    assert trades
    for a, b in zip(trades, trades[1:]):
        assert a.exit_ts <= b.entry_ts
    cal = results_calendar(trades, "D")
    assert cal["trades"].sum() == len(trades)


def test_split_periods_are_chronological_and_disjoint():
    df = synthetic(1000)
    a, b, c = split_periods(df)
    assert len(a) + len(b) + len(c) == 1000
    assert a.ts.iloc[-1] < b.ts.iloc[0] and b.ts.iloc[-1] < c.ts.iloc[0]


def test_walk_forward_and_sensitivity():
    df = synthetic()
    wf = walk_forward(STRATEGIES["trend_pullback"], df, "binance", "BTC/USDT", "1h",
                      grid={"target_r": [1.5, 2.0]}, train_bars=1200, test_bars=600, min_trades=1)
    assert len(wf) == 3 and set(wf["params"].iloc[0]) == {"target_r"}
    sens = sensitivity(STRATEGIES["momentum_breakout"], df.iloc[:1500], "binance", "BTC/USDT", "15m")
    assert "(base)" in sens["param"].tolist() and len(sens) > 5
