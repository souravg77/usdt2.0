# Introduction to Trading System Development — David Cardoza (WPI Interactive Qualifying Project)

Source file: 7af4f59e.txt, 42 pages (read in full, p. 1-42). Page numbers = printed footers, which match the form-feed count.
Type: undergraduate project report, not a trading manual. Market: **US large-cap stocks** (BA, UNH, MSFT, V, VZ, MA, PEP, NKE, SBUX, PYPL), **daily bars**, swing-trading horizon (days to weeks), backtested on TradeStation from 2010, long-only.
Text quality: clean, but **every figure is missing** (strategy code, Fig. 2 and Fig. 5; equity curves; Monte Carlo charts; the stats tables in Fig. 11-14). So the exact EasyLanguage rules and parameters for the trendline system are **not in the text**.

## Main ideas
- Purpose: build a rules-based, automated system to take emotion and "gut feel" out of trading (p. 4-5).
- Two strategies on the same CANSLIM-selected stocks, then combined into a "system of systems" with capital weighted by each strategy/stock pair's System Quality (p. 5, 32).
  1. Trend-following "smoothies": buy at trendline support, sell at trendline resistance (p. 18, 23-27).
  2. Moving-average crossover with per-stock optimized lengths (p. 24, 29-31).
- Optimization (walk-forward) and Monte Carlo are used "to determine if a system is likely to be profitable" (p. 19).
- Edge = positive expectancy; a system that cannot beat alternatives is not worth trading (p. 19).
- The author's personal objective: maximum profit with least drawdown, little market research, technical only (p. 17).

## Background / general concepts (mostly textbook)
- Trader vs investor mindsets; commissions hurt frequent traders (p. 7-8). Index return taken as ~5-10%/yr benchmark (p. 9).
- Efficient-market hypothesis (Fama) vs behavioral finance (Shiller): overreaction creates exploitable inefficiency (p. 9).
- Institutional accumulation drives price; buy what institutions are building (p. 10).
- Trading styles: scalp, day, swing (2 days to several weeks), long-term (p. 16-17). Author says swing is riskier than day trading because of longer holding (opinion, p. 17).
- A trading system must define entry rules, exit rules, position sizing and order types (p. 14).
- Support = level price falls to but not below; resistance = level price rises to but not above (p. 18).
- Breaking outside the support/resistance channel on a sharp move signals a trend change (p. 23).
- SMA vs EMA definitions; fast MA above slow MA = price has risen rapidly; slow above fast = recent sharp drop (p. 24).

## Stock selection (CANSLIM, quantified) — p. 22-23, 26-27
- C: current quarterly earnings high vs prior years. A: 5-year annual earnings growth positive. N: new product/event. S: supply/demand, proxied by volume. L: leaders, proxied by market cap. I: institutional ownership. M: market direction bullish.
- Concrete screen used (finviz): market cap > **$80B**; then of three tests — institutional ownership > **10%**, monthly performance > **5%**, annual performance > **10%** — must pass **at least 2 of 3** (p. 26).
- 29 stocks passed; **10** picked from different sectors for diversification (p. 26-27).
- Author admits look-ahead: assumes stocks that qualify today also qualified in 2010 (p. 18).
- Crypto translation idea: universe filter of top market-cap coins with 30-day return > 5% and 1-year return > 10% (2 of 3 with a liquidity/"ownership" proxy).

## Strategy 1 — trendline support/resistance ("smoothies") — p. 18, 23-27
- Trendlines drawn with TradeStation's TLValueEasy (inputs: start bar, target bar to project to, end bar) (p. 26). The bar counts are not given (in the missing Fig. 2).
- Buy when: **slow MA > fast MA** AND **today's open > fast MA** AND **low touches the trendline support** (p. 26). Note: literal text — slow above fast, i.e. buying a pullback, oddly combined with open above the fast MA.
- Sell when the opposite holds for each condition / price reaches the resistance trendline (p. 26-27).
- No protective stop in the system (author later says it needs one, p. 35-36).
- Hold time optimized with walk-forward; trading too often "overfits", too rarely takes profits too late (p. 25).
- Works when price rises steadily; underperforms when volatile and in bear markets (p. 26-28). Only expected to work on CANSLIM/growth stocks.

## Strategy 2 — moving-average crossover — p. 24, 29-31
- Fast MA crosses over slow -> trade; crosses under -> exit (long-only in effect, "not designed for shorting", p. 21).
- MA lengths optimized **per stock for profit**, search range **1 to 35 days**. On BA the optimum was **22 (fast) and 34 (slow)** days (p. 30).
- Book says longer MA lengths = lower risk but more compute; suggests testing a wider range (p. 30, 40).
- Author claims it worked in both runs and volatile periods (p. 31).

