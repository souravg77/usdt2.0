# Agents A–L

The master prompt's twelve roles are classes in `src/aitrader/agents/core.py`. Each one takes typed
inputs, returns one typed message (`agents/messages.py`, schema version in every message, plain
JSON via `as_dict`) and logs under its own name (`aitrader.agents.A` … `aitrader.agents.L`), so a
problem in one shows up with its letter and doesn't break the others.

| Agent | Class | Message | What it does |
|---|---|---|---|
| A Market data | `MarketDataAgent` | `MarketSnapshot` | missing, duplicate and out-of-order candles; newest candle more than 2 bars old; one-bar moves over 8 ATR; price stream quiet for over 15 s; spread and depth within 1% of the mid from an order book |
| B Indicators | `IndicatorAgent` | `FeatureReport` | the 20 standard indicators (docs/indicators.md), the share that are valid, and uncertainty (1 − mean confidence) |
| C Trend | `TrendAgent` | `TrendView` | votes of EMA, SMA, MACD, ADX, Supertrend and Ichimoku; strength (ADX); agreement across timeframes |
| D Reversal | `ReversalAgent` | `ReversalView` | range, RSI divergence, oscillator zones; a candidate only when an extreme is confirmed by a divergence or a rejection bar |
| E Breakout | `BreakoutAgent` | `BreakoutView` | squeeze, ATR expansion, volume over 2× average, Donchian/Bollinger/Keltner breakouts, failed breakouts |
| F Derivatives | `DerivativesAgent` | `DerivativesView` | basis, mark premium, funding and its change, open-interest change, distance to liquidation, perp round-trip cost |
| G Regime | `RegimeAgent` | `RegimeView` | TRENDING_BULL, TRENDING_BEAR, RANGING, HIGH_VOLATILITY, LOW_LIQUIDITY or UNCERTAIN; which strategies may trade in it; a no-trade decision |
| H Research | `ResearchAgent` | `ResearchReport` | walk-forward folds, ±20% parameter sensitivity, a fingerprint of the candles used |
| I Portfolio | `PortfolioAgent` | `PortfolioView` | picks ranked pairs with at most 2 from the BTC cluster and 2 from the ETH cluster (correlation ≥ 0.7), skips wide spreads |
| J Risk | `RiskAgent` | `RiskDecision` | the gatekeeper: refuses entries without a stop or a resting stop, after the daily/weekly loss cap, beyond position and exposure limits; 0.25% of equity per trade; leverage at most 3× |
| K Execution | `ExecutionAgent` | `ExecutionReport` | quantity and price on the exchange grid (quantities round down), idempotent `orderLinkId`s, ledger vs account (shortfalls and unknown holdings) |
| L Auditor | `AuditorAgent` | `AuditReport` | live vs backtest expectancy (flags more than 2 standard errors below), live vs paper slippage, the strategy's promotion-ladder state |

`agents.analyse(df, exchange, symbol, timeframe)` runs A–G on a pair's closed candles and asks
each strategy for a signal on the newest bar. It returns a `Board`: every agent's message, the
candidates (strategy, side, entry, stop, target, confidence) and, for each strategy that didn't
signal, the reason (bad market data, the regime's no-trade decision, a no-trade condition, or no
setup on this bar). Confidence is the share of the trend, breakout and reversal views that agree
with the signal times the regime's confidence: a description, not a chance of winning.

Nothing in `agents/` places an order. The executor still sizes through the risk engine, puts a
resting stop on Bybit, and needs the live lock for real money.
