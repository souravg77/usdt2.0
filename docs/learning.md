# How the agent learns strategies from books

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
   periods. The last 20% stays untouched. To pass, a strategy needs at least 20 in-sample and
   10 out-of-sample trades, a positive in-sample result, at least +0.05R per trade out of sample,
   a profit factor of at least 1.1 and a drawdown under 15R. Failures are kept with the reasons.
4. **Paper trade.** The `paper` service (`aitrader learn paper`) trades every strategy that
   passed, on paper, using the closed candles the collector stores. It uses the same fill rules
   as the backtest (next open plus slippage, stop first, fees), sizes each trade at the
   strategy's risk on a 10,000 USDT paper account, and respects the economic-calendar filter.
5. **Retire.** After 20 paper trades a strategy with a negative average result is retired, and
   one that loses more than 10R from its peak is retired at once. Retired strategies keep their
   history.

There is no automatic step to real money. The registry has no "live" status; going live stays
behind the Phase 6 go/no-go gate and the three-setting live lock.

## Strategies already read from the books

The six strategy books Shawn sent were read in full, and their 41 rule-based strategies ship
with the code in `src/aitrader/learn/library/`. `learn add-library` registers them, so they
don't need the API key. Carli's CCI divergence is a hand-coded strategy (`cci_divergence`); add
it with `learn add-builtin`. The list and what was approximated are in
[knowledge/rules-from-books.md](knowledge/rules-from-books.md).

Every new book makes the library bigger. Each one is read the same way: notes go in
`docs/knowledge`, strategies become specs, and specs go through the backtest gate and paper
trading. Only strategies with results that hold up survive.

## Commands

```bash
aitrader learn read data/books/my-book.pdf --test   # draft strategies, then backtest them
aitrader learn add-library --test                   # the 41 strategies already read from Shawn's books
aitrader learn add-builtin                          # also run the hand-coded strategies through the gate
aitrader learn test                                 # backtest anything still untested
aitrader learn list                                 # every strategy: status, backtest and paper results
aitrader learn show <id>                            # full rules, sources, results and history
aitrader learn retire <id>                          # stop paper-trading one by hand
aitrader learn paper --once                         # one paper pass (the service runs this every minute)
```

On the VPS, prefix each with `docker compose run --rm collector`, and start the paper service
with `docker compose up -d` (it runs next to the collector).

## What you need

- `ANTHROPIC_API_KEY` in `.env` for `learn read`. Only book text goes to the API. Reading a
  60-page book costs a few cents to a few tens of cents, depending on the model
  (`AITRADER_LLM_MODEL`, default `claude-sonnet-5-5`).
- Enough history for the gate: download at least a year of candles for the timeframes the
  book's strategies use (`aitrader download --since 2024-01-01`).

## Reading the results honestly

- A book strategy that fails the gate is normal. Most published rules don't survive fees.
- Under 100 trades, results are noisy. The paper stage exists because a backtest pass can
  still be luck.
- Read each strategy's `decisions` with `learn show`. They are the AI's guesses where the book
  was vague, and they change results. If one is wrong, say so and the spec can be fixed.
