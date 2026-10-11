# The Power of Divergence (Trading with the Trendlines, vol. 2) — David Carli (5th ed., 2020)

Market: written for forex first, with examples on US stocks, gold, Nasdaq-100 futures and one
BTC-USD chart (Fig. 51, p. 58). Short book (~75 printed pages, about 30 of them charts).
Page citations use the book's **printed** page numbers. The pdf page (form-feed count) is printed + 8,
e.g. printed p. 41 = pdf p. 49.

## Main idea
- One strategy: a reversal trade built on a **price vs CCI divergence**, filtered by three rules
  (CCI zone, CCI stays on one side of zero, Fibonacci extension of the second price extreme),
  **triggered only by a trendline break**, with a fixed swing stop and Fibonacci-extension targets (pp. 40-49).
- "Mere divergence, by itself, does not mean anything" (p. 69): it can keep going for days or weeks.
  The trendline break is the confirmation.
- Chapters 1-2 (pp. 7-34) are beginner TA background: chart types, candles, Dow trends, S/R, MAs,
  breakout/fakeout, retracement/pullback, gaps. None of that is a separate system.

## Background concepts (pp. 7-34)
- Candles named: long candle, short candle, marubozu, spinning top (indecision), doji types:
  long-legged (strong indecision), gravestone (bearish, at highs), dragonfly (bullish, at lows), four-price doji (pp. 13-16).
- Dow: uptrend = higher highs and higher lows; downtrend = lower highs and lower lows; major (years),
  medium (months), minor (weeks) trends nest inside each other; trade with the main trend of your
  timeframe (pp. 17-19). Note: this conflicts with the book's own reversal strategy.
- Support/resistance gets stronger the more times it is tested without breaking; historic highs/lows
  are key; polarity change after a break (pp. 19-22). Static (yearly high/low, Fib levels) vs dynamic
  (trendlines, moving averages) S/R (p. 22).
- MAs: SMA, EMA, WMA explained; a 200-period EMA is shown acting as support then resistance (pp. 23-25).
- Breakout = close through S/R; fakeout = price pokes through then closes back on the old side, in
  one or several sessions (pp. 25-27). He won't teach fakeout trading.
- Retracement/pullback: counter-trend move from profit taking; a pullback that retests broken
  resistance as support confirms the breakout (pp. 28-29).
- Gaps ("windows"): rising window = current low > previous high; the bottom of the window is support.
  Falling window = current high < previous low (p. 30).
  **Rule: do not open a long right after a rising window** (or a short after a falling one); a plain
  retracement can hit the stop (p. 31). Gaps only matter inside an established trend. Types:
  breakaway (big volume), runaway/measuring, exhaustion (very high volume, near the end of a trend) (pp. 33-34).

