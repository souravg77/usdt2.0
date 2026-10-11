# Trading basics

Sources: Binance Academy trading eBook; Finance Illustrated *Crypto Trading For Beginner Traders*.

## Trading vs investing, and two kinds of analysis

- **Trading** aims at shorter-term profits from price moves; **investing** holds for years.
- **Fundamental analysis (FA)** asks what an asset is worth: for crypto, adoption, network
  activity, team, tokenomics, supply schedule.
- **Technical analysis (TA)** studies price and volume, assuming past patterns repeat. It is
  also a risk-management tool because it gives clear invalidation points. The agent is mainly TA,
  with news and calendar filters on top.
- Markets are driven by **supply and demand**, which respond to news, sentiment, regulation,
  macro data and liquidity.

## Trends and cycles

- A **trend** is the general direction: higher highs and higher lows in an uptrend, the reverse
  in a downtrend. Bull markets are long uptrends, bear markets long downtrends.
- **Market cycles** repeat in phases: accumulation, run-up (public participation), distribution,
  and decline. Sentiment swings from fear to euphoria and back.
- **Dow Theory:** prices reflect known information; trends are primary, secondary and minor;
  a trend should be **confirmed by volume** and by related markets; a trend stays valid until a
  clear reversal.
- **Elliott Wave:** five waves with the trend and three corrective waves against it, repeating at
  every scale. Popular but subjective, so the agent does not use it.
- **Wyckoff:** supply and demand, cause and effect, effort (volume) vs result (price). Its five
  steps: find the trend, pick strong assets, check there is enough "cause", check the move is
  likely (price and volume), time the entry.

## Trading styles

| Style | Holding time | Notes |
|---|---|---|
| Scalping | Seconds to minutes | Targets under 1% per trade, many trades; fees dominate. Not for beginners |
| Day trading | Within a day | TA on 5m to 1h charts. `momentum_breakout` |
| Swing trading | Days to weeks | TA plus some FA. `trend_pullback`, `bollinger_squeeze` |
| Position / trend trading | Weeks to months | Rides the main trend |
| Buy and hold (HODL) | Years | `buy_the_dip` is the active version |
| Paper trading | Any | Practice with fake money first, including fees. The agent's default mode |

Diversification across uncorrelated assets (Modern Portfolio Theory) lowers overall risk.
Most crypto moves together with BTC, so holding many altcoins gives less diversification than it
seems.

## Long and short

Going **long** means buying in the hope the price rises. Going **short** means selling (often
borrowed coins) to buy back cheaper. Short selling needs margin or derivatives and is off in the
pilot.

## Orders and the order book

- The **order book** lists open buy (bid) and sell (ask) orders by price; the **matching engine**
  pairs them. **Depth** is how much size sits near the price.
- **Market order:** fills now at the best available prices. On a thin book it walks up the book,
  and the difference from the expected price is **slippage**.
- **Limit order:** fills only at your price or better, but might never fill.
- **Stop-loss order:** activates at a stop price. A **stop-limit** can be skipped in a crash
  because the price jumps past the limit. A **stop-market** always exits, so it is the safer
  stop. Put the stop at the trade's **invalidation point**, decided before entry.
- **Makers** add orders to the book (usually limit orders) and pay lower fees; **takers** remove
  liquidity (market orders) and pay more. Binance spot is about 0.1% per side before discounts.
- **Bid-ask spread:** the gap between best bid and best ask. A small spread means a liquid market.

## Reading charts

- **Candlesticks** show open, high, low and close for each period. Patterns (flags, triangles,
  wedges, hammers, stars, dojis) suggest setups with clear targets and invalidation points.
- **Trend lines** need at least two or three touches; more touches and higher timeframes are
  more reliable.
- **Support** is a price floor where buyers step in; **resistance** a ceiling where sellers do.
  A broken support often turns into resistance and the other way round.

## Indicators

- **Leading** indicators try to anticipate; **lagging** ones confirm. **Overlays** sit on price;
  **oscillators** move between bounds. No indicator predicts the future.
- **Volume:** "volume precedes price". A move on high volume is more trustworthy; one on low
  volume is weak. Price levels with heavy past volume often matter again.
- **RSI:** momentum from 0 to 100. Above 70 overbought, below 30 oversold. Divergence from price
  hints at a weakening move.
- **Moving averages:** SMA (simple) and EMA (more weight on recent prices). Golden cross (50 above
  200) is bullish; death cross bearish. They lag.
- **MACD:** difference of two EMAs with a signal line; crossovers and divergences show
  momentum shifts.
- **Fibonacci retracement:** levels 23.6%, 38.2%, 50%, 61.8% and 78.6% of a prior move, where
  pullbacks often pause.
- **Stochastic RSI:** the RSI's position within its own recent range, 0 to 1. Above 0.8
  overbought, below 0.2 oversold. Very sensitive, best near the extremes.
- **Bollinger Bands:** a 20-period SMA with bands two standard deviations away. Touching a band
  is not a signal by itself. A **squeeze** (bands very close) means low volatility, often followed
  by a big move in either direction.
- **VWAP:** average price weighted by volume. Price above VWAP leans bullish, below bearish;
  crossing it is used as a signal.
- **Parabolic SAR:** dots below price in uptrends, above in downtrends; a flip signals a
  reversal. Used as a **trailing stop**. Good in strong trends, noisy in ranges.
- **Ichimoku Cloud:** several lines (conversion 9, base 26, leading spans 52) forming a cloud that
  acts as support and resistance. Price above the cloud is bullish.

All of these are in `aitrader.indicators`.
