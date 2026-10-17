# Phases 5 and 6: testnet, alerts, the go/no-go gate and the live pilot

The paper service (`learn paper`) decides every trade. The **executor** copies those decisions to
an exchange with real orders: first on the Binance Spot **testnet** (fake money), and only after
the go/no-go gate and your approval, on your real account with a small **pilot** budget.

Nothing in this document happens on its own. Each step that moves closer to real money is a
command you run on the VPS.

## How the pieces fit

| Service (docker compose) | What it does | Starts with |
|---|---|---|
| `collector` | keeps candles up to date | `docker compose up -d` |
| `context` | funding, open interest, total market cap ([market-context.md](market-context.md)) | `docker compose up -d` |
| `paper` | finds signals, applies the calendar, market-context and AI filters, paper-trades them | `docker compose up -d` |
| `web` | dashboard, including the Trading page with the kill switch | `docker compose up -d` |
| `executor-testnet` | copies paper trades to the Binance testnet | `docker compose --profile testnet up -d executor-testnet` |
| `telegram` | alerts, daily summary, weekly report, `/status /pause /resume /kill` | `docker compose --profile telegram up -d telegram` |
| `executor-live` | the live pilot (real money) | only after step 6 below |

Every service writes to one event log (`data/ops/events.jsonl`): trades, errors, disconnects,
risk pauses, the kill switch and a heartbeat every 5 minutes. The dashboard shows it, Telegram
sends it, and the go/no-go gate reads it to judge stability.

## Risk engine

Every executor order is sized and checked here. Strategies can't switch it off. Settings in
`.env`:

| Setting | Default | Meaning |
|---|---|---|
| `AITRADER_RISK_PER_TRADE` | 0.005 | risk per trade (0.5% of equity); never above 1%, whatever you set |
| `AITRADER_DAILY_LOSS_PCT` | 0.02 | realised loss today (UTC) that pauses trading |
| `AITRADER_WEEKLY_LOSS_PCT` | 0.05 | realised loss this week that pauses trading |
| `AITRADER_MAX_OPEN_POSITIONS` | 3 | most positions at once |
| `AITRADER_MAX_COIN_EXPOSURE_PCT` | 0.25 | most of equity in one coin |
| `AITRADER_MAX_TOTAL_EXPOSURE_PCT` | 0.9 | most of equity invested (spot, no leverage) |
| `AITRADER_MIN_ORDER_USDT` | 10 | smaller orders are skipped, never rounded up |

Size comes from the stop distance: `units = equity × risk × filter size ÷ (entry − stop)`, then
cut to the exposure limits and to the cash available. A loss-cap pause stays on until you resume.

## Pause and kill switch

- **Pause** stops new entries everywhere (paper and executors). Open positions are still managed.
- **Kill switch** also makes the executors sell every position they opened, at market. It never
  touches coins the executor didn't buy.
- Both stay on until a person resumes.

Three ways to use them:

```bash
docker compose run --rm collector trade pause --reason "news"   # or: trade resume
docker compose run --rm collector trade kill --confirm
```

The dashboard's **Trading** page has the same buttons (the kill switch needs you to type `KILL`),
and so does Telegram (`/pause`, `/resume`, `/kill CONFIRM`).

## Step 1: testnet keys and the testnet executor

Follow the Binance guide for testnet keys and put them in `.env` (`BINANCE_TESTNET_API_KEY`,
`BINANCE_TESTNET_API_SECRET`). Then:

```bash
docker compose --profile testnet up -d executor-testnet
docker compose logs -f executor-testnet
```

The service runs with `AITRADER_MODE=testnet`, so it can only reach the testnet. When the paper
service opens a position on Binance, the executor buys it on the testnet within about 15
seconds, sized by the risk engine. It sells when the paper trade closes, or at once if the testnet
price is at or below the stop. Each fill is compared with its paper twin, so slippage is
measured.

How failures are handled:

- Every order has a client id made from the signal and is saved before it is sent. After a
  timeout or a restart, the executor looks the id up instead of buying twice.
- Partial fills are recorded; the position is the amount actually bought.
- A rejected order is logged and that signal is not retried.
- If the exchange is unreachable 3 times in a row you get a disconnect alert; new entries wait,
  exits keep being tried.
- If the paper service stops writing for 5 minutes, new entries stop until it is back.
- Signals older than 2 bars are skipped rather than chased.
- After a restart, every open position is checked against the account balance.

**Test the kill switch on the testnet** once a position is open:
`docker compose run --rm collector trade kill --confirm`. Check that the testnet executor sold it,
then `trade resume`. The go/no-go gate looks for this test in the event log.

