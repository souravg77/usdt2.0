# Futures: shorts on Bybit USDT perpetuals

Decided by the owner on 2026-10-12: the agent also trades **short**, on Bybit USDT perpetual
futures. Longs stay on spot.

## Where the short strategies come from

Every long strategy has a **short twin** (`strategies/mirror.py`): the same rules run on the
price chart turned upside down (each price becomes 1/price, so highs and lows swap). A dip bought
in an uptrend becomes a bounce sold in a downtrend; a breakout up becomes a breakdown; RSI under
30 becomes RSI over 70. The twin's stop sits above the entry and its target below. Funding-rate
filters change sign ("longs crowded" reads as "shorts crowded").

A twin is only a candidate. It goes through the same backtest gate, walk-forward, Monte Carlo,
holdout and paper trading as every other strategy, and most will fail. In the registry a twin is
kind `short`, id `short-<id of the long one>`, and carries a copy of the long rules.

```bash
aitrader learn add-shorts     # add the twins (the dashboard's "Turn paper trading on" does this)
aitrader learn test           # backtest them like any other candidate
```

## Costs in backtests and paper trading

| | Spot (longs) | Perpetual (shorts) |
|---|---|---|
| Taker fee | 0.1% | 0.055% |
| Maker fee (targets) | 0.1% | 0.02% |
| Slippage | 5 bps per fill | 5 bps per fill |
| Funding | none | 0.01% per 8 hours **charged** as a cost |

Funding is usually paid *to* shorts, but it can turn, so the backtest never counts it as income:
results are on the cautious side. Signals and fills use the spot candles; the perpetual's price
tracks spot closely and the small gap is not modelled.

## Leverage and liquidation

- **Isolated margin**: a position can lose at most the margin put into it.
- **Leverage** `AITRADER_LEVERAGE` in `.env`: 1 by default, never more than 3 (`MAX_LEVERAGE`).
  Size always comes from the risk engine (0.25% of equity at risk, from the stop distance), so
  leverage doesn't make a trade bigger. It only sets aside less margin, and brings the
  liquidation price closer.
- A short is taken only when its stop is at most **halfway to the liquidation price** (with
  Bybit's 0.5% maintenance margin). At 1x that is up to +49% above the entry; at 3x up to +16%.

## Mark price, index price, basis and the real liquidation price

`data/derivs.py` reads the perpetual's own numbers (public data, no key):

- **Last**: the perpetual's last trade. **Index**: Bybit's average of spot prices on several
  exchanges. **Mark**: what Bybit liquidates on and values open P&L with; it follows the index,
  so a wick on one exchange doesn't liquidate anyone.
- **Basis** = (last - index) / index; **mark premium** = (mark - index) / index.
- **Funding** rate, its yearly equivalent, the time to the next payment, and open interest.

`aitrader context derivs` prints them for your pairs. The WebSocket version is
`BybitLinearStream` (the public linear stream, same reconnect and staleness rules as spot).

On the live executor:

- A short's resting stop triggers on the **mark price** (`triggerBy: MarkPrice`), the same
  price Bybit liquidates on, so a wick in the last price can't fire it early.
- Every minute it reads Bybit's own liquidation price for each open short. If the mark is within
  3% of it, or the stop is no longer at most halfway from entry to it, trading **halts** (see
  [Safety halt](live-trading.md#safety-halt-automatic)).
- When a short closes, the funding Bybit settled while it was open (the account's SETTLEMENT
  rows) is taken off its P&L and shown as `funding_usdt`.
- The account must be in **One-Way** position mode; in Hedge mode every short is skipped with
  "switch BTC/USDT:USDT to One-Way position mode".

## What runs where

| | Status |
|---|---|
| Backtests, reports, replay | short twins included |
| Paper trading | shorts paper-traded with perp costs, isolated margin and the liquidation check |
| AI trade filter | learned from long trades, so it doesn't judge shorts |
| Live executor | sells the coin's USDT perpetual short (one-way mode, `AITRADER_LEVERAGE`), puts a reduce-only stop on Bybit above the entry, and buys back with reduce-only orders. Same live lock, go-live approval and kill switch as spot |
| API key | Contracts / Derivatives / Options permissions are now accepted; withdrawal and transfer rights are still refused, and the key must be bound to the server's IP |

## Before the first real short

1. Bybit app: **Derivatives → margin mode → Isolated Margin**. On a Unified Trading Account this
   setting covers the whole account, so the agent only *checks* it and never changes it; until it
   is Isolated, every short signal is skipped with "switch the Bybit account to Isolated Margin".
2. The API key needs Contracts / USDT Perpetual trade permission (still no withdraw or transfer).
3. Optional: `AITRADER_LEVERAGE=2` (or 3) in `.env`. The agent sets that leverage on each
   perpetual before its first order.

4. Bybit app: **Derivatives → position mode → One-Way** (the default).

The kill switch buys back every short as well as selling every spot coin. Funding is settled by
Bybit on the open position; the executor reads it back when the short closes and takes it off
that trade's P&L.

Bybit offers derivatives only in some countries; check that your account can trade USDT
perpetuals before relying on the live part.
