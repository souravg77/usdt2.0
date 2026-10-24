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

## Strategy families from the master prompt (untested research candidates)

Twelve more hand-coded strategies cover families from section 4 of the master prompt that fit
**long-only spot** trading. They live in `src/aitrader/strategies/families.py`. None of them has
been backtested on real data yet, and nothing here claims or expects a profit. `learn
add-builtin` registers them as `candidate`; each must pass the same backtest gate (`learn test`:
in-sample and out-of-sample, after fees and slippage, t-stat >= 2) before it can paper trade, and
most research candidates are expected to fail it. The thresholds are starting points written down
before any test, not tuned values.

Shared rules: long spot entries (each strategy also has a short twin for Bybit perpetuals, see
[futures.md](futures.md)); 0.5% of equity at
risk per trade, units = risk / (entry - stop); entry at the next bar's open; **no trade while a
candle is missing in the last 100 bars, or when ATR(14) is above its 95th percentile of the last
500 bars**. `aitrader strategies` prints every rule card, including its regime.

| Strategy | Family (prompt #) | Regime | Entry | Stop | Exit | Must not trade when |
|---|---|---|---|---|---|---|
| `supertrend_trend` | Supertrend following (#5) | close > EMA200, ADX >= 20 | Supertrend(10, 3) flips up | Supertrend line, at most 4 ATR | 3R, or next open after Supertrend flips down | RSI > 80 |
| `mtf_trend_alignment` | Multi-timeframe alignment (#7) | 4x higher frame (rebuilt from this frame's candles) EMA20 > EMA50, close > EMA50; this frame EMA20 > EMA50 | close crosses back above EMA20 | lower of 10-bar low and 2 ATR | 2R, or close < EMA50 | latest higher bar incomplete; RSI > 75 |
| `keltner_breakout` | Keltner breakout (#11) | close > EMA200 | close crosses above Keltner(20, 2, ATR10) upper, volume >= 1.2x average | lower of EMA20 and 1.5 ATR | 2R, or close < EMA20 | bar range > 3 ATR; RSI > 80 |
| `atr_expansion_breakout` | ATR expansion breakout (#14) | quiet before (prior ATR percentile <= 60%), close > EMA50 | true range >= 2x prior ATR, close in top 25% of bar and above 20-bar high | bar low - 0.1 ATR | 2R, time stop 24 bars | bar range > 4 ATR |
| `volatility_regime_switch` | Volatility-regime switching (#38) | trend: ADX >= 25, uptrend, ATR pct <= 85%; range: ADX < 20, ATR pct <= 60%, close > SMA200 | trend: 20-bar high breakout; range: close back above lower Bollinger band | 2 ATR (trend) / 1.5 ATR (range) | trend 3R or close < EMA50; range: Bollinger middle (>= 0.8R), 24-bar time stop | ATR pct > 85% or neither regime |
| `williams_r_reversal` | Williams %R reversal (mean reversion) | close > SMA200 | %R(14) crosses above -80 | 5-bar low - 0.25 ATR | %R > -20, 2R cap, 30-bar time stop | ADX > 35 with -DI > +DI |
| `ma_deviation_reversion` | MA deviation reversion (#21) | close > SMA200 | up bar within 3 bars of close <= SMA20 - 2.5 ATR | 5-bar low - 0.25 ATR | SMA20 at the signal (>= 1R away), 20-bar time stop | ADX > 35 with -DI > +DI |
| `obv_breakout` | OBV breakout confirmation (#27) | close > EMA50 | close above 20-bar high and OBV above its 20-bar high | 1.5 ATR | 2R, or close < EMA50 | RSI > 80; a zero-volume bar in the window |
| `mfi_momentum` | MFI momentum confirmation (#28) | close > EMA50 > EMA200 | MFI(14) crosses above 50 after being < 40 within 10 bars | lower of 10-bar low and 2 ATR | 2R, or close < EMA50 | MFI > 80; a zero-volume bar |
| `cmf_pressure` | CMF buying pressure (#29) | close > EMA50 | CMF(20) crosses above +0.05, close > previous high | 2 ATR | 2R, or CMF < -0.05 | RSI > 80; a zero-volume bar |
| `funding_extreme_long` | Funding-rate extreme reversal (#33), long side | perp funding <= -0.03% per 8h (shorts crowded) | close crosses above EMA20 | lower of 10-bar low and 2 ATR | 2R, close < EMA50, or 48-bar time stop | funding missing or stale; RSI > 70 |
| `ensemble_vote` | Ensemble voting (#40) | its members' | a member signals and >= 2 of 3 families (trend, breakout, volume) voted in the last 3 bars | 2 ATR | 2R, or close < EMA50 | two members of one family count as one vote |

Notes:

- `funding_extreme_long` reads the Bybit USDT-perpetual funding rate stored by `aitrader context` (the
  same data as the market-context filter). The backtest gate, report and paper trader attach it to
  the candles for this strategy only, using the newest value stamped by each bar's close. Without
  that history it makes no trades and the gate rejects it for too few trades. The trade is a spot
  buy; the perpetual is only read.
- `mtf_trend_alignment` builds the higher frame (4h for 1h bars, 1h for 15m bars) from the
  strategy's own candles, because the engine hands a strategy one timeframe. A higher bar counts
  only after its last candle has closed, and never if one of its candles is missing.
- `ensemble_vote` gives every family one equal vote. It does not weight members by past results,
  because that would leak the test period into the rule.

## All 40 master-prompt strategies

`strategies/catalog.py` maps every strategy in the master prompt's list to the class that
implements it. New ones are in `strategies/master.py` (long rules; the short twins trade the
bearish side) and `strategies/quant.py` (the two-leg ones). Every class answers the same
interface, defined in `strategies/base.py`:

| Method | What it answers |
|---|---|
| `generate(df, ...)` | every signal over closed candles (backtest, paper and live all use it) |
| `entry_signal(df, ...)` | the signal on the newest closed bar, or none |
| `exit_signal(df, signal)` | "stop", "target", "exit rule: ..." or none, on the newest bar |
| `invalidation_condition(df, signal)` | the close is through the stop, or the regime is no longer eligible |
| `calculate_stop_loss(entry, df)` | the strategy's stop for that entry |
| `eligible_regimes` | the market regimes it may open trades in; signals on other bars are dropped |
| `no_trade_conditions(df, tf)` | plain-language reasons it must not trade now (missing candles, disorderly ATR, wrong regime, no funding data) |

Regimes come from `market/regime.py` (trend-up, trend-down, range, high-volatility, uncertain).
Liquidity is judged by the pair ranking and the liquidity check with real order books, so the
strategy gate never labels a bar low-liquidity. A short twin trades the mirror regimes: a long
strategy for trend-up becomes a short for trend-down.

Every one of these is an untested candidate until it passes `learn test`; the keep-top step
(`learn keep-top`) still paper-trades only the best 10-20.

**Trend following**

| # | Strategy | Class | Status | Regimes |
|---|---|---|---|---|
| 1 | EMA crossover | `ema_crossover` | trades | trend-up, uncertain |
| 2 | SMA crossover | `sma_crossover` | trades | trend-up, uncertain |
| 3 | MACD trend following | `macd_trend` | trades | trend-up, uncertain |
| 4 | ADX-filtered trend following | `adx_trend` | trades | trend-up, uncertain |
| 5 | Supertrend following | `supertrend_trend` | trades | trend-up, uncertain |
| 6 | Ichimoku trend confirmation | `ichimoku_trend` | trades | trend-up, range, uncertain |
| 7 | Multi-timeframe trend alignment | `mtf_trend_alignment` | trades | trend-up, uncertain |
| 8 | Donchian trend following | `donchian_trend` | trades | trend-up, range, uncertain |

**Breakout and volatility**

| # | Strategy | Class | Status | Regimes |
|---|---|---|---|---|
| 9 | Donchian channel breakout | `donchian_breakout` | trades | trend-up, range, uncertain |
| 10 | Bollinger Band squeeze breakout | `bollinger_squeeze` | trades | trend-up, range, uncertain |
| 11 | Keltner Channel breakout | `keltner_breakout` | trades | trend-up, range, uncertain |
| 12 | Opening-range breakout | `opening_range_breakout` | trades | trend-up, range, uncertain |
| 13 | Volume-confirmed breakout | `volume_breakout` | trades | trend-up, range, uncertain |
| 14 | ATR expansion breakout | `atr_expansion_breakout` | trades | trend-up, range, uncertain |
| 15 | VWAP reclaim or breakdown | `vwap_reclaim` | trades (breakdowns are the short twin) | trend-up, range, uncertain |
| 16 | Support/resistance breakout and retest | `breakout_retest` | trades | trend-up, range, uncertain |

**Mean reversion**

| # | Strategy | Class | Status | Regimes |
|---|---|---|---|---|
| 17 | RSI reversal | `rsi_reversal` | trades | trend-up, range, uncertain |
| 18 | Stochastic RSI reversal | `stoch_rsi_reversal` | trades | trend-up, range, uncertain |
| 19 | Bollinger Band mean reversion | `bollinger_mean_reversion` | trades | range, uncertain |
| 20 | VWAP mean reversion | `vwap_mean_reversion` | trades | range, uncertain |
| 21 | Moving-average deviation reversion | `ma_deviation_reversion` | trades | trend-up, range, uncertain |
| 22 | Fibonacci retracement reaction | `fibonacci_reaction` | trades | trend-up, range, uncertain |
| 23 | Volume-profile value-area reversion | `value_area_reversion` | trades | range, uncertain |
| 24 | Range-bound support/resistance | `range_support_bounce` | trades | range |

**Momentum and price action**

| # | Strategy | Class | Status | Regimes |
|---|---|---|---|---|
| 25 | MACD histogram momentum | `macd_histogram_momentum` | trades | trend-up, range, uncertain |
| 26 | RSI divergence | `rsi_divergence` | trades | trend-up, range, uncertain |
| 27 | OBV breakout confirmation | `obv_breakout` | trades | trend-up, range, uncertain |
| 28 | MFI momentum confirmation | `mfi_momentum` | trades | trend-up, range, uncertain |
| 29 | CMF buying/selling pressure | `cmf_pressure` | trades | trend-up, range, uncertain |
| 30 | Candlestick reversal patterns | `candlestick_reversal` | trades | trend-up, range, uncertain |
| 31 | Market structure | `structure_break` | trades | trend-up, range, uncertain |
| 32 | Liquidity-sweep reversal | `liquidity_sweep` | trades | trend-up, range, uncertain |

**Crypto-specific and quantitative**

| # | Strategy | Class | Status | Regimes |
|---|---|---|---|---|
| 33 | Funding-rate extreme reversal | `funding_extreme_long` | trades (crowded longs (funding very positive) is the short twin) | trend-up, trend-down, range, uncertain |
| 34 | Funding-rate trend filter | `funding_trend_filter` | trades | trend-up, uncertain |
| 35 | Spot/perpetual basis trading | `basis_carry` | research only (two legs (spot and perp) must open together) | - |
| 36 | Cross-exchange spread arbitrage | `cross_exchange_arbitrage` | not possible (needs a second exchange; the platform is Bybit only) | - |
| 37 | Pairs trading and cointegration | `pairs_trading` | research only (two coins must open together) | - |
| 38 | Volatility-regime switching | `volatility_regime_switch` | trades | trend-up, trend-down, range, high-volatility, low-liquidity, uncertain |
| 39 | ML signal classification | `ml_classifier` | trades | trend-up, range, uncertain |
| 40 | Ensemble voting | `ensemble_vote` | trades | trend-up, range, uncertain |


Research only means the class measures the opportunity after all costs (`BasisCarry.evaluate`,
`BasisCarry.opportunities`, `PairsTrading.analyse` with an Engle-Granger test and an
Ornstein-Uhlenbeck half-life) but never makes a signal: both legs would have to open and close
together, and the executor trades one leg at a time with its own stop. Cross-exchange
arbitrage is recorded as not possible because the platform trades on Bybit only (owner decision).

## ml_boosted (machine learning that finds its own entries)

No hand-written entry rule: a gradient-boosted tree model (scikit-learn HistGradientBoosting) decides
when to buy. For every past bar of the chart it learns whether price reached +3 ATR before -1.5 ATR
within 24 bars, from 22 features (RSI 7/14, MACD histogram, ADX, DI spread, Bollinger %B and width,
distance from EMA 20/50/100, 1-24 bar returns, ATR % and ATR 14/50 ratio, relative volume, position
in the 20-bar range, candle body and wicks). A long opens when its chance for the newest closed bar
crosses above 50 %; the short twin (`short: ml_boosted`) has its own model, trained on the upside-down
chart. Stop 1.5 ATR, target 2R, time stop 24 bars, 0.5 % risk. 1h and 4h.

- Walk-forward: a model is retrained every 250 bars on up to the last 1,500 bars whose outcome was
  already known, and only predicts the bars after it, so backtests and Replay are out-of-sample.
- Paper trading reads about 2,000 bars per chart (the collector keeps that much 1h and 4h history)
  and reuses each trained model until the next retrain step.
- It is a candidate like every other strategy: `learn add-builtin ml_boosted`, `learn add-shorts`,
  `learn test`, then the usual picks. `ml_classifier` is the simpler logistic-regression baseline.

## New indicators

Added to `aitrader.indicators` with unit tests (warm-up, value checks, no look-ahead):

| Indicator | Function | Warm-up | Rule-language features |
|---|---|---|---|
| Supertrend | `supertrend(df, n=10, mult=3)` | ATR(n) bars; direction 0 before | `supertrend(10,3)`, `supertrend_dir(10,3)` |
| On-Balance Volume | `obv(df)` | none, but its level is arbitrary: compare it with its own highs or average | `obv`, `obv_high(20)`, `obv_sma(20)` |
| Money Flow Index | `mfi(df, n=14)` | n + 1 bars | `mfi(14)` |
| Chaikin Money Flow | `cmf(df, n=20)` | n bars | `cmf(20)` |
| Keltner Channels | `keltner(df, n=20, mult=2, atr_n=10)` | max(n, atr_n) bars | `keltner_upper/mid/lower(20,2,10)` |
| Williams %R | `williams_r(df, n=14)` | n bars | `williams_r(14)` |
| Validity | `missing_candles(df)`, `indicator_valid(df, warmup)` | - | `no_gaps(200)`, `sma_dev_atr(20,14)` (stretch from the mean in ATRs) |

`indicator_valid(df, warmup, step_ms, value)` is the shared validity status: True only when the
indicator has `warmup` bars of history, no candle is missing inside that window, and (if given)
the value is a finite number. Book-derived rule specs can require it with
`{"left": "no_gaps(200)", "op": ">=", "right": 1}`.

## Liquidity check

Before an order, `strategies.liquidity_check(book, order_usdt)` requires a spread of at most
0.10% and at least 10x the order size resting within 0.5% of the mid price on each side, so the
entry and a stop-market exit can both fill (Binance Academy on spread and slippage; Treasury on
wash-traded volume, which is why the live book is checked instead of the reported volume). The
Phase 5 order loop will call it; the values are starting points.

## Checking the signal log (Phase 2 exit criterion)

```bash
aitrader download --since 2023-01-01 --exchange bybit --symbol BTC/USDT --timeframe 4h
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
