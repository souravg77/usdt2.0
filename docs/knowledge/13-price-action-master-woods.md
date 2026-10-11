# How to Trade with Price Action (Master Edition) — Galen Woods, 2014

Notes on book b68b5cfc. Read in full, p. 2 to p. 82. The book collects 10 blog articles from Trading Setups Review.
**Market:** mostly US index futures (ES, NQ, YM, TF), plus CL and ZN, on **intraday charts (5m, 10m, 30m, 1h)**. One chapter covers forex futures (6E, 6J) on 30m/1h charts. None of it is crypto. Charts were made in NinjaTrader.

## Main ideas
- Trade in the direction of the intraday trend ("path of least resistance"), and use simple price-action patterns to time entries with a small, defined risk (p. 39, p. 73).
- Keep the toolset small. A single 20-period moving average may be "the only indicator you need" (p. 52).
- Trapped traders drive the market. Traders who get stopped out of losing trades, or shaken out of good ones, add order flow that you can trade with (p. 59-64).
- Most of the work is staying out of bad trades: avoid tight congestion, never fight momentum on a trend day, and don't overtrade (p. 75-80).
- The author says plainly that the re-entry approach "is not mechanical" and is discretionary (p. 70).

## Ch. 1: Inside-bar statistics (p. 10-15)
- Backtest setup: ES 5m, regular session, Nov 2008 to Nov 2013. Trend filter is the **slope of the 20-period EMA**. Every trade uses **1:1 reward:risk**.
- Benchmark: all inside bars win **37.33%** of the time (n = 4107).
- Volume relative to the previous bar: high (>75%) wins 39.41% (n = 746); low (<25%) wins 32.76% (n = 174). Both are said to underperform (the table's signs look inconsistent).
- Range relative to the previous bar: **wide (>75% of the prior bar's range) wins 52.69%** (n = 167); narrow (<25%) wins 50% (n = 32, small sample).
- Body vs range: "directional" (body > half the range) wins 39.17%; doji-like wins 37.31%. No real difference.
- Signal bar closing in the trade direction (a bullish inside bar for longs) wins **40.97%**; against the trade, 33.19%.
- Filtered rule (wide range + closes in the trade direction), win rates: ES 50, YM 47.58, NQ 48.04, TF 49.89, CL 51.17, ZN 39.13. That is about +8 points over the unfiltered result on most contracts.
- The author warns that the assumptions are naive (1:1 R:R, EMA filter) and that this is "not meant to be used in isolation" (p. 14).

## Ch. 2: Chart types (p. 16-38)
- Line, bar (OHLC), candlestick, volume, tick, range, Point & Figure, Renko, Kagi and Three-Line Break charts, with Heikin-Ashi as a bonus.
- Volume charts: a sensible setting is the average volume of your usual timeframe (example: 28,000 contracts for ES 5m). Volume-based bars make volume indicators meaningless.
- Tick charts: tune them to match the volatility of your usual timeframe. Fibonacci settings such as 144 and 233 ticks are popular.
- Range bars: inside bars and NR4/NR7 cannot occur on them.
- P&F: example box of 4 ticks with the standard 3-box reversal (12 ticks).
- Renko: use large bricks on a higher timeframe to track the trend and trade in its direction on a smaller one. Renko can also help you let profits run.
- Kagi is weak in sideways markets. Three-Line Break's "white suit" is a buy signal.
- Advice: treat a new chart type as a complement to the one you use, not a replacement. Every chart type has drawbacks (p. 37-38).

## Ch. 3-4: Four ways to define the intraday trend (p. 39-47)
1. **20 SMA + price action** (p. 39-41). Bullish when: (1) price touches the SMA; (2) at least one bar stays above it; (3) a pullback toward the SMA has **no bar high below the SMA**; (4) price breaks the last extreme high. The bearish version mirrors this.
2. **Price channel** (p. 42). Uses moving averages of the bar highs and bar lows (after Jake Bernstein's MA channel). No period is given. Bull trend = **two bars completely above the channel**; bear trend = two bars completely below. Keltner or Bollinger bands can stand in, with adapted rules.
3. **Higher timeframe** (p. 44-45). For example, hourly bars set the trend for 5m trading: a higher high on the hourly turns the trend bullish, a lower low turns it bearish. Elder's Triple Screen uses a factor of 5 between timeframes (1m/5m/25m). Day traders often use 30m or 1h; forex traders may use 4h.
4. **Trend lines** (p. 46). A break of the trend line implies a reversal. Drawing them is a discretionary skill.
- No method is perfect. Indicator look-back periods need to suit current volatility (p. 47).

## Ch. 5: Day trading with only a 20-period MA (p. 48-52)
- Any intermediate period works. A 200 MA lags too much and a 3 MA is redundant. The author uses an EMA but says an SMA is fine. Be consistent.
- Five context questions: Is price above or below the MA? How did it get there? How much does price overlap the MA (example: 7 of 20 bars)? What is the slope? How often does the slope flip?
- If the market is bullish but not strongly trending, take longs only and aim for **nearer targets**.
- Setup: in a bull trend, **buy when price retraces to the 20 MA**. The example entry was a two-bar reversal at the MA. Candlestick patterns at the MA work too.
- Management: the MA makes a natural **trailing stop** (p. 51).

## Ch. 6: Entry orders (p. 53-58)
- Use **stop orders for breakouts** (example: a stop one tick beyond an inside bar) and **limit orders to fade breakouts or failures** (example: a sell limit just above the high of a tight range).
- Stop entries give confirmation and execute efficiently. Limit entries are an advanced form of reversal trading.
- Choose the entry type your strategy calls for. There is no perfect entry.

## Ch. 7: Trapped traders (p. 59-64)
- Two kinds: traders stuck in losing positions, who will have to exit at their stops, and traders stopped out of winning positions, who will chase.
- **Hikkake**: an inside-bar breakout fails, and you enter the other way as the breakout traders bail out. Look for the best-quality inside bars first.
- **Two-legged pullback** in a trend traps two groups of traders.
- **Pin bar**: the best ones poke beyond a *major* swing high or low. The long tail shows the trap.
- **Trend bar failure** is mentioned; the details are in the Strategies Edition.

## Ch. 8: Re-entry strategy (p. 65-70)
- Long rules: (1) a bullish pin bar forms; (2) the next bar moves above its high (the original setup triggers); (3) price falls below the pin low "but not too far below" (the original traders get stopped out); (4) buy when price breaks above any bullish bar.
- The examples use a 20 EMA on 30m and 1h forex futures. The winning re-entry had the EMA as support. **The losing re-entry happened below the EMA**, which hinted that the bias had changed. Re-entries also go poorly once a measured target (e.g. from a triangle) has already been hit.
- Good re-entries come soon after the original setup. The trade-off is fewer trades, and some moves never offer a re-entry. The approach is recommended for people who overtrade.
- The short rules on p. 67 contain a typo: they say "buy" where they should say sell.

## Ch. 9: Livermore tips (p. 71-74)
- Start with the broad market trend: check the index before individual stocks.
- Go for large impulse swings. Take longs when the next resistance is far away, and let profits run.
- Being wrong on the trend is different from being wrong on timing. Time entries with bar or candle patterns and use a **pattern stop** just below a bullish pattern.
- Don't overtrade: have clear entry rules and limit your trade count. A day trader who overtrades should try **one good trade a day**.
- Avoid reversal trades. Trade with the trend using longer MAs and major trend lines.

## Ch. 10: Intraday tips (p. 75-80)
- **Avoid tight congestion.** Mid-session is usually quiet, so measure the average hourly or half-hourly range. Congestion = the market **fails to close higher (or lower) for at least 3 consecutive bars**. Stop trading when you see it (p. 77).
- Don't chase a 10-tick profit inside a 5-tick range. Scalping is not for beginners.
- **Narrow range bars** (NR7, NR4/ID) give low-risk triggers, but **not inside congestion**. Example: a bullish pullback ends on an NR7 bar; the conservative target is the last extreme, which still gives a good R:R (p. 78).
- **Don't fight momentum or a trend day.** A bullish trend day opens near its low and closes near its high. Watch how price reacts at the last swing high or low.
- Cutting the number of bad trades matters more than catching the trade of your life.

## Risk and money management
- The book gives **no position-sizing or percent-risk numbers**. Risk is controlled through pattern stops (just beyond the signal bar or pattern) and by preferring narrow bars.
- Other figures: the inside-bar study uses 1:1 R:R; weak trends call for nearer targets; strong swings call for letting profits run; one good trade a day for overtraders.

## Psychology
- Think like trapped traders but don't act like them. Accept being wrong instead of re-shorting a trend day "to be a hero". Be patient (skip the original entry, wait for the re-entry). Paper trade before going live (p. 81).

## What the author says doesn't work or is weak
- Low-volume inside bars (32.8%) and inside bars closing against the trade (33.2%). Inside-bar filters on ZN added only +1.6 points.
- Narrow-range bars inside tight congestion. Trading mid-session congestion in general.
- A 200 MA for day trading (too slow); a 3 MA (redundant).
- Kagi breakouts in sideways markets. Indiscriminate trend lines produce whipsaws.
- Re-entries below the 20 EMA, or after the measured target has been reached.

## Claims to treat with caution
- The inside-bar win rates (37% to 53%) come from the author's own NinjaTrader backtest with 1:1 R:R and no stated costs or slippage. The best subgroups are small (n = 167 and n = 32), and the volume table's "relative to benchmark" figures don't match the stated benchmark.
- The "+8% edge on most futures" claim for filtered inside bars has no out-of-sample test or cost model, and the author calls it "not rigorous" (p. 15).
- "Hikkake works exceptionally well" and "re-entry offers a higher probability" are not backed by any statistics.
- The NR7 and MA-pullback examples are single hand-picked charts.
