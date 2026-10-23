# How the agent learns strategies from books

> **Reading new books is turned off.** `learn read` used Claude through the Anthropic API, which
> the platform no longer uses; the local model is too small for it. Everything after the read step
> (the backtest gate, paper trading, keep or retire) still runs, and `learn add-library` registers
> the strategies already read from the supplied books.

```
book (PDF) ──read──> draft strategies ──backtest gate──> paper trading ──results──> keep or retire
              Claude      rule specs          lab              paper loop
```

1. **Read.** `aitrader learn read book.pdf` sends the book's text, page by page, to Claude
   with a description of the rule language. Claude writes each strategy the book describes as
   a rule spec: entry conditions, stop, target and exits, the page and quote it came from, the
   choices it had to make where the book is vague (`decisions`), and what it couldn't code
   (`uncodable`: news, fundamentals, hand-drawn patterns, shorting, leverage).
2. **Check.** Every spec is validated against a whitelist of indicators, operators and number
   ranges ([`learn/rules.py`](../src/aitrader/learn/rules.py)). Nothing in a spec is run as
   code. The language can only describe long spot trades with a stop, risk is capped at 1% per
   trade, and anything else is rejected. Broken specs get one repair round, then are dropped.
3. **Backtest.** `aitrader learn test` runs each new strategy on every stored market for its
   timeframes, with fees and slippage, on the in-sample (first 60%) and out-of-sample (next 20%)
   periods. To pass, a strategy needs at least 50 in-sample and
   30 out-of-sample trades, a positive in-sample result, at least +0.05R per trade out of sample,
   an out-of-sample average at least 2 standard errors above zero (t >= 2: dozens of strategies
   are tested, so some look good by luck), a profit factor of at least 1.1 and a drawdown under
   15R. It must also hold up walk-forward (most of 6 rolling unseen windows above 0R after costs)
   and in a Monte Carlo stress test of its out-of-sample trades (95th-percentile drawdown over 100
   trades at most 15R, at most a 15% chance those 100 trades end below zero). Only then is the
   untouched last 20% run, once, as a final holdout (at least 20 trades, above 0R); a strategy
   that fails only the holdout goes to "under review" instead of paper. Every number, and the
   rung each test leads to, is in [promotion-ladder.md](promotion-ladder.md). Failures are kept
   with the reasons. "Turn paper trading on" on the dashboard (or
   `learn paper-all`) also paper-trades the ones that failed, but only strategies that passed
   this gate can be approved for live, and each one needs its own paper record (at least 30
   trades, above 0R) on top of the combined go/no-go numbers.
   After that, only the best 10 to 20 keep paper trading (`learn keep-top --n 15`, or "Keep only
   the best" on the Trading page; the number is remembered). They are ranked with the ones that
   passed the backtest first, then by result per trade out of sample plus paper, shrunk toward 0
   when there are few trades (30 trades of 0R are mixed in). The rest stop opening trades (open
   ones still close) and are retired with the reason "not in the best N"; each ranking looks at
   them again, so one can come back. The Markets chart starts with the indicators the kept
   strategies use.
4. **Paper trade.** The `paper` service (`aitrader learn paper`) trades every strategy that
   passed, on paper, using the closed candles the collector stores. It uses the same fill rules
   as the backtest (next open plus slippage, stop first, fees), sizes each trade at the
   strategy's risk on a 10,000 USDT paper account, and respects the economic-calendar filter.
5. **Retire.** After 20 paper trades a strategy with a negative average result is retired, and
   one that loses more than 10R from its peak is retired at once. Retired strategies keep their
   history.

There is no automatic step to real money. The registry has no "live" status; going live stays
behind the Phase 6 go/no-go gate, your approval and the three-setting live lock. Where each
strategy stands on the way there is its rung on the [promotion ladder](promotion-ladder.md).

## Strategies already read from the books

The eight strategy books Shawn sent were read in full, and their 54 rule-based strategies ship
with the code in `src/aitrader/learn/library/`. `learn add-library` registers them, so they
don't need the API key. Carli's CCI divergence is a hand-coded strategy (`cci_divergence`); add
it with `learn add-builtin`, which also adds the twelve untested strategy-family candidates
listed in [strategies.md](strategies.md). The list and what was approximated are in
[knowledge/rules-from-books.md](knowledge/rules-from-books.md).

Every new book makes the library bigger. Each one is read the same way: notes go in
`docs/knowledge`, strategies become specs, and specs go through the backtest gate and paper
trading. Only strategies with results that hold up survive.

## Commands

```bash
aitrader learn read data/books/my-book.pdf --test   # draft strategies, then backtest them
aitrader learn add-library --test                   # the 54 strategies already read from Shawn's books
aitrader learn add-builtin                          # also run the hand-coded strategies through the gate
aitrader learn test                                 # backtest anything still untested
aitrader learn list                                 # every strategy: ladder rung, backtest and paper results
aitrader learn show <id>                            # full rules, sources, results and history
aitrader learn retire <id>                          # stop paper-trading one by hand
aitrader learn paper --once                         # one paper pass (the service runs this every minute)
```

On the VPS, prefix each with `docker compose run --rm collector`, and start the paper service
with `docker compose up -d` (it runs next to the collector).

## What you need

- `learn read` is turned off: it needed Claude through the Anthropic API, which the platform no
  longer uses, and the local model (the `ollama` service) is too small to write valid rules from a
  book. `learn add-library` still registers the strategies already read from the supplied books.
- Enough history for the gate: download at least a year of candles for the timeframes the
  book's strategies use (`aitrader download --since 2024-01-01`).

## Reading the results honestly

- A book strategy that fails the gate is normal. Most published rules don't survive fees.
- Under 100 trades, results are noisy. The paper stage exists because a backtest pass can
  still be luck.
- Read each strategy's `decisions` with `learn show`. They are the AI's guesses where the book
  was vague, and they change results. If one is wrong, say so and the spec can be fixed.
