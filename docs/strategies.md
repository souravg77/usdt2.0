# Book strategies as rules

Sources: *Crypto Trading For Beginner Traders* (Finance Illustrated, 2021), 25 pages, and the
Binance Academy trading eBook (for `bollinger_squeeze` and the indicator rules). See
[knowledge/rules-from-books.md](knowledge/rules-from-books.md) for every rule taken from the books.

**Read this first.** The ebook is an introductory guide. It names four strategies (day trading,
trend trading, hedging, HODL) and a few ideas (buy the rumor and sell the news, sell at peak
euphoria), but it gives **no indicators, levels, stops, targets or timeframes**. Everything below
is a written decision that turns its wording into a testable rule. None of it is the ebook's own
rule. Each rule can be changed, and Phase 3's backtests decide whether any of it has an edge.

Run `aitrader strategies` to print the current rule cards straight from the code.

| Ebook section | What it says | Coded as | Status |
|---|---|---|---|
| #1 Day Trading | Buy and sell after small moves; use up to 100x leverage | `momentum_breakout` (5m/15m) | Coded, **no leverage** |
| #2 Trend Trading | Identify up/down trends; don't trade at the end of a trend | `trend_pullback` (1h/4h) | Coded, long only |
| #3 Hedging | Size bigger when confident, smaller when not | Confidence-scaled sizing in the Phase 4 AI filter | Not a signal strategy |
| #4 HODL | Buy during a dip; hold projects with solid technology | `buy_the_dip` (1d/4h), BTC and ETH only | Coded |
| Cycle of emotions | Sell at peak happiness, buy back after the dip | `buy_the_dip` exit when RSI > 70 | Coded as exit rule |
| Buy the rumor, sell the news | Act on news before it lands | Event and news filters (Phase 2, steps 4-5) | Calendar done; news later |
| Profit in both directions | Open short positions | Not coded | Off until futures are allowed after the pilot |

## trend_pullback (ebook #2, Trend Trading)

| Field | Rule |
|---|---|
| Setup | EMA20 > EMA50 > EMA200, market structure making higher highs and higher lows, ADX(14) >= 20 |
| Entry | Bar low touches EMA20 and the bar closes back above it as a bullish candle; market order at the next open |
| Stop | Lower of the 10-bar low and entry - 2 x ATR(14) |
| Target | 2R fixed; the backtest also scores a trailing exit on a close below EMA50 |
| Filter | Skip when RSI > 70, price is more than 3 ATR above EMA50 ("trend about to end"), or ATR percentile > 95% |
| Size | 0.5% of equity at risk |

## momentum_breakout (ebook #1, Day Trading)

| Field | Rule |
|---|---|
| Setup | Close above the prior 20-bar high on volume >= 1.5 x the 20-bar average, with close above EMA200 |
| Entry | Market order at the next open |
| Stop | Entry - 1 x ATR(14) |
| Target | 1.5R fixed; time stop after 24 bars |
| Filter | Skip when RSI > 80 or the breakout bar's range is > 3 ATR (news spike) |
| Size | 0.5% of equity at risk, **no leverage** |

## buy_the_dip (ebook #4, HODL, plus the cycle of emotions)

| Field | Rule |
|---|---|
| Setup | Close > SMA200 (long-term uptrend), close at least 10% under the 20-bar high, RSI(14) < 35 |
| Entry | Within 5 bars of the setup, the first bullish bar that closes above the previous high; market order at the next open |
| Stop | Entry - 3 x ATR(14) |
| Target | Exit when RSI(14) > 70 ("sell at peak happiness"), capped at 3R |
| Filter | BTC/USDT and ETH/USDT only by default |
| Size | 0.5% of equity at risk |

## bollinger_squeeze (Binance Academy: Bollinger squeeze, volume, VWAP, Parabolic SAR)

| Field | Rule |
|---|---|
| Setup | Bollinger(20, 2) band width in the lowest 20% of its last 120 bars, at some point in the last 5 bars |
| Entry | Close above the upper band on volume >= 1.5 x its 20-bar average, with close above the 50-bar VWAP; market order at the next open |
| Stop | Lower of the middle band and entry - 1 x ATR(14); stop-market exit |
| Target | 2R fixed; the backtest also scores a Parabolic SAR trailing stop |
| Filter | Skip if the breakout bar's range is > 3 ATR; long breakouts only in the pilot |
| Size | 0.5% of equity at risk |

The book calls the squeeze direction-neutral. Downside breakouts are ignored until shorting is
allowed. `trend_pullback` and `buy_the_dip` also record StochRSI (overbought above 0.8, oversold
below 0.2) as a feature for the Phase 4 filter rather than as a hard rule.

## Liquidity check

Before an order, `strategies.liquidity_check(book, order_usdt)` requires a spread of at most
0.10% and at least 10x the order size resting within 0.5% of the mid price on each side, so the
entry and a stop-market exit can both fill (Binance Academy on spread and slippage; Treasury on
wash-traded volume, which is why the live book is checked instead of the reported volume). The
Phase 5 order loop will call it; the values are starting points.

## Checking the signal log (Phase 2 exit criterion)

```bash
aitrader download --since 2023-01-01 --exchange binance --symbol BTC/USDT --timeframe 4h
aitrader signals --strategy trend_pullback --symbol BTC/USDT --timeframe 4h
```

This writes `data/signals_<strategy>_<exchange>_<symbol>_<tf>.csv` with each signal's time,
entry, stop, target and reason. Compare a handful of signals with the chart by eye.

## Economic calendar filter

`aitrader calendar` shows upcoming high-impact events from the Crypto Craft weekly feed (this
week and next week), converted from New York time to UTC. It also shows the current decision:

- From 30 minutes before a High-impact release, new entries are blocked and stops should tighten.
- For 30 minutes after it, entries stay blocked while spreads and volatility settle.
- If the feed can't be read or is older than 12 hours, trading continues at half size (fail safe).

All three values are starting points to tune in the backtest.

## Not done yet in Phase 2

- Fundamental and on-chain context (funding, open interest, BTC dominance, flows)
- Crypto event feeds (token unlocks, listings, ETF flows)
- News sentiment scoring

Each source's API, cost and terms need checking before we build it. Exchange funding and
announcement data come first, because they are free and reliable.
