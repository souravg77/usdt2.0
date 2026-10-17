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
