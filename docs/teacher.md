# Backtest teacher

The teacher turns the backtests into lessons: **in which market situations each strategy made
money, in which it lost, and where taking profit at 1R caught more of its trades.** The idea is
that markets repeat: when the same kind of situation comes back, the strategies that did well in
it before get their signals taken and the ones that lost in it are skipped.

Code: `src/aitrader/learn/teacher.py`. Page: **Teacher** in the dashboard menu.

## What it looks at

Every signal a strategy gave in the backtest is tagged with the market situation on the signal
candle, using the same numbers the AI filter reads (`ai/features.py`):

| Part | Values | From |
|---|---|---|
| Trend | up, range, down | close vs EMA 50 and EMA 200, with ADX above 20 |
| Volatility | low, normal, high | where today's ATR sits among the last 500 (bottom third, middle, top third) |
| Volume | quiet, normal, heavy | volume against its recent average (under 0.8x, 0.8 to 1.5x, over 1.5x) |

That gives 27 situations, for example "uptrend, high volatility, heavy volume".

## What it learns

For each strategy and situation, using the **first 70%** of the strategy's backtest signals:

- **trade**: at least 20 signals and a positive result per trade after fees and slippage. The
  result is shrunk toward zero (10 trades of 0R are mixed in), so a handful of lucky trades
  can't make a lesson.
- **avoid**: at least 20 signals and a negative shrunk result.
- **unproven**: fewer than 20 signals. Nothing changes for these signals.
- **1R profit-take**: the same signals replayed with a take-profit at 1R (the planned entry plus
  the stop distance). If that beat the strategy's own exit by at least 0.02R per trade, the
  lesson says to use it there. This is what raises the win rate: more trades close green. It is
  kept only where it also raised the result per trade, because a higher win rate with smaller
  wins can lose money.

## How it is checked

The lessons are then followed on the **last 30%** of the signals, which they never saw, and
compared with taking every signal. They can be turned on only when following them gave a better
result per trade there, over at least 30 trades. The newest 20% of each market's history is never
used at all: it stays untouched for the go-live gate.

## Using it

- Dashboard: **Teacher** page, **Teach from the backtests**. It runs as a job, then shows the
  result on unseen data, the best lessons, the situations to avoid, and for each pair its
  situation right now with the strategies proven in it.
- **Turn on** appears only when the lessons passed the check. Then paper trading:
  - skips a strategy's signal when its lesson for the current situation is "avoid",
  - adds the 1R take-profit where the lesson says so,
  - takes every other signal as before.
- **Turn off** goes back to every signal at once.
- CLI: `aitrader learn teach`, `aitrader learn teach-on --by <name>`, `aitrader learn teach-off`.
  `learn paper --no-teacher` ignores the lessons.
- The installer schedules a fresh lesson every Sunday at 04:15. It is only a candidate: paper
  trading keeps following the lessons you turned on until you turn on the new ones.

Live trading never reads the lessons. A live strategy is frozen by the go-live approval.

## Improving the strategies

The Teacher page also has **Improve the paper strategies** (CLI: `aitrader learn improve [ids]`;
code: `src/aitrader/learn/improve.py`). It tries different settings for every strategy that is
paper trading and keeps a change only when the change still wins on data the search never saw:

1. **What it may change.** For hand-coded strategies: their numbers (indicator lengths such as
   EMA, RSI or Donchian periods, thresholds such as ADX or RSI levels, stop and target sizes).
   For book rules: the stop (ATR multiple and length, percent, or lowest-low length), the target
   in R and the time stop. It never touches risk per trade, the volatility cap or other safety
   settings. Short twins are tuned through their long rules.
2. **Search on the older 60%.** Each number is moved 25% down and up, one at a time, and the
   best move is kept. Then it searches again from there, up to 30 backtests per strategy. A
   setting must give at least 30 trades, and results are shrunk toward zero, so a rare lucky
   setting doesn't win.
3. **Check on the next 20%**, which the search never saw. The new settings have to make money
   there, and beat the current settings by at least 0.03R per trade, over at least 30 trades.
4. **Full gate.** The improved version is registered as a new strategy, `<id>~<code>`, named
   "... (improved)". It goes through the same backtest gate as any new strategy: in sample, out
   of sample, walk-forward, Monte Carlo, and last the untouched newest 20%. Only if it reaches
   paper trading is the old version retired ("replaced by the improved version"). Otherwise
   nothing changes.

The page lists every strategy it worked on: how many settings it tried, what changed, the result
per trade and win rate on the unseen data before and after, and **IMPROVED** where the new version
replaced the old one. Live trading is never changed: an improved version starts on paper and needs
its own go-live approval.

Trying many settings is how strategies get "curve-fitted": the settings fit the past and fail
later. The unseen 20%, the full gate and the newest 20% are there to catch that. They lower the
risk; they don't remove it.

## Limits

- A situation that paid in the past can stop paying. The check on unseen data lowers that risk;
  it doesn't remove it. Nothing here promises a profit.
- The more situations and strategies there are, the more lessons look good by chance. That is
  why every lesson needs 20 signals, results are shrunk toward zero, and the whole set has to
  beat the plain strategies on data it never saw.
- The AI trade filter ([ai-filter.md](ai-filter.md)) learns from the same kind of information
  with a model. The teacher is the simple version you can read line by line. Both can be on at
  once: a signal has to pass both.
