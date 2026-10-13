# Strategy promotion ladder

Every strategy the agent knows sits on one rung of this ladder (master prompt, section 11):

```
RESEARCH -> BACKTESTED -> OUT-OF-SAMPLE VALIDATED -> PAPER -> SHADOW MONITORED
         -> LIVE-ELIGIBLE -> LIVE ENABLED WITH EXPLICIT APPROVAL

off the ladder: REJECTED, UNDER REVIEW, RETIRED
```

A strategy moves up one rung only when it passes that rung's test. Each test and its numbers are
below. They were written into the code (`learn.lab.Gate`, `learn.paper.RetireRule`,
`trade.golive.GateConfig`) before any strategy was run against them, and are not changed to let
a particular strategy through. Passing every rung is evidence that a strategy held up on the data
it was tested on. It is not a promise that it will make money.

The dashboard's strategy list and `aitrader learn list` show each strategy's rung.

## Where each rung is kept

| Rung | Kept in | Set by |
|---|---|---|
| RESEARCH, BACKTESTED, OUT-OF-SAMPLE VALIDATED, PAPER, REJECTED, UNDER REVIEW, RETIRED | the strategy registry (`data/learn/registry.json`, field `status`) | the backtest gate (`learn test`), the paper service, `learn retire`, `learn paper-all` |
| SHADOW MONITORED | counted from `data/learn/paper_state.json` | the paper service |
| LIVE-ELIGIBLE | read from `data/golive/report.json` | the go/no-go gate (`golive check`) |
| LIVE ENABLED | read from `data/golive/approval.json`, checked by `GoLive.guard` | only you, with `golive approve --strategy <id> --by <name>` at a terminal, typing the confirmation phrase |

The top three rungs are never written into the registry: the registry refuses those statuses.
So editing the registry can't make a strategy look approved, and the only way to LIVE ENABLED is
the existing human approval in `trade/golive.py`. Even then, real orders also need the live lock
in `.env` to be opened by you, and the pilot limits ([live-trading.md](live-trading.md)).

## The rungs and their tests

All results are in R (the amount risked per trade) after modelled costs: 0.1% fee per side and
5 bps slippage per slipped fill by default. History for each market is split in time order:
first 60% in sample, next 20% out of sample, last 20% the final holdout. Trades are pooled over
every stored market (exchange, pair, timeframe) with at least 300 bars.

### RESEARCH

A strategy written as rules the rule language accepts (long spot entries with a stop, risk at
most 1% per trade, no leverage, no shorting), or a hand-coded built-in. It stays here until there
is stored price history for its timeframes.

### BACKTESTED (in sample)

- at least 50 in-sample trades
- in-sample expectancy above 0R per trade

Fail: REJECTED.

### OUT-OF-SAMPLE VALIDATED

Out of sample:

- at least 30 trades
- expectancy at least +0.05R per trade
- profit factor at least 1.1
- t-statistic of the average at least 2 (dozens of strategies are tested, so some look good by luck)
- maximum drawdown at most 15R

Walk-forward over rolling windows: the first 80% of each market (never the holdout), after a
250-bar warm-up, is cut into 6 windows. Each window runs on its own as unseen data, with the
warm-up before it so indicators are ready. Windows are pooled across markets by number. A window
with fewer than 5 trades is too small to count. Needed: at least 4 counted windows, and at least
60% of them (4 of 6, 3 of 5, 3 of 4) above 0R per trade. Nothing is tuned between windows; the
strategy's rules are fixed.

Monte Carlo stress test of the out-of-sample trade sequence: 100 trades (the size of the paper
stage) drawn at random with replacement from the out-of-sample results, 2000 times, seed 7, so the
numbers repeat. Needed:

- 95th-percentile maximum drawdown at most 15R (19 runs in 20 stay inside the out-of-sample limit)
- chance that the 100 trades end below zero at most 15%

The report also gives the 95th-percentile longest losing streak, for information.

Fail on any of these: REJECTED.

### PAPER (final holdout, then paper trading)

The final 20% of history is run only after everything above has passed, and only to confirm:

- at least 20 trades
- expectancy above 0R per trade

