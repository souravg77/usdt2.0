# Blockchain and Bitcoin: how it works

Sources: Chiu & Koeppl (2018), US Treasury (2022), Deloitte "Future of Exchanging Value".

## The problem a blockchain solves

Digital money is just data, and data can be copied. Without a bank keeping the books, the same
coin could be paid to two people. This is the **double-spending problem**. Bitcoin's answer is
a public ledger that everyone can check, kept up to date by competing record keepers (miners)
who are paid to be honest and would have to spend a lot to cheat.

## The building blocks

- **Ledger.** A list of who owns what. In Bitcoin every full node keeps a full copy of it.
- **Keys and addresses.** Ownership means holding the private key that can sign a spend. The
  public key (hashed into an address) lets anyone check that signature. Lose the private key and
  the coins are gone for good; leak it and anyone can take them. There is no reset.
- **Transactions.** A signed message moving value from one address to others. A small fee pays
  the miner who includes it.
- **Blocks.** Miners group pending transactions into a block. Each block contains the hash (a
  fixed-length fingerprint) of the previous block, so the blocks form a chain. Changing an old
  transaction changes its block's hash and breaks every block after it.
- **Distributed ledger technology (DLT).** The general idea of a ledger shared and agreed between
  many parties. A blockchain is one kind. Ledgers can be *permissionless* (anyone can join and
  validate, like Bitcoin and Ethereum) or *permissioned* (only approved parties validate).

## Agreeing on the next block: consensus

- **Proof of work (PoW, Bitcoin).** Miners race to find a number that makes the block's hash
  meet a difficulty target. That takes a huge amount of computing (hash rate) and electricity.
  The winner adds the block and earns the **block reward** (newly created coins) plus the fees.
  Bitcoin targets roughly one block every 10 minutes and has a fixed maximum supply, so new
  issuance shrinks over time and fees must eventually pay for security.
- **Proof of stake (PoS, Ethereum today).** Validators lock up ("stake") coins. The chance of
  proposing the next block depends on the stake, and cheating can cost the stake. Much less
  energy, but power concentrates in whoever holds the most tokens, which can invite collusion
  or vote-buying (Treasury).
- **Longest chain rule.** Nodes follow the chain with the most accumulated work. If two miners
  find blocks at the same time the chain briefly **forks**; one branch wins and the other's
  blocks are orphaned, so a transaction in an orphaned block can drop out.

## Confirmations and finality

A payment is "confirmed" once it is in a block, and each later block adds another confirmation.
Chiu & Koeppl show why sellers wait:

- To double spend, an attacker pays you, then secretly mines a competing chain without that
  payment and publishes it once it is longer. Each confirmation you wait for means the attacker
  must win one more block race in a row.
- So **finality is probabilistic**, not absolute. It improves with more confirmations, more
  honest mining, and smaller payment sizes. Large payments need longer waits.
- This creates a real trade-off between **speed, safety and cost**: more mining makes attacks
  costlier but burns resources; longer confirmation lags make payments safer but slower.
- Their estimate: Bitcoin's design at the time cost society about **1.4% of consumption**. A
  better design (less mining, rewards from coin issuance instead of fees, sensible confirmation
  lags) could cut that to about **0.08%**, comparable to cash with low inflation. Cryptocurrency
  could compete with retail payments only if it scales.

Practical meaning for the agent: an exchange deposit or withdrawal is not final until the
exchange's required confirmations pass, and network congestion delays it. The agent trades only
on exchange balances, never moves coins on-chain, and treats deposits as unavailable until the
exchange credits them.

## Attacks and weak points

- **51% attack.** Whoever controls more than half the hash rate (or stake) can rewrite recent
  blocks and double spend. Mining is concentrated in a few large pools, which makes this a real
  concern for smaller coins (Treasury).
- **Miner / maximal extractable value (MEV).** Block producers choose and order transactions and
  can profit by placing their own trades ahead of others, which looks like front-running.
- **Software risk.** Blockchains are open-source software: bugs happen, upgrades need agreement,
  and smart contracts are public and hard to patch, so an exploited bug can be drained before
  anyone fixes it. Cross-chain bridges are a frequent target.
- **Scaling.** More nodes means more security but slower, costlier transactions. Fees swing with
  congestion (Ethereum's average daily fee hit about $197 in May 2022), which makes small
  transactions uneconomic.
- **Privacy.** Addresses are pseudonymous, not anonymous. Chain analysis can link an address to
  a person, and then all its history is visible.

## Coin and protocol are tied together

Deloitte's point: the coin pays the miners who keep the ledger running, so Bitcoin the currency
and bitcoin the network can't really be separated. A blockchain without its coin needs some
other way to pay its record keepers (Ripple's XRP plays a smaller, anti-spam role instead).
