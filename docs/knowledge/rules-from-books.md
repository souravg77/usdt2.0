# Rules taken from the books

Every concrete rule we found and where it lives. A rule is only kept if the backtest says it
helps; these are the starting values.

| Rule | Book | In the code |
|---|---|---|
| A move should be confirmed by volume (Dow Theory, "volume precedes price") | Binance Academy | `momentum_breakout` and `bollinger_squeeze` need volume >= 1.5 x its 20-bar average |
| Bollinger squeeze: low volatility is followed by a breakout | Binance Academy | New `bollinger_squeeze` strategy (long side only in the pilot) |
| Parabolic SAR as a trailing stop in strong trends | Binance Academy | `bollinger_squeeze` exits on a close below the SAR (`trailing="close<psar"`) |
| VWAP above = bullish | Binance Academy | `bollinger_squeeze` entry needs close above the rolling VWAP |
| StochRSI above 0.8 overbought, below 0.2 oversold | Binance Academy | `trend_pullback` records StochRSI as a feature for the Phase 4 filter; `buy_the_dip` records it too |
| RSI above 70 overbought, below 30 oversold | Binance Academy, Finance Illustrated | Already in all three original strategies |
| Stop at the invalidation point, decided before entry | Binance Academy | Every `Signal` carries a stop |
| Stop-market is safer than stop-limit | Binance Academy | Exits are modelled as stop-market; the backtest fills gaps at the open |
| Small spread = liquid market; thin books cause slippage | Binance Academy | `strategies.filters.liquidity_check` (max spread 0.10%, min depth on each side) |
| Paper trade first, including fees | Binance Academy | Paper mode is the default; backtests charge fees and slippage |
| Leverage moves the liquidation price close to entry | Binance Academy, Treasury | No leverage in the pilot (unchanged) |
| Reported volume can be wash trading | Treasury | Volume rules limited to major pairs on Binance and HTX |
| Keep less money on the exchange; custodians can fail | Binance Academy, Treasury | Documented in `docs/ubuntu-setup.md`; trade-only keys |
| Fibonacci retracement levels | Binance Academy | `indicators.fib_levels` (helper for later targets; not used in a rule yet) |
| Ichimoku cloud | Binance Academy | `indicators.ichimoku` (feature only) |

The economics and policy books (Chiu & Koeppl, Deloitte, Treasury) don't contain trading rules.
They feed the reference notes and the risk guardrails above.

## Strategy books (second batch)

Six more books were read in full. Their strategies are rule specs in
`src/aitrader/learn/library/`, one file per book. `aitrader learn add-library --test` registers
them and sends each one through the same backtest gate and paper stage as everything else.
Each spec cites its page and quote, lists the choices made where the book is vague
(`decisions`), and lists what couldn't be coded (`uncodable`). Run `aitrader learn show <id>`
to read them.

Carli's CCI divergence has too many linked rules for the rule language, so it is a hand-coded
strategy, `cci_divergence` (`src/aitrader/strategies/divergence.py`).