Pass: the strategy is paper-traded on live candles by the paper service. Fail: UNDER REVIEW. The
holdout result is stored either way (`learn show <id>`, field `backtest.holdout`), with `looks`:
how many times it has been run. A holdout that has been looked at more than once is less
independent than the first look, and the count says so.

While on paper, a strategy is RETIRED when its paper drawdown passes 10R, or when it has 20 paper
trades and its average is below 0R.

### SHADOW MONITORED

The strategy has at least 30 closed paper trades on real Bybit prices (the same count the go/no-go
gate asks of each strategy). The dashboard shows its paper average in R next to its out-of-sample
backtest, so a strategy whose paper results drift away from the backtest stands out. (This rung
used to mean trades on Bybit demo trading, which was removed because it isn't offered in every
country.)

### LIVE-ELIGIBLE

The Phase 6 go/no-go gate (`aitrader golive check`) passed with this strategy selected:

- at least 100 paper trades in all, and for every selected strategy at least 30 paper trades and
  a result above 0R by itself
- paper expectancy above 0R
- passed the backtest gate above (including the holdout) with out-of-sample expectancy above 0R
- paper drawdown at most 10R (the live loss limit)
- paper and backtest expectancy within 0.15R of each other
- 14 days of paper service without a crash or a heartbeat gap over 30 minutes
- trade-only API keys confirmed, and the kill switch tested within 30 days

Paper trading fills orders on this server, so the first real orders are the live pilot's: start
it with a small budget.

### LIVE ENABLED WITH EXPLICIT APPROVAL

You run `aitrader golive approve --strategy <id> --by <name>` for one or two strategies after reading the
gate report and the risk settings. The approval re-runs the gate and is refused if it fails. It
then asks you to type `I approve <ids> for real-money trading` with the strategy ids; anything
else, no terminal, or piped input means no approval, and there is no flag that skips the
question. The approval file is sealed, so a hand-written or edited one counts for nothing. It is
tied to the strategies' rules and the active AI model: if either changes, the strategy drops back
to LIVE-ELIGIBLE or PAPER on the ladder and the live executor stops entering for it. The
live monitor sends trading back to paper when live drawdown passes 10R or live results fall more
than 0.3R per trade below paper after 20 live trades. Live trading stays off by default.

### REJECTED

Failed the in-sample or out-of-sample part of the gate (including walk-forward and Monte Carlo).
Kept with the reasons. `learn test --retest` runs the gate again, for example after more history
was downloaded.

### UNDER REVIEW

Passed everything up to the holdout, then failed the holdout. It is not paper-traded on its own.
A person decides: retire it, or paper-test it anyway with `learn paper-all`. A strategy that
failed any part of the gate, the holdout included, is never selected for the go/no-go gate.

### RETIRED

Paper results fell below the bar, or retired by hand with `learn retire`. It keeps its history.

## Registry files from before the ladder

Older registry files used four statuses. They are read without any manual step:

| Old status | Read as |
|---|---|
| `candidate` | `research` |
| `rejected` | `rejected` |
| `paper` | `paper` (PAPER) |
| `retired` | `retired` |

The new name is written back the next time the registry is saved. History entries keep the names
they were written with. A strategy that passed the old gate keeps its PAPER status; its stored
backtest has no walk-forward, Monte Carlo or holdout result until it is tested again
(`learn test <id>`).

`learn paper-all` (the dashboard's "turn paper trading on") still moves untested, rejected and
under-review strategies to paper so they can be watched. The ladder shows those as PAPER
with the note "did not pass the backtest gate", and they can't go further up.

## How the ladder is tested

`tests/test_promotion.py` checks that there are exactly these seven rungs in order
(RESEARCH, BACKTESTED, OOS_VALIDATED, PAPER_DEMO, SHADOW_MONITORED, LIVE_ELIGIBLE, LIVE_ENABLED;
PAPER_DEMO is the PAPER rung, run on real Bybit prices on this server), that the registry refuses
the top three, that one strategy climbs every rung in turn and only with the evidence each needs,
and that LIVE ENABLED can't be reached without the typed phrase: wrong phrases, a phrase for other
strategies, no terminal, piped input, unknown flags and forged or edited approval files are all
refused.
