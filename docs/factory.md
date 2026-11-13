# Strategy Factory, evolution, luck correction and stress test

These four pieces let the agent **invent strategies itself** instead of only reading them from books,
without letting it fool itself. They come from the NEXUS roadmap
(Discover → Generate → Evolve → Backtest → Challenge → Validate → Paper). Nothing here places an
order or changes the live lock. Every new strategy starts at RESEARCH on the
[promotion ladder](promotion-ladder.md).

Code: `src/aitrader/learn/factory.py`, `evolve.py`, `luck.py`, `stress.py`.

## 1. Strategy Factory (`aitrader learn factory`)

The factory writes rule strategies from building blocks the [rule language](learning.md) already has.
Each strategy is a *genome*:

| Part | How many | Blocks |
|---|---|---|
| trend | 0 or 1 | close above EMA(50/100/200), EMAs stacked up, Supertrend up, ADX with +DI above -DI, above the Ichimoku cloud, higher highs |
| trigger | 1 | RSI, Stochastic, CCI, Williams %R or MFI crossing up; MACD histogram crossing 0; Bollinger breakout or reclaim; N-bar breakout; VWAP or EMA reclaim; FVG retest; order-block tap; liquidity sweep; engulfing; hammer; RSI divergence |
| confirm | 0 or 1 | volume surge, CMF above 0, OBV above its average, green Heikin-Ashi, strong candle body |
| volatility | 0 or 1 | ATR not extreme, Bollinger squeeze, price stretched below its average |
| stop | 1 | ATR multiple or lowest low of N bars |
| exit | 1 | target in R or a trailing exit, always with a time stop |
| timeframe | 1 | 15m, 30m, 1h or 4h |

Filters that clash with the trigger are not paired with it (for example a squeeze filter with a
dip-buying trigger). The genome becomes an ordinary rule spec that `validate_spec` checks, so the
factory can only write what the safe rule language allows: long entries with a stop. Futures AI
trades them short through their short twins (`--shorts`).

```bash
aitrader learn factory --n 20 --timeframes 1h,4h --shorts
aitrader learn test                      # Spot AI
aitrader learn test --market futures     # the short twins
```

At most 2 strategies of one family are made, so one lucky idea can't fill the list.

## 2. Evolution (`aitrader learn evolve`)

Evolution breeds children from the factory strategies with the best **in-sample** result (R per trade
after costs, shrunk toward 0 by 20 trades of 0R, with at least 30 trades). Out-of-sample,
walk-forward and holdout results are **never** used to choose parents. If they were, the children
would be fitted to the data that is meant to judge them.

- **Mutation:** one change. A number moves to a neighbouring value, a block is swapped for another of the same kind, a filter is added or dropped, or the stop, exit or timeframe changes.
- **Crossover:** each part comes from one of two parents.

Children record their parents, how they were made and their generation. Parents are never changed.

```bash
aitrader learn evolve --n 20 --shorts
aitrader learn test
aitrader learn lineage <id>              # the family tree
```

## 3. Luck correction (`aitrader learn luck`)

The normal gate asks for an out-of-sample t-statistic of at least 2. Luck gets past that about 1 time
in 40. That is fine for a strategy written down from a book before it was tested. It is not fine
for a machine that tries thousands of ideas, where about 25 in 1,000 pass by luck alone.

For machine-made strategies the bar rises with the number of **families** tried. A family is the
same blocks, stop kind, exit kind and timeframe; strategies that differ only in their numbers count
once. The bar allows a 5% chance that any of them gets through by luck (Bonferroni):

| Families tried | 1 | 10 | 50 | 100 | 500 | 1000 | 5000 |
|---|---|---|---|---|---|---|---|
| t needed | 2.00 | 2.58 | 3.09 | 3.29 | 3.72 | 3.89 | 4.26 |

A family counts from the moment it is made, tested or not, so leaving losers untested can't lower
the bar. A short twin is its own family. Book and hand-coded strategies keep the plain bar of 2.

**What this means in practice:** wide random search gets expensive fast. Make a modest batch, then
let evolution refine the best ones: number-only mutations stay inside their family and don't raise
the bar.

## 4. Stress test

Machine-made strategies that pass out of sample, walk-forward and Monte Carlo are then attacked
before the final holdout is looked at. All scenarios use the out-of-sample part only:

| Scenario | What changes | Passes when |
|---|---|---|
| harsh costs | fees x2, slippage x3, funding x3, same trades re-run | above 0R per trade |
| stop gaps | every 4th stop-out loses 0.5R more (crash, thin book) | above 0R per trade |
| no best trades | the best 5% of trades removed (a lucky pump) | above 0R per trade |
| no best market | the pair and timeframe that made most removed | above 0R per trade |
| losing streak | Monte Carlo of 100 trades | at most 12 losses in a row in 19 of 20 runs |

A failure rejects the strategy with the reason. All numbers are fields of `learn.lab.Gate`, fixed
before any strategy runs. The stress test and luck bar only ever add reasons to reject; they can't
pass anything the normal gate would fail.

## Where results show

`learn test` prints each result as before. The saved backtest now has `luck`
(`families_tried`, `min_t_oos`) and `stress` (each scenario's numbers) for machine-made strategies.
`learn show <id>` prints the full record.

## Not built (on purpose)

The NEXUS roadmap also suggests reinforcement learning and a rewrite onto Postgres, TimescaleDB,
Celery and Redis. Neither would make the results more trustworthy, and both would put the
working platform at risk, so they are left out.
