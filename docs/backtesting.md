# Backtest lab (Phase 3)

```bash
# 1. get enough history (4h needs years; 15m needs months)
aitrader download --since 2021-01-01 --exchange binance --timeframe 4h --timeframe 1h
# 2. backtest on in-sample and out-of-sample periods
aitrader backtest --strategy trend_pullback --timeframe 4h
aitrader backtest --strategy momentum_breakout --timeframe 15m --symbol BTC/USDT
aitrader backtest --strategy buy_the_dip --timeframe 1d
# 3. only after you've stopped changing rules: one run on the untouched final period
aitrader backtest --strategy trend_pullback --timeframe 4h --final
```

With Docker, prefix each command with `docker compose run --rm collector`.

## How fills are simulated

- Entry at the **next** bar's open plus slippage (default 5 bps), after the signal bar closes.
- A stop fills at the stop minus slippage. If the price gaps through it, the fill is at the
  open. If one bar touches both stop and target, the **stop** counts.
- Targets fill at the target as maker. Trailing and time exits fill at the next open.
- Fees default to 0.1% per side (Binance spot base rate). Use `--funding-8h 0.0001` to charge
  perpetual funding.
- Only one trade per strategy and symbol is open at a time.
- Results are in **R**, the net profit divided by the planned risk (entry minus stop), after all
  costs.

## Splits

Data is split by time into 60% in-sample, 20% out-of-sample and 20% final test. Tune only on the
in-sample part. `--final` runs the final period; treat that result as the one that counts.

Walk-forward (`aitrader.backtest.walk_forward`) optimises on a training window, tests on the next
window and rolls forward. `sensitivity` moves every parameter by ±20% to show whether a result
depends on exact values. `monte_carlo` redraws a trade sequence many times with a fixed seed and
gives the 95th-percentile drawdown and the chance a run of trades ends below zero. The backtest
gate for agent-drafted strategies uses all three steps, plus the final holdout, in the order and
with the thresholds in [promotion-ladder.md](promotion-ladder.md).

Besides expectancy, win rate, profit factor and drawdown, `metrics` reports per-trade Sharpe and
Sortino (in R, not annualised: trades are irregular in time), the trades it took to recover from
the worst drawdown, and the costs per trade split into fees, slippage and funding.

## Rejection rules (from the roadmap)

Drop a strategy if:

- its expectancy is not positive after costs,
- it has fewer than about 100 trades,
- it only works on one coin or one period,
- or its result disappears when parameters move a little.

The goal of Phase 3 is a short list of 2-4 strategies that pass.
