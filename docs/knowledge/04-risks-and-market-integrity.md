# Risks and market integrity

Sources: US Treasury (2022), Binance Academy trading eBook.

## Four kinds of risk (Binance Academy)

- **Market risk:** the asset loses value. Reduced with stop-losses and position sizing.
- **Liquidity risk:** you can't find buyers (or sellers) near the current price. Reduced by
  trading only deep markets and checking the spread and order book first.
- **Operational risk:** human error, hardware or software failure, or fraud by insiders.
- **Systemic risk:** a big player fails and drags the sector down (Lehman in 2008; Celsius
  and Voyager in 2022). Reduced by diversification and by not leaving more money
  on an exchange than the strategy needs.

## Conduct risks (Treasury)

- **Fraud, scams and theft.** Losses reported to the FBI were over $1.6 billion in 2021. Common
  patterns: fake investment platforms, romance and "pig-butchering" scams, rug pulls
  (developers abandon a project and take the money), impersonation, scams that ask victims to
  pay at crypto ATMs, and hacks of exchanges, bridges and DeFi protocols.
  Older people and service members are frequent targets.
- **Missing disclosures.** Many platforms don't give clear information on fees, risks, custody,
  or their own trading. A project's white paper is a promise, not a guarantee.
- **Conflicts of interest.** Platforms may act as exchange, broker, market maker, lender and
  proprietary trader at once, hide related trading firms in other countries, give large clients
  special treatment, steer users to their own stablecoin, and allow insider trading on new
  listings.
- **Price manipulation.** Because wallets are pseudonymous, it is hard to tell real demand from
  bots. Reported volumes and prices can be inflated by **wash trading** (trading with yourself)
  and **self-trading**; bots can **front-run** visible orders. Volume on small coins and small
  exchanges is the least trustworthy.
- **Problematic terms.** Staking lock-ups, delayed withdrawals, no redemption rights on some
  stablecoins, automatic DeFi liquidations, arbitration clauses and class-action bans.
- **Unsuitable products.** No adviser checks whether a product suits you; marketing stresses
  profits and hides losses.

## Operational risks (Treasury)

- No way to reverse a mistaken or fraudulent transaction.
- Lost or stolen private keys mean lost funds.
- Software bugs, rushed upgrades, smart-contract exploits, cross-chain bridge hacks.
- Security vs. scalability trade-offs: congestion causes unpredictable fees and delays.
- Concentrated mining or staking power, 51% attacks and MEV (see [01](01-blockchain-and-bitcoin.md)).

## Intermediary and custody risks (Treasury)

- Volatile collateral leads to margin calls and forced liquidations that cascade.
- Thin capital and liquidity buffers at crypto firms.
- No complete legal regime for crypto custody. If a custodian goes bankrupt, customers may be
  unsecured creditors and wait months or years. Commingled funds and sub-custodians add risk.
- Crypto accounts are **not** deposit-insured, whatever the marketing suggests.

## Regulatory risk

Rules differ by country and are still changing. In the US the SEC, CFTC, banking agencies, CFPB,
DOJ and states each cover parts, and no one fully oversees spot markets in non-security crypto.
Exchanges block some regions (Binance.com blocks US users). New rules, enforcement actions or
delistings can move prices sharply and can cut off access to an exchange.

## How the agent responds

| Risk | What the agent does |
|---|---|
| Market | Every trade has a stop at its invalidation point; 0.5% of equity at risk per trade |
| Leverage and liquidation | Spot only, no margin, no shorting in the pilot |
| Liquidity and slippage | Skip markets whose spread or book depth is too thin (`strategies.filters`); backtests charge fees and slippage |
| Wash trading and fake volume | Volume rules run only on major pairs on Binance and HTX; the default symbol list is BTC/USDT and ETH/USDT |
| Exchange failure | Keep only trading capital on each exchange; trade-only API keys with no withdrawal right and an IP whitelist |
| Stop-limit gaps | Exits are stop-market orders, which always fill (the backtest also fills gaps at the open) |
| Scheduled news | The calendar filter blocks entries around high-impact events |
| Operational | Live trading stays locked behind three settings; paper trading first |
