# A Practical Guide to Swing Trading — Larry Swing (mrswing.com) — notes (id cd611cca)

Market: US stocks (NYSE/AMEX/NASDAQ), daily end-of-day charts, circa 2002. Long and short
swings; the short side needs a margin account. Read in full: 75 pdftotext pages (74 form feeds).
Much of the book is promotion for the author's software (SwingTracker), newsletters
(MasterSwings, BestSwings, MrSwing Lite) and brokers; the actual trading content is
concentrated in ch. 5, 6, 8 and 10.

## Core idea (p. 7, p. 16-18)
- Trade in the direction of the trend, wait for a pullback, and enter only when price shows a
  sign of resuming the trend. Swing = several days to a few weeks; scans look for a 1-5 day horizon (p. 54).
- Uptrend = higher highs and higher lows (saw-tooth); buy the pullback. Downtrend = lower highs
  and lower lows; sell short the "pull-up" (p. 16-17).
- Rules are meant to be fully mechanical so fear and greed cannot interfere (p. 26).
- "The secret ... Confidence, Discipline, Focus and Patience" (p. 26).

## Universe filters (p. 18, p. 24, p. 55)
- Price at least $7 (p. 18) or $12 (p. 24, scan default; $7 "depending on market conditions").
- 20-day average volume >= 500,000 shares (sometimes 250,000). Reason: low-price, low-volume
  stocks are easier for market makers to manipulate.

## Trend / pullback measures (ch. 5, p. 24-25)
- Simple MAs of close: 10, 20, 50 days only (p. 40).
- Uptrend signs: close above SMA10 and SMA20; SMA10 above SMA20.
- 3-day pullback: today's high < yesterday's high AND yesterday's high < high two days ago.
- Force Index (Elder): FI = volume_today * (close_today - close_yesterday) (p. 42).
  Smoothed with a 3-day MA and a 13-day MA ("Larry's Index").
  Long setup: FI-3MA <= 0 (bears win short term) and FI-13MA >= 0 (bulls in control longer
  term) (p. 25, p. 43). Short setup is the mirror (p. 43).
- DMI (Wilder): uptrend confirmed when ADX > 30 ("the higher the better") and +DI > -DI
  (p. 25, p. 44). Book calls it "ADX20" and describes ADX as a 20-day average.
- Claim: market trends only ~30% of the time, ranges ~70% (p. 44) — hence the ADX filter.

## FORCESWING LONG scan — the author's "first scan every trading day" (p. 54-55)
MAV20 >= 500000 AND CLOSE > 12 AND CLOSE > SMA10 AND CLOSE > SMA20 AND HIGH < HIGH1 AND
HIGH1 < HIGH2 AND FORCE3 <= 0 AND FORCE13 >= 0 AND ADX20 > 30.
- The case studies (p. 60-62) use a variant: CLOSE > SMA50 AND CLOSE > SMA20, 3-day lower highs,
  FORCE3 <= 0, FORCE13 >= 0 (no ADX, no volume/price filter stated).
- The SHORT scan (p. 56) mirrors it: close < SMA10 and SMA20, LOW > LOW1 > LOW2, ADX20 > 30.
  Note: its printed force condition is the same as the long scan (FORCE3<=0, FORCE13>=0),
  which contradicts the stated logic and p. 43 (short wants FI-3MA >= 0, FI-13MA <= 0) — likely a typo.

## The Master Plan — entry and exit (ch. 6, p. 26-32)
- Scan after the close; orders placed next morning, usually within the first 30 minutes (p. 28).
- Gap definition: open >= $0.50 above (gap up) or below (gap down) prior close (p. 28).
- No gap: place order a few minutes after open; buy stop $0.06 (1/16) above PREVIOUS day's high.
- Gap in trade direction (gap up for long): wait 30 min; buy stop $0.06 above TODAY's high so far.
- Gap against trade (gap down for long): wait ~5 min; buy stop $0.06 above today's high so far.
- "Buying AFTER the open is BETTER" (p. 28). The buy stop ensures the stock is already moving
  in the trade's direction; if it keeps pulling back, no trade is placed (p. 19).