| Strategy | Book | Timeframes | Entry, in short |
|---|---|---|---|
| `inside_bar_trend_continuation` | 10 Most Profitable Trading Strategies | 1h, 4h, 1d | Inside bar after a mother bar inside a higher-timeframe uptrend; buy when a candle closes above the mother bar |
| `inside_bar_oversold_reversal` | 10 Most Profitable Trading Strategies | 4h, 1d | Inside bar at a market bottom with the oscillator oversold; buy on a close above the mother bar. |
| `doji_bottom_reversal` | 10 Most Profitable Trading Strategies | 4h, 1d | Doji at a market bottom with oscillator oversold; buy when price closes above the doji's high, stop below the  |
| `bullish_engulfing_oversold` | 10 Most Profitable Trading Strategies | 4h, 1d | Bullish engulfing in a downtrend with stochastics oversold; buy on breakout above the engulfing candle's high, |
| `trendline_breakout_lower_high` | 10 Most Profitable Trading Strategies | 4h, 1d | After a downtrend of lower highs, buy once price closes above the previous lower high with a volume spike, rat |
| `darvas_box_volume_breakout` | 10 Most Profitable Trading Strategies | 1h, 4h, 1d | Consolidation box breakout: buy a close above the box top on a volume surge after a tight range; stop below th |
| `opening_range_breakout_15m` | 10 Most Profitable Trading Strategies | 5m, 15m | Turbocharged market opening: break of the first 15-minute range in the direction of the previous day's trend,  |
| `high_52w_breakout` | 10 Most Profitable Trading Strategies | 1d | 52-week-high momentum: buy a daily close at a new 52-week high on rising volume, long side only. |
| `adx_ema14_trend_intraday` | 10 Most Profitable Trading Strategies | 5m, 15m | ADX-EMA: buy when ADX(14) is between 20 and 50, +DI above -DI, price above EMA(14) and the previous day trende |
| `quasimodo_bullish_higher_low` | 10 Most Profitable Trading Strategies | 15m, 30m, 4h | Bullish Quasimodo: after a downtrend, price breaks the last lower high (higher high), then pulls back to make  |
| `bear_trap_support_reclaim` | 10 Most Profitable Trading Strategies | 15m, 1h, 4h | Bear trap: price breaks below 20-bar support, then reclaims it with a volume spike and a bullish candle; buy a |
| `ma_cross_20_60_trend100` | 9 Advanced and Profitable Trading Strategies | 4h, 1d | Buy when the 20-period MA crosses above the 60-period MA, with price above the 100-period trend MA; exit when  |
| `heikin_ashi_two_green_stoch_reversal` | 9 Advanced and Profitable Trading Strategies | 1d | Daily Heikin-Ashi reversal: two completed green HA candles after a run of red ones, Stochastic(14,7,3) coming  |
| `swing_correction_range_breakout` | 9 Advanced and Profitable Trading Strategies | 4h, 1h | In an uptrend, wait for a tight overlapping correction (contracting volatility), then buy when two candles clo |
| `bullish_engulfing_next_candle` | 9 Advanced and Profitable Trading Strategies | 1h, 4h, 1d | Buy after a bullish engulfing candle; stop at the engulfing candle's low; trail with the trend. |
| `long_lower_shadow_reversal` | 9 Advanced and Profitable Trading Strategies | 1h, 4h, 1d | Buy the open after a candle whose lower shadow is at least twice its real body at a turning point; stop at the |
| `hammer_reversal` | 9 Advanced and Profitable Trading Strategies | 1h, 4h, 1d | Buy the open after a hammer (small body at the top of the candle, long lower handle); stop at the hammer low,  |
| `role_reversal_pullback_20ma` | 9 Advanced and Profitable Trading Strategies | 1h, 4h, 1d | In an uptrend (higher highs/lows) after a breakout to a new high, buy the pullback to the 20 MA / broken-resis |
| `bb_squeeze_mid_break` | 9 Advanced and Profitable Trading Strategies | 4h, 1h | After a Bollinger(20,2) squeeze, buy when a full candle closes above the middle band; stop at the prior candle |
| `narrow_range_nr7_breakout` | 9 Advanced and Profitable Trading Strategies | 1d, 4h | Buy when price breaks above the high of the narrowest-range candle of the last 7 (or 4) bars; stop at that can |
| `rsi2_momentum_above_90` | 9 Advanced and Profitable Trading Strategies | 1h, 4h, 1d | Momentum use of RSI(2): buy when RSI(2) moves above 90 in an uptrend; exit on the opposing signal (RSI(2) belo |
| `momentum_reversal_stoch_support` | 9 Advanced and Profitable Trading Strategies | 1d, 4h | Daily setup: after a clear decline, with daily (and weekly) stochastic oversold at a major support zone, buy o |
| `forceswing_long` | A Practical Guide to Swing Trading | 1d, 4h | Author's daily FORCESWING LONG scan: uptrend (close above SMA10 and SMA20), 3-bar lower-high pullback, Force I |
| `forceswing_long_ride_the_wave` | A Practical Guide to Swing Trading | 1d, 4h | Same FORCESWING LONG setup, but exit managed only by a trailing stop to approximate the book's 'ride the wave' |
| `forceswing_case_study_variant` | A Practical Guide to Swing Trading | 1d, 4h | Setup used in the book's case studies: close above SMA50 and SMA20, 3-bar lower-high pullback, FI-3MA <=0 and  |
| `master_plan_full_criteria_long` | A Practical Guide to Swing Trading | 1d, 4h | All chapter-5 uptrend/pullback criteria combined: close above SMA10 and SMA20, SMA10 above SMA20, 3-bar lower  |
| `bull_flag_ma_pullback` | Day Trading Guide For Beginners | 5m, 15m | Ross Cameron bull flag: after a strong run, a light-volume pullback to the 10/20 MA, entry when volume returns |
| `flat_top_breakout` | Day Trading Guide For Beginners | 5m, 15m | Breakout over a flat resistance level (in the book, premarket highs) on high relative volume; buy-stops of sho |
| `momentum_relvol_breakout` | Day Trading Guide For Beginners | 5m, 15m, 1h | Momentum candidate (above MAs, 2x relative volume, no nearby resistance) breaking to new highs. |
| `reversal_rubber_band_long` | Day Trading Guide For Beginners | 5m | Long side of the reversal strategy: a stock at new intraday lows after 5-10 straight red candles with extreme  |
| `breakout_retest_hold` | Day Trading Guide For Beginners | 15m, 1h, 4h | Break of a defined resistance level on high relative volume, then a retest of the level that holds; enter on t |
| `support_bounce` | Day Trading Guide For Beginners | 1h, 4h, 1d | Buy a bounce at a defined support level; exit if support breaks. |
| `daily_new_high_breakout_atr_stop` | Day Trading Guide For Beginners | 4h, 1d | Go long on a breakout to new highs on the daily chart with a 2.5x ATR stop and 1% account risk (NVDA example). |
| `trend_bb50_stop_long` | Day Trading Guide For Beginners | 1d | Multi-month trend long (SPY example) with the stop at the lower Bollinger Band of 2 SD around the 50 SMA. |
| `rsi_oversold_to_50ma` | Day Trading Guide For Beginners | 15m, 1h, 4h | Buy when RSI moves into oversold territory and take profit when price reaches the 50-period moving average; st |
| `trend_pullback_broad_screen` | Day Trading Guide For Beginners | 1d | Book's broad FinViz trend-pullback screen: price above the 20-day SMA, within 5% of the 50-day high, down on t |
| `sma5_streak_pullback` | Day Trading Guide For Beginners | 4h, 1d | Book's narrow mechanical trend-pullback screen: above the 20 SMA, closed above the 5 SMA for 7+ days, closes b |
| `trend_pullback_plan` | Day Trading Guide For Beginners | 1h, 4h | Written trend-pullback plan: higher highs and lows with momentum, shallow light-volume pullback to a moving av |
| `cardoza_trendline_support_pullback` | Introduction to Trading System Development | 4h, 1d | Strategy 1 of the book: in an uptrending growth asset, buy when price dips to trendline support while the slow |
| `cardoza_sma_crossover_22_34` | Introduction to Trading System Development | 4h, 1d | Strategy 2 of the book: buy when the fast SMA crosses above the slow SMA, exit when it crosses back under; len |
| `cardoza_smoothies_range_support_buy` | Introduction to Trading System Development | 1h, 4h, 1d | Generic 'smoothies' support/resistance trade from the book's overview: buy when price reaches support expectin |
| `cci_divergence` | The Power of Divergence | 1d, 4h | Bullish CCI(20) divergence (zone, zero-line and Fibonacci rules), close above the falling trendline |

New rule-language features added for these books: CCI, slow stochastic, Force Index, +DI/-DI,
Heikin-Ashi colour, NR7 / bar range, range-to-ATR, candle patterns (bullish engulfing, hammer,
doji, inside bar, lower-shadow ratio), candle and close-above/below-MA streaks, bullish RSI / CCI
/ MACD divergence, and exit conditions (`exit.when`).

### Not coded yet

These appear in the books but need data or order types the bot doesn't have. They are listed
in each spec's `uncodable`:

- Partial profit taking, break-even stop moves, pyramiding and scaled entries (one entry and one
  exit per trade for now).
- Buy-stop entries at the prior high: approximated by "close above the prior high, buy at the
  next open".
- Hand-drawn trendlines, horizontal zones counted by touches, bull flags, Darvas boxes,
  Quasimodo labels: approximated with rolling highs and lows where possible.
- Pre-market highs, news catalysts, share float, Commitments of Traders, weekly charts and
  multi-timeframe filters.
- Stop-trading-for-the-day after two losses (needs a portfolio-level loss counter; Phase 4 risk
  engine).
- Every short-side setup (no shorting in the pilot).

### Testing lessons from Cardoza

Cardoza's report shows how easy it is to fool yourself. Its headline profits come from per-stock
in-sample optimisation on stocks picked with hindsight, with no commissions or slippage, and its
own Monte Carlo found the edge unlikely to be real. Our gate already avoids those traps: fees and
slippage are always charged, the decision uses out-of-sample data, and the last 20% of history
stays untouched. His System Quality score (expectancy x sqrt(N) / stdev of R) is a useful extra
ranking for strategies that pass, and is a candidate for the Phase 4 allocation step.