## Testing / validation rules (compare with our backtest gate)
- **R multiple (book's definition)**: trade net profit / **average loss of all trades** (p. 20). (Usual Van Tharp definition divides by initial risk; this is a different normalization.)
- **Expectancy** = sum of R multiples / number of trades = profit per dollar risked per trade (p. 20).
- **Expectunity** = expectancy x trades per year (annualized), used to compare systems with different trade frequency (p. 20).
- **System Quality (SQ)** = expectancy x sqrt(number of trades) / stdev(R multiples) (p. 20) — an SQN-style t-statistic.
- **Allocation rule**: only strategy/stock pairs with SQ > 0 get capital; weight_i = SQ_i / sum of positive SQs (p. 20, 32, 37). Negative-SQ pairs are dropped.
- **Walk-forward optimization**: optimize parameters in-sample, check out-of-sample; re-optimize over time because optimal parameters drift (p. 20-21).
- **Walk-forward efficiency (WFE)**: OOS performance relative to in-sample; 100% = OOS as good as IS. The text says above/below **50%** means likely better/worse — garbled, but 50% is the implied pass line (p. 21). Cluster analysis (TradeStation WFO) used to produce WFE.
- **Monte Carlo**: resample trades to estimate distribution of average return per trade and max drawdown (p. 19, 34-36).
- Data: daily bars from **2010**; starting capital **$100,000** (p. 18, 33).
- **Not modeled**: commissions, slippage, waiting periods (p. 6) — author says they must be considered before going live.
- **No minimum sample size, no max parameter count, no OOS % split, no drawdown limit** are stated.
- Per-stock optimization with parameters reported in-sample is what produced the headline profits — i.e. no hold-out confirmation is shown.

## Results reported (p. 32-39)
- System Quality table (Fig. 8): e.g. NKE S2 3.62 (20% weight), MA S2 2.21, MSFT S1 1.98, SBUX S1 1.90; negatives for UNH (both), VZ S1, V S2, MSFT S2, SBUX S2, PYPL S1.
- Portfolio Maestro, fixed params, all 10 stocks: 73.99% winners, average winner $129.33, **largest loss bigger than largest win**; historical average return per trade only **0.015%**, and Monte Carlo said a positive average return per trade was **unlikely** (p. 34-35).
- Excel per-stock-optimized, SQ-weighted: +$7,986; unweighted: +$105,219, with smaller equity drops (p. 38-39).

## Risk / money management
- No per-trade risk %, no position-sizing formula beyond SQ-weighted capital fractions.
- Diversification across 10 stocks in different sectors and two strategies reduced equity-curve drops (p. 32-33, 40).
- Author's own recommendations: add stop orders (sell below a set level), drop negative-SQ pairs, add strategy activation/suspension rules, test on non-growth and falling stocks (p. 33, 35-36, 40).

## Psychology
- Emotion, uncertainty and forgetfulness cause bad manual trades; automation enforces consistency (p. 4-5).
- Risk aversion and personal preference should shape the rules (time frame, style, sizing) (p. 6, 14).
- Monte Carlo helps, but "ultimately the trader must use his intuition" on whether a trade is logical (p. 19).

## What the author says does not work / limitations
- One-size-fits-all parameters across stocks performed poorly (Portfolio Maestro run) (p. 26, 29, 33).
- Trendline system fails in volatile and bear markets and on non-growth stocks (p. 26-28).
- MA crossover cannot handle unpredictable intraday moves; what worked one day may not work the next (p. 29).
- Value investing less useful in bull markets (p. 15).
- Too-frequent trading = overfitting (p. 25).

## Claims to treat with caution
- "It is proven that scientific analysis is an efficient method" / automated systems are "a profitable venture" (p. 4, 6) — not supported by OOS evidence.
- $105,219 profit (p. 39) and $7,986 (p. 38): parameters were optimized per stock on the same data (in-sample), stocks chosen with today's knowledge (survivorship/look-ahead), no commissions or slippage.
- 73.99% win rate and $129.33 average winner (p. 35) — the same run's Monte Carlo found a positive average return per trade unlikely, and average return per trade was only 0.015%.
- "Optimized such that trades are reliably profitable" (p. 29) — no out-of-sample figures given.
- WFE interpretation on p. 21 is internally inconsistent (100% vs 50%).
- Figure 8 inconsistency: PYPL strategy 2 shows SQ 1.67 but 0.00 allocation; the stock list on p. 32 names only 9 of 10 stocks.
- 22/34 MA lengths are a single-stock (BA) in-sample optimum, close to the edge of the 1-35 search range.