## Indicator: Commodity Channel Index (pp. 35-36, 40, 62)
- Donald Lambert's CCI measures how far price is from its statistical average.
- **Period: 20** (the author's setting, p. 62). He says 14 (the common default) or other values are
  allowed but give different signals. Source is not stated; TradingView's CCI defaults to hlc3
  (typical price) with the 0.015 constant. Assume that.
- Usual swing range is about +/-150. Classic overbought > +100, oversold < -100 (p. 35-36; the text has
  a typo, "CCI <100"). **The strategy uses the stricter +150 / -150 zones** to filter false
  signals (p. 40).
- He rejects the textbook CCI signals: if they worked, so many traders wouldn't blow up (p. 36).

## Divergence definition (exact, pp. 37, 41-46)
- Bullish: **price makes a lower low while the CCI makes a higher low** (p. 37).
- Bearish: price makes a higher high while the CCI makes a lower high (p. 37).
- Swing points: two consecutive price swing lows (first low A, intervening rebound high B, second low C)
  compared with the CCI lows at the same bars. The book gives **no pivot strength, no lookback window
  and no minimum/maximum bar distance**. Its daily examples have the two lows about 2-8 weeks
  apart (my estimate from the figure descriptions, not stated). Suggested implementation: pivot lows with
  2-5 bars each side, the two lows 5-60 bars apart, and the second low the most recent confirmed pivot.
- **Rule 1, zone (p. 41):** in a bullish divergence the CCI's higher low (second low) should be in the
  oversold area **below -150** (bearish: the lower high above +150). Exception (p. 43): the
  *completion* (second low) may sit "slightly outside" the zone. In practice: first CCI low < -150 and
  second low < -150, or allow the second low a small tolerance (e.g. < -130). That tolerance is my choice.
- **Rule 2, zero line (p. 41, 43-45):** during the divergence (between the two lows) **the CCI must not
  rise above 0** (bearish: must not drop below 0). Exception: a shallow, very brief poke over zero is
  tolerated (p. 45). Suggested: max CCI between the lows <= 0, or <= ~+25 for at most 1-2 bars.
- **Rule 3, Fibonacci (p. 41, 45-46):** measure the last retracement AB (bullish: first low A to rebound
  high B). The second price low C must be at an extension of **1.13, 1.272, 1.414 or 1.618** of AB,
  i.e. (B - C)/(B - A) between 1.13 and 1.618. **Over 1.618 the pattern is cancelled.** Below 1.13 isn't
  addressed; treat it as invalid (the lower low has to be meaningful).
- "They are not strict rules; some small exceptions are granted" (p. 42).

## Entry (pp. 46-48)
- The divergence alone is not a signal. Draw the **price trendline resistance** (bullish case: the
  descending line over the highs of the decline into C) and enter on its **breakout**.
- Aggressive: enter as soon as price crosses the trendline intrabar (you're surely in, but exposed to fakeouts).
- Less aggressive: wait for a **close beyond the trendline**. Worse price, but filters some fakeouts (p. 47).
- No break means no trade, even if the divergence was valid (EUR-CHF example, p. 48).
- Decide stop and target before clicking buy (p. 48).

## Stop and targets (pp. 48-49, 62)
- Stop: **15-20 pips/ticks below the last low** (the second low C) for longs (p. 48). For crypto, translate
  to a small buffer, e.g. 0.1-0.3 ATR or 0.2-0.5% below C.
- Target 1: **127.2% Fibonacci extension** of "the movement started with the last retracement". Close
  **half** there and move the stop to **break-even**. Target 2 (final): **161.8% extension** (p. 49).
  The wording is ambiguous. Most plausible reading, bullish: T = C + k*(B - C), with B the last swing
  high before C, k = 1.272 / 1.618.
- Target is the only flexible part; any target is fine if **reward:risk >= 1:1**. You may also let a
  runner ride (p. 62). The last example (LAM Research) stalls before target; the half-off plus
  break-even rule protects the gain (p. 62).

## Timeframes (pp. 62, 69)
- Mainly **daily**, rarely 4h, **never lower**. Weekly sometimes: excellent but slow. Higher timeframe
  means deeper follow-through.

## Money management (pp. 64-68)
- Position sizing only. Shares = max loss / (entry - stop), or = capital * risk% / (entry - stop).
  Example: $400 / ($31 - $28) = 133 shares.
- Risk examples: **0.5%** of a $60k account, **0.75%** of a $50k account. No hard cap given.
- Forex size = [(1000 * max loss) / stop pips] / pip value per 1000. Round down.
- Always use a stop: news or data can wreck a purely chart-based trade (p. 71).

## Psychology / discipline (pp. 6, 70-72)
- Apply every rule with discipline; the strategy only pays if you're disciplined (p. 70).
- Stress about an open trade comes ~90% of the time from oversizing (p. 71). Leverage ruins accounts;
  growing $1k to $100k takes a long time; chasing high monthly returns raises risk until ruin.
- He recommends confirming with fundamental analysis and trading only when both agree (p. 6).
- Losses happen: the Amazon long hit its stop a couple of days after entry (p. 70).

## What the author says does not work
- Trading a divergence without trendline-break confirmation (pp. 46, 69).
- Textbook CCI overbought/oversold signals (p. 36). The +/-100 zone gives too many false signals, so use +/-150 (p. 40).
- Divergence whose second extreme extends beyond 1.618 of AB: cancelled (p. 41).
- Buying right after a gap up (p. 31). Going against the main trend for small rebounds (p. 19), which
  sits awkwardly next to his counter-trend divergence strategy.
- Below-4h timeframes for this strategy (p. 62).

## How it is coded

The strategy is `cci_divergence` in `src/aitrader/strategies/divergence.py`, which follows these notes (3-bar pivots, lows 5-60 bars apart, entry within 20 bars, one 127.2% target). The original reading notes are kept below.

### Implementation notes for a divergence feature
- `bullish_cci_divergence(period=20, zone=-150, pivot=3, min_sep=5, max_sep=60)` should return true on the
  bar where the second low C is confirmed and: low[C] < low[A]; cci[C] > cci[A]; cci[A] < -150;
  cci[C] < -150 (with optional tolerance); max(cci[A..C]) <= 0 (optional tolerance);
  1.13 <= (B - C)/(B - A) <= 1.618, with B = highest high between A and C.
- It also needs `trendline_break_up`: a line through the last two swing highs before or at C (or B and the
  first lower high after B), with the signal on a close above the line's projected value. It should
  fire within N bars of C (suggest <= 20 daily bars) and before price makes a low under C.
- Expose B and C so the stop (C minus buffer) and targets (C + 1.272/1.618*(B-C)) can be computed.

## Claims to treat with caution
- "will give you a high percentage of profitable trades" / "very high percentage of trades closed in
  gain" (pp. 6, 69): no statistics, sample size or backtest given.
- CCI gives "excellent quality signals", better than other oscillators (pp. 40, 69): stated, not shown.
- The ~16 example charts (pp. 50-61) are hand-picked winners. Only one loser is shown (p. 70).
- "The strategy works on all the time-frames" (p. 69) contradicts "never on the lower ones" (p. 62).
- Fig. 48 caption says "bearish" while the text says bullish (p. 55). The ToC skips chapter 5 and
  mentions "Harmonic Patterns" (p. 34) that never appear. These look like editing leftovers.
