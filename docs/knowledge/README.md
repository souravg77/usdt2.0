# Knowledge base

These notes are what the agent learned from the books Shawn supplied, written in our own words
so they can live in the repository. The books themselves are not committed (they are copyrighted);
`aitrader kb build` indexes your own copies locally so the AI layer can quote them.

| Note | Covers | Main sources |
|---|---|---|
| [01-blockchain-and-bitcoin.md](01-blockchain-and-bitcoin.md) | Ledgers, hashing, blocks, mining, proof of work and stake, double spending, confirmations and finality, forks, 51% attacks, fees, MEV | Chiu & Koeppl; Binance Academy; US Treasury |
| [02-money-and-trust.md](02-money-and-trust.md) | What money does, why trust matters, how crypto compares as store of value and means of exchange, the cost of Bitcoin's design | Deloitte / Gruen; Chiu & Koeppl |
| [03-crypto-ecosystem.md](03-crypto-ecosystem.md) | Coins vs tokens, stablecoins, NFTs, DeFi, wallets and keys, custody, exchanges (CEX/DEX), miners and validators | US Treasury; Binance Academy |
| [04-risks-and-market-integrity.md](04-risks-and-market-integrity.md) | Fraud, hacks, wash trading, front-running, conflicts of interest, custody and insolvency risk, operational risk | US Treasury; Binance Academy |
| [05-trading-basics.md](05-trading-basics.md) | Markets, orders, order books, spread and liquidity, fees, risk management, trading styles, chart reading, indicators | Binance Academy; Finance Illustrated ebook |
| [06-swing-trading-larry-swing.md](06-swing-trading-larry-swing.md) | FORCESWING scans, Force Index, buy stops above the prior high, 1/15 position sizing | Larry Swing, *A Practical Guide to Swing Trading* |
| [07-ten-strategies-porwal.md](07-ten-strategies-porwal.md) | Inside bar, doji, engulfing, trendline and Darvas breakouts, opening range, 52-week high, ADX + EMA14, Quasimodo, bear trap | Nikhil Porwal, *10 Most Profitable Trading Strategies* |
| [08-divergence-trading-carli.md](08-divergence-trading-carli.md) | CCI divergence with zone, zero-line and Fibonacci rules, trendline-break entry, extension targets | David Carli, *The Power of Divergence* |
| [09-nine-strategies-sadowski.md](09-nine-strategies-sadowski.md) | MA cross, Heikin-Ashi + stochastic, range breakout, candle reversals, role reversal, BB squeeze, NR7, RSI(2) | Roman Sadowski, *9 Advanced and Profitable Trading Strategies* |
| [10-day-trading-warrior.md](10-day-trading-warrior.md) | Momentum day trading: bull flags, flat tops, relative volume, reversals, risk rules, screens | Warrior Trading, *Day Trading Guide For Beginners* |
| [11-system-development-cardoza.md](11-system-development-cardoza.md) | Building and testing systems: R multiples, expectancy, System Quality, walk-forward, Monte Carlo, bias | David Cardoza, *Introduction to Trading System Development* |
| [12-price-action-strategies-woods.md](12-price-action-strategies-woods.md) | Price-action setups: inside bars, NR7, three-bar reversal, trend bar failure, M2B, Hikkake, engulfing, volume pullbacks | Galen Woods, *How to Trade with Price Action: Strategies* |
| [13-price-action-master-woods.md](13-price-action-master-woods.md) | Price-action trading with one moving average: inside-bar statistics, pullbacks, pin-bar re-entry, NR7, Hikkake | Galen Woods, *How to Trade with Price Action: Master Edition* |
| [14-ml-strategy-design-asgari-khasteh.md](14-ml-strategy-design-asgari-khasteh.md) | Machine learning for 4h crypto direction: k-NN, Random Forest, XGBoost, 60-candle inputs, fee-threshold labels, results and limits | Asgari & Khasteh, *Profitable Strategy Design for Trades on Cryptocurrency Markets with Machine Learning Techniques* |
| [rules-from-books.md](rules-from-books.md) | Every concrete rule we took from the books and where it lives in the code | All |

## Sources

1. Binance Academy, *Cryptocurrency trading* eBook (supplied twice; the two files are identical).
2. Jonathan Chiu and Thorsten V. Koeppl, *The Economics of Cryptocurrencies: Bitcoin and Beyond*,
   Bank of Canada / Queen's University working paper, September 2018.
3. Deloitte, *The Future of Exchanging Value:
   Cryptocurrencies and the trust economy* (foreword by Nicholas Gruen), around 2015.
4. U.S. Department of the Treasury, *Crypto-Assets: Implications for Consumers, Investors, and
   Businesses*, September 2022.
5. Finance Illustrated, *Crypto Trading For Beginner Traders* (2021), covered in
   [../strategies.md](../strategies.md).
6. Larry Swing, *A Practical Guide to Swing Trading*.
7. Nikhil Porwal, *10 Most Profitable Trading Strategies* (2023).
8. David Carli, *The Power of Divergence* (5th ed., 2020).
9. Roman Sadowski, *9 Advanced and Profitable Trading Strategies*.
10. Warrior Trading, *Day Trading Guide For Beginners*.
11. David Cardoza, *Introduction to Trading System Development* (WPI project report).
12. Galen Woods, *How to Trade with Price Action: Strategies* (2014).
13. Galen Woods, *How to Trade with Price Action: Master Edition* (2014).

Notes 06-13 were written by reading each book in full; their strategies are in
`src/aitrader/learn/library/` (see [rules-from-books.md](rules-from-books.md)).

## What "training" means here

Reading a book does not change how a trading model behaves. The books are used in three ways:

1. **Rules.** Concrete, testable rules from the books (volume confirms a move, stop-market
   exits, StochRSI 0.8/0.2, the Bollinger squeeze, Parabolic SAR trailing stops, spread as a
   liquidity check) are written into the strategy code and backtested like any other rule.
2. **Reference.** `aitrader kb build` turns these notes plus your PDFs into a local search index.
   `aitrader kb search "double spending"` returns the matching passages with their source and
   page. The Phase 4 AI layer uses the same search to explain trades and answer questions.
3. **Guardrails.** The risk sections become checks: spot only, no leverage, trade-only API keys,
   wide-spread and thin-book markets skipped.

The part of the agent that actually *learns* (the Phase 4 trade filter) is trained on backtest
and paper-trading results, not on book text, because only those show whether a rule makes money.