- Unfilled order: repeat daily for up to 5 trading days (p. 30).
- After the fill (p. 29), place an OCO pair:
  - Profit: sell limit for HALF the shares at entry +7%.
  - Stop: sell stop at the HIGHER (tighter) of entry -4% or $0.06 below the low of the reference
    day (previous day if no gap, today's pre-entry low if gap).
- Daily trailing (p. 30): each day, if (reference low - $0.06) is above the current stop, raise
  the stop to it. Gap days use today's low after waiting (30 min gap down / 5 min gap up).
- Second half rides the wave and exits only on the trailing stop (prior-day low - $0.06) (p. 32).
- Max loss per trade 4%; target ~7%; author says actual wins often exceed 7% and losses are often
  under 4% (p. 27). Short swing: exact mirror (sell stop 6c under low, buy stop 4% above /
  6c above high, buy limit for half at -7%) (p. 32-33).
- Market direction matters: when the whole market moves against you, more trades lose; the stop
  is the protection (p. 27).

## Money management (p. 7-8, p. 51)
- Split capital into 15 equal slices, one per trade (6.66% of capital each); can go to 20
  positions, or start with 2-3. Slice size grows with the account.
- With a 4% max stop on a 1/15 slice, account risk per trade is about 0.27%.
- Pick ~20-25 candidates daily, place more buy-stop orders than free slots; the first ones to
  trigger get the capital ("the first 15 (of the 25 stop orders) that pass the swing test").
  Make sure the broker rejects orders when cash runs out.
- Margin optional; cash-only is the conservative choice.

## Volume and other TA notes (ch. 8, p. 36-45)
- "You trade people, not stocks" (p. 36). Volume shows intensity of a move.
- Strong uptrend: higher volume on up legs, lower on corrective legs (mirror for downtrends).
- High volume typical at tops, at breakouts from ranges, and in panic before bottoms (p. 38).
- Equivolume (Arms): box height = range, width = volume. Short-wide boxes -> turning points;
  tall-narrow -> established trends; "power box" (both larger) confirms breakouts; narrow
  box on a breakout casts doubt (p. 39).
- Volume table (p. 39): reversal = above-average volume with little price movement, or after a
  huge advance/decline; continuation = above-average volume with strong move or breakout, or
  below-average volume with no price movement.
- Trend checklist (p. 40-41): uptrend = HH/HL, rising MA10 and MA20, closes above MA20 & MA50,
  support at the 20 or 50 day MA. Downtrend is the mirror.
- Up/Down/In/Out bar colouring (p. 44-45): Up = higher high and higher low; Down = lower high
  and lower low; In = inside bar; Out = outside bar. Descriptive only, no rule attached.
- Candlesticks mentioned only historically; no patterns taught.

## Psychology / discipline
- Mechanical rules remove fear and greed; patience — "profits come slowly, but surely" (p. 9).
- No need to watch positions intraday: entry stop + OCO exits, adjust stops once a day (p. 10).
- "Only trade what you like!" — final discretion over scan output (p. 55, p. 59).

## What the author says doesn't work / avoid
- Buy-and-hold (losing money "more common than we would like to admit", p. 9).
- Low-price / low-volume stocks (manipulation) (p. 24).
- Entering at the open; entering a stock still moving against you (the buy stop filters these).

## Case studies (p. 60-62), all longs, 2002
- DFXI: entry 42.48, stop 41.35, half at +7% (45.45), rest trailed out 43.44 -> +4.63% avg.
- PFG: entry 27.26, target never hit, trailed out 27.94 -> +2.49%.
- PTEN: entry 26.84, half at 28.72, rest trailed out 29.99 -> +9.37%.
- Pages 63-68 are blank (charts lost in text extraction).

## Crypto translation notes
- $0.06 / $0.50 offsets are stock-tick-specific; for crypto use a fraction of price or ATR.
- No market open/gaps in 24/7 crypto: the gap and wait-N-minutes rules have no direct meaning;
  daily candle boundary (00:00 UTC) can stand in for "the open".
- Share-volume and $-price filters do not translate; use a quote-volume liquidity filter instead.

## Claims to treat with caution
- "thousands of investors" use the Master Plan and testimonials such as "up over $5000 in one
  month" (p. 7, p. 13) — anecdotal, unverified, promotional.
- "we enjoy a high percentage of winning trades because the charts pattern used have been
  back-tested" (p. 49) — no backtest statistics, sample size or period are given.
- "Being disciplined ... will insure that profits exceed losses" and "you will make money"
  (p. 16, p. 19) — no evidence; a 7%/4% partial target does not by itself guarantee positive expectancy.
- "trade successfully with very little risk" (p. 9) — unsupported.
- Market trends only ~30% of the time (p. 44) — stated as fact, no source.
- Only three hand-picked winning case studies are shown; no losing trades are presented.
