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
    trades = run_backtest(STRATEGIES["trend_pullback"](), df, "bybit", "BTC/USDT", "1h")
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
    wf = walk_forward(STRATEGIES["trend_pullback"], df, "bybit", "BTC/USDT", "1h",
                      grid={"target_r": [1.5, 2.0]}, train_bars=1200, test_bars=600, min_trades=1)
    assert len(wf) == 3 and set(wf["params"].iloc[0]) == {"target_r"}
    sens = sensitivity(STRATEGIES["momentum_breakout"], df.iloc[:1500], "bybit", "BTC/USDT", "15m")
    assert "(base)" in sens["param"].tolist() and len(sens) > 5


def test_parabolic_sar_trailing_exit():
    up = [(100 + i, 101.5 + i, 99.5 + i, 101 + i) for i in range(20)]
    down = [(120 - 2 * i, 120.5 - 2 * i, 117 - 2 * i, 118 - 2 * i) for i in range(6)]
    df = bars(up + down)
    strat = Fixed(at=[3], stop=50.0, target=1000.0, trailing="close<psar")
    (t,) = run_backtest(strat, df, "bybit", "BTC/USDT", "1h", FREE)
    assert t.exit_reason == "trail"
    assert t.exit_ts > int(df.ts.iloc[19]) and t.r > 0


def test_cost_breakdown_splits_fees_and_slippage():
    cfg = BacktestConfig(taker_fee=0.001, maker_fee=0.001, slippage_bps=10)
    df = bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 111, 99, 110)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h", cfg)  # target exit: slippage on entry only
    assert t.fee_r == pytest.approx((100.1 * 0.001 + 110 * 0.001) / 5)
    assert t.slippage_r == pytest.approx(0.1 / 5) and t.funding_r == 0
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 94, 96)])
    [t] = run_backtest(Fixed(), df, "x", "BTC/USDT", "1h", cfg)  # stop: slipped both ways
    assert t.exit_reason == "stop" and t.slippage_r == pytest.approx((0.1 + 95 * 0.001) / 5)
    m = metrics([t])
    assert m["slippage_r_per_trade"] == pytest.approx(t.slippage_r) and m["fee_r_per_trade"] == pytest.approx(t.fee_r)


def test_sharpe_sortino_and_recovery():
    class T:
        def __init__(self, r):
            self.r, self.cost_r = r, 0.0

    r = [1.0, -1.0, -2.0, 1.0, 1.0, 1.0, 2.0]
    m = metrics([T(x) for x in r])
    assert m["sharpe"] == pytest.approx(np.mean(r) / np.std(r, ddof=1))
    assert m["sortino"] == pytest.approx(np.mean(r) / np.sqrt(np.mean(np.minimum(r, 0) ** 2)))
    assert m["max_drawdown_r"] == 3 and m["recovery_trades"] == 3  # bottom after trade 3, back at +1 after trade 6
    assert metrics([T(1), T(-1)])["recovery_trades"] is None  # not recovered yet
    assert metrics([T(1), T(2)])["recovery_trades"] == 0


def test_monte_carlo_is_repeatable_and_bounded():
    from aitrader.backtest import monte_carlo

    r = [1.5, -1.0, -1.0, 2.0, -1.0, 0.5]
    a, b = monte_carlo(r, runs=500, horizon=50, seed=1), monte_carlo(r, runs=500, horizon=50, seed=1)
    assert a == b and 0 <= a["loss_probability"] <= 1 and a["max_drawdown_r_p95"] > 0
    assert a["losing_streak_p95"] >= 2
    win = monte_carlo([0.5, 1.0], runs=200, horizon=30)
    assert win["loss_probability"] == 0 and win["max_drawdown_r_p95"] == 0
    assert monte_carlo([-0.5, -1.0], runs=200, horizon=30)["loss_probability"] == 1


def test_walk_forward_without_a_grid_just_rolls_forward():
    df = synthetic()
    wf = walk_forward(STRATEGIES["trend_pullback"], df, "bybit", "BTC/USDT", "1h",
                      grid={}, train_bars=250, test_bars=550)
    assert len(wf) == 5 and wf["train_score"].isna().all()
