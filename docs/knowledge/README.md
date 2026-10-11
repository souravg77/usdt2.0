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
