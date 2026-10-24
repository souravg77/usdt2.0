# Phase 4: the AI trade filter

The filter sits on top of the strategies that already passed the backtest gate. For each signal
it estimates the probability that price reaches **1R above the entry before it hits the stop**.
Below a threshold the signal is skipped. Above it, the position size grows with confidence,
from half the strategy's normal risk up to the full amount. It never goes above the strategy's
own risk, which is already capped at 1%. The filter does not invent trades and cannot open one
that a strategy did not signal.

## Spot AI and Futures AI each have their own

Nothing here is shared between the two engines:

| | Spot AI | Futures AI |
|---|---|---|
| AI filter models | `data/ai/models` | `data/ai/models_futures` |
| Training set | `data/ai/dataset.csv` | `data/ai/dataset_futures.csv` |
| Backtest teacher | `data/learn/teacher` | `data/learn/teacher_futures` |
| Learns on | spot candles, long strategies, Spot AI's paper trades | the perpetuals' own candles (perp fees and funding), longs and short twins, Futures AI's paper trades |

Each is trained, turned on and turned off in its own view on the dashboard, or with `--market futures`
on the command line (`aitrader ai --market futures retrain`, `aitrader learn teach --market futures`).
The ML strategies (`ml_boosted`, `ml_classifier`) already train on whichever engine's candles they
trade, so their models are separate too. The models made before this split stay Spot AI's.

## What it learns from

`aitrader ai dataset` backtests the strategies and writes one row per signal to
`data/ai/dataset.csv`:

- By default it uses the strategies with status `paper` or `retired` in `learn list`, meaning the
  ones that passed the backtest gate. `--all` adds every built-in and every rule.
- It uses every stored market (exchange × symbol × the strategy's timeframes).
- It uses only the in-sample and out-of-sample periods. The final 20% of each market's history
  stays untouched for the Phase 6 go/no-go test. `--include-final` overrides this; don't use it
  before Phase 6.
- Closed paper trades from the `paper` service are added as `source = paper`.

Every signal is simulated with the backtest's fill rules: next-bar open, fees, slippage, stop
first, and gaps. `taken` marks the signals the strategy would really trade, since it holds one
position per market at a time.

### Features (roadmap Phase 4, step 2)

| Roadmap item | Columns |
|---|---|
| Strategy name, timeframe | `strategy`, `timeframe` (categorical) |
| Market regime | `regime` (+1 up trend, −1 down trend, 0 range: close and EMA50 vs EMA200, ADX > 20), `dist_ema200` |
| ATR percentile | `atr_pctile` (500 bars), `atr_pct` |
| Trend strength | `adx`, `di_spread`, `trend_slope` (EMA50 over 10 bars, in ATRs), `ret_24` |
| Funding rate, open-interest change | `funding_rate`, `oi_change`: **empty for now**, see below |
| Time of day | `hour_sin`, `hour_cos`, `weekday` |
| Distance to key levels | `dist_high50`, `dist_low50` (in ATRs), `range_pos200`, `dist_round` (distance to the nearest round number) |
| Recent win/loss streak | `streak`, `recent_r` (the strategy's last trades that had closed before the signal) |
| Also | `rsi`, `rel_volume`, `stop_atr` (stop distance in ATRs), `target_r` |

All features use only bars up to the signal bar. A test checks this by recomputing them on
truncated history.

## How a model is trained and tested (steps 3 to 5)

The model is gradient boosting: scikit-learn's `HistGradientBoostingClassifier`. It uses the
same histogram method as LightGBM and needs no extra system libraries in the Docker image.

Signals are split by time, never shuffled:

| Part | Share | Used for |
|---|---|---|
| Train | oldest 60% | fitting the model. Trades still open when validation starts are dropped, so no later outcome leaks in |
| Validation | next 20% | choosing the threshold: the probability cut that gives the best expectancy, keeping 30 to 100% of signals |
| Test | newest 20% | the comparison below. Nothing is tuned on it |

The model tested is the one that ships. It is not refit afterwards.

On the test period, three versions trade one position per strategy and market:

1. The unfiltered rules.
2. The rules with the new filter.
3. The rules with the active filter, if there is one.

When the filter skips a signal, the slot stays free for the next one, just as in live trading.
A new model only becomes a **candidate** when all of these hold:

- the unfiltered rules made at least 30 trades and the filtered rules at least 15;
- the filtered rules beat the unfiltered ones: expectancy at least +0.02R per trade higher, or
  drawdown at most 90% of theirs with expectancy no worse;
- filtered expectancy is above zero.

Otherwise the model is **rejected**: it is kept on record but can never be used. This is the
roadmap's rule: "If the filter does not beat the baseline out of sample, do not use it."

## Promotion and rollback (step 6)

Every model is saved in `data/ai/models/<version>/` with `model.pkl`, `report.json` and
`report.md`. The version is the UTC training time, for example `m20261101-030000`.

| Command | What it does |
|---|---|
| `aitrader ai retrain` | builds the training set, trains and tests. Adds a candidate or a rejected version. **Never promotes** |
| `aitrader ai list` | every version with its status and test results |
| `aitrader ai report [VERSION]` | the side-by-side report: unfiltered vs new vs active, per strategy, feature importance |
| `aitrader ai promote VERSION` | shows the report and asks you to type the version. Only candidates can be promoted. Records who approved it |
| `aitrader ai rollback` | at once, makes the previous model active again (or turns the filter off if there was none) |
| `aitrader ai off` | takes every signal at full size again. Nothing is deleted |
| `aitrader ai score --strategy ID --symbol BTC/USDT --timeframe 4h` | the probability and decision for a strategy's recent signals |

The `paper` service reads the registry on every tick. A promotion or rollback takes effect on
the next tick without a restart. For each signal the paper log records the probability and the
model version, and it keeps a list of the signals the filter skipped. `learn paper --no-ai`
ignores the filter. If the model file fails its checksum or cannot be loaded, the paper trader
logs the error and trades unfiltered, rather than stopping.

Retrain monthly with the cron line in [ubuntu-setup.md](ubuntu-setup.md#7-train-the-ai-trade-filter-phase-4).

## What is not built yet

- **Funding rate and open interest.** Nothing collects them yet; that is still on the Phase 2
  list. The columns exist and the model ignores them while they are empty. Once the collector
  stores them, the next retrain uses them.
- **The LLM layer for news and events.** The roadmap adds it "only for news and event context".
  There is no news or crypto-event feed yet. The economic-calendar filter already blocks or
  shrinks entries around scheduled events.
- **A trained model.** Training needs real backtest results from the VPS. In this repository the
  filter has only been run on synthetic prices, where it behaves as expected: it finds an edge
  planted in the data and rejects data that has none.

## Reading the report honestly

- Under about 100 test trades the comparison is noisy. Treat a small win as "not proven".
- A filter that keeps very few signals can look great by luck. The 15-trade minimum is a floor,
  not a target.
- The active model's column may include data it was trained on, which flatters it. The report
  says so.
- The AUC tells you whether the probabilities rank signals better than chance (0.5). The
  expectancy and drawdown rows tell you whether that ranking actually makes money after costs.