## Step 2: Telegram alerts

1. In Telegram, talk to **@BotFather**, send `/newbot`, and pick a name. Copy the token.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser and copy `chat` → `id`.
3. Put both in `.env`: `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_CHAT_ID=...`.
4. `docker compose run --rm collector telegram --test` sends a test message.
5. `docker compose --profile telegram up -d telegram`.

You get testnet and live trades, paper closes, errors, crashes, disconnects, risk pauses, the
kill switch, a daily summary just after 00:00 UTC, and the weekly report. The bot only obeys
commands from your chat id.

## Step 3: weekly review

```bash
docker compose run --rm collector report weekly
```

For each paper strategy it shows this week's and all-time results next to the backtest, and
flags any strategy whose paper expectancy is more than 0.15R from the backtest after 20+ trades.
For the executors it shows the average difference from paper per trade and the slippage. It is
saved under `data/reports/` and sent to Telegram. Add it to cron for Monday mornings:

```
0 6 * * 1 cd ~/AI_crypto_trader && docker compose run --rm collector report weekly >/dev/null 2>&1
```

## Step 4: the go/no-go gate

```bash
docker compose run --rm collector golive check
```

prints every criterion with PASS or FAIL:

| Criterion | Needs (change in `.env`) |
|---|---|
| Paper trades completed | ≥ 100 (`AITRADER_GATE_MIN_PAPER_TRADES`) |
| Expectancy after costs, paper | > 0 |
| Expectancy after costs, backtest out of sample | > 0 for every selected strategy |
| Max drawdown in paper | ≤ 10R, your live loss limit (`AITRADER_GATE_MAX_DRAWDOWN_R`) |
| Paper vs backtest gap | ≤ 0.15R (`AITRADER_GATE_MAX_GAP_R`) |
| Stability | no crash and no heartbeat gap over 30 min for 14 days (`AITRADER_GATE_STABLE_DAYS`) |
| Security: keys | you confirm trade-only keys, no withdrawal, IP whitelist, 2FA |
| Security: kill switch tested | a testnet kill-switch run in the last 30 days |

Confirm the manual items once they are true:

```bash
docker compose run --rm collector golive attest keys --by Shawn
docker compose run --rm collector golive attest compliance --by Shawn --note "checked legality and tax in my country"
```

The dashboard's Trading page shows the latest gate result and has a "Check the gate now" button.
It can't approve anything.

## Step 5: approve the pilot

When the gate passes, approve one or two strategies by id (`aitrader learn list` shows them):

```bash
docker compose run --rm collector golive approve --strategy <id> --by Shawn
```

The approval is tied to those strategies' rules and to the active AI model. If either changes,
the live executor stops opening trades for it until you check the gate and approve again.
`golive revoke --by Shawn` withdraws it at once.

## Step 6: open the live lock (you, on the VPS)

Real orders need all of these, and the code checks every one before each order:

1. Real Binance keys in `.env` (trade only, withdrawals off, VPS IP whitelisted).
2. `AITRADER_PILOT_MAX_USDT` set to the most the pilot may use (the roadmap suggests 5-10% of
   what you plan to trade). The executor treats this as its whole account.
3. The live lock, in `.env`:
   ```
   AITRADER_MODE=live
   AITRADER_LIVE_UNLOCKED=true
   AITRADER_LIVE_CONFIRM=I accept real-money risk
   ```
4. Start it: `docker compose --profile live up -d executor-live`.

`executor-live` does not restart itself after a crash, so a failure stops trading instead of
looping. The update script keeps the lock as it was: an update that would change it is rolled
back.

To go back to paper at any time: `trade kill --confirm` (sells the pilot's positions), then
`docker compose stop executor-live` and set `AITRADER_LIVE_UNLOCKED=false`.

## While the pilot runs

- Same risk caps as above; a daily or weekly loss cap pauses trading.
- After every closed live trade the executor compares live results with paper. If live drawdown
  passes the 10R limit, or live expectancy is more than 0.3R per trade below paper after 20 live
  trades, trading pauses and you get an alert.
- Scale up only after a few months of live results close to paper: raise
  `AITRADER_PILOT_MAX_USDT` in small steps, never more than doubling at a time, and add a
  strategy only after it has passed the same gate.

## Compliance

Check that spot crypto trading on your exchange is legal where you live, how gains are taxed, and
keep the trade records (`data/trade/live_state.json` has every order and fill; back it up). This
software gives no financial advice and can lose money.
