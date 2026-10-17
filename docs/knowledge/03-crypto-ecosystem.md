# The crypto-asset ecosystem

Sources: US Treasury (2022), Binance Academy trading eBook.

## Kinds of crypto-assets

- **Coins** are native to their own blockchain (BTC on Bitcoin, ETH on Ethereum) and usually pay
  the network's fees.
- **Tokens** are issued by smart contracts on an existing chain. They can be utility tokens,
  governance tokens (votes in a DAO) or claims on other assets.
- **Fungible** tokens are interchangeable, one unit is as good as another (coins, stablecoins).
- **Stablecoins** aim to hold a steady value against a currency such as the US dollar, backed by
  reserves or by algorithms. Retail users often have **no right to redeem** them with the
  issuer and depend on selling them on an exchange. USDT, the agent's quote currency, is one.
- **NFTs** are non-fungible tokens that claim to represent a specific item. The legal rights they
  give are unclear.
- **DAOs** run a protocol by token-holder votes; in practice voting power is very concentrated
  (one study: under 1% of holders had 90% of votes).

## Wallets, keys and custody

- A **wallet** stores the keys that control crypto. *Hosted* (custodial) wallets are run by an
  exchange or custodian who controls the keys; *unhosted* (self-custody) wallets are controlled by
  the user, often through a seed phrase.
- "Not your keys, not your coins": coins left on an exchange are a claim on that exchange. If it
  fails, customers may be treated as unsecured creditors and wait months or years, or lose funds.
- Wallet software can be flawed (one wallet stored seed phrases in plain text).

## Who does what

- **Centralized exchanges (CEXs)** such as Binance and HTX match buyers and sellers in an order
  book, hold customer funds, and often also lend, stake, make markets and trade for themselves,
  which creates conflicts of interest.
- **Decentralized exchanges (DEXs)** and other **DeFi** protocols run on smart contracts. Many
  use automated market makers instead of order books. In 2022 DeFi held about $86 billion of
  "total value locked", DEXs being the biggest category, then lending (about $17 billion).
  "Decentralized" is often only partly true: developers, governance token holders and oracles
  still have control.
- **Lending and staking.** Platforms pay "yield" for lending out or staking coins. Loans are
  over-collateralized (often borrowing up to about 50% of collateral value) and liquidated
  automatically when collateral falls too far. Staking can lock funds for a period.
- **Miners and validators** confirm transactions and earn rewards and fees.
- **Data aggregators** publish prices and volumes (for example CoinMarketCap). Reported volumes
  can include wash trades, so they are not always reliable.
- **Traditional finance** increasingly connects through custody, payments, funds and loans.

## Spot and derivatives

- **Spot** markets settle immediately: you buy BTC and receive it. The agent trades spot only.
- **Derivatives** (futures, perpetual swaps, options) derive their value from an underlying asset
  and usually allow leverage and shorting. They are off for the pilot.
- **Margin** means trading with borrowed money: 5x leverage gives 5x the gains and 5x the losses,
  and the liquidation price sits closer to the entry the higher the leverage.
