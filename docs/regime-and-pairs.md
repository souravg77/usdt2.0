# Market regimes and pair ranking

Two helpers describe the market. Neither places orders. Code: `src/aitrader/market/`.

## Market regime (agent G)

Each closed candle gets one label. The classifier checks them in this order and uses the first
that fits:

| Regime | When |
|---|---|
| `low-liquidity` | less than 1M USDT traded per day on average (close x volume) |
| `high-volatility` | ATR as a share of price is above the 90th percentile of the last 500 bars |
| `trend-up` | ADX(14) at least 20, +DI above -DI, price above a rising EMA200 |
| `trend-down` | ADX(14) at least 20, -DI above +DI, price below a falling EMA200 |
| `range` | ADX(14) under 20: no clear trend |
| `uncertain` | under about 210 bars of history, or a trend reading whose signals disagree |

Each label comes with a confidence (0-1) and a short reason, for example
`ADX 31, price above rising EMA200`. Only closed candles and rolling indicators are used, so a
bar's label does not change when later bars arrive. A test checks this.

Where the label is used:

- **Paper trading** saves the regime on every signal (`regime`: label, confidence, reason) and
  on every closed trade. Each strategy's paper results include `by_regime` (shown by
  `aitrader learn show <id>`).
- **Backtest gate** (`learn test`) adds `by_regime_oos`, the out-of-sample trades split by regime.
  The gate itself ignores it.
- **Multi-year report** (`learn report`) adds `by_regime` for each strategy and for all of them
  pooled. The terminal table ends with the pooled line.
- **Pairs page** on the dashboard shows each pair's current regime on 1h, 4h and 1d, plus paper
  and backtest results by regime.

By default the regime never blocks a trade. A rule strategy can opt out of some regimes with
`"avoid_regimes": ["trend-down", "low-liquidity"]` in its spec. Entries on bars with those labels
are then skipped in backtests and in paper trading. Strategies without the key keep their ids
and behave exactly as before.

Look up current regimes in the terminal with `aitrader pairs regimes` (add `--symbol` or
`--timeframe` to narrow it). It reads the candles the collector stored.

## Pair ranking (agent I)

`aitrader pairs rank` reads Bybit's spot markets and 24h tickers from public data (no key). It
saves `data/pairs/ranking.json` with a timestamp, each pair's numbers and the reasons for its
rank. `aitrader pairs show` prints the saved ranking. The dashboard's **Pairs** page shows the
ranking and has a "Rank pairs now" button.

Pairs that are dropped (the saved file lists each one with the reason):

- anything that is not a spot pair quoted in USDT, and inactive markets
- stablecoins (USDC, FDUSD, DAI, ...) against USDT
- leveraged tokens such as `BTC3L` and `ETH3S`
- pairs listed less than 30 days ago, when Bybit gives a listing date
- 24h turnover under 2M USDT (`--min-turnover`), spread over 30 bps, or under 25k USDT of
  order-book depth within 1% of the mid price. Depth is checked for the best 40 candidates
  (`--depth-top`).

The remaining pairs get a score out of 100:

| Part | Points | Best when |
|---|---|---|
| 24h turnover | 35 | about 1B USDT or more |
| spread | 20 | close to zero |
| depth within 1% of mid | 20 | about 10M USDT or more |
| 24h high-low range | 10 | 2-12% of price. Faster markets lose points. |
| platform results | 15 | out-of-sample trades of strategies that passed the backtest gate, plus closed paper trades on the pair. Under 30 trades counts as neutral. |

A fast-moving price never raises the score above the band, so a pair can't rank high just because
it is moving a lot. The score is a liquidity and cost filter. It does not forecast returns.

### Using the ranking

Set this in `.env`:

```
AITRADER_PAIRS=ranked          # default: fixed (AITRADER_SYMBOLS, or the TOP_PAIRS list)
AITRADER_PAIRS_TOP=20          # how many ranked pairs to use
AITRADER_PAIRS_MAX_AGE_HOURS=48
```

When the setting is on, `download`, the `collect` service and the `learn paper` service use the
top ranked pairs. The paper service reads the list again on every tick. If the ranking file is
missing, empty or older than the maximum age, they fall back to the fixed list and log a warning.
The installer adds a daily cron job (02:45) that runs `aitrader pairs rank`, so the ranking stays
fresh whether or not the setting is on. Pairs with
an open paper position keep getting candles from the collector after they leave the ranking, so
those positions can still close. `--symbol` on the command line still overrides everything.

The ranking comes from Bybit. Candles are still downloaded from the exchanges the commands use
(`--exchange`). A ranked pair that one of those exchanges does not list fails to download, and the
download command says so.

### Survivorship bias

The ranking lists the pairs that are liquid **today**. If you backtest those pairs over the last 5
years, the test leaves out every coin that was delisted, collapsed or faded during that time.
Those are exactly the coins a strategy would have held and lost money on. This makes backtest
results look better than real trading would have been. Treat multi-year results on ranked pairs
as an upper bound. The fixed list has the same problem to a smaller degree, because it was also
picked from today's most-traded coins.

## Futures AI pairs: Perpetual (hot pairs) or Futures (dated, top gainers)

The Futures view's top gainers card has a **Contract** switch, like Bybit's own Perpetual / Futures
choice:

| Contract | What Futures AI trades | Leverage | Real money |
|---|---|---|---|
| Perpetual (default) | the hot crypto perpetuals: the most traded over 24h, up or down, TradFi perps (stocks, metals) left out. Bybit's own Hot tab is hand-picked and not in its public API, so this is the nearest public measure | each coin's own maximum (up to 100x on BTC; paper only above 3x) | yes, behind the usual live gate, 3x max |
| Futures (dated) | the top gainers among the dated USDT futures (BTCUSDT-30OCT26 and so on) | each contract's own maximum, read from Bybit | never: paper only |

Switching scans that contract type afresh; the other type's list is not traded in the meantime.
Dated contracts are few (mostly BTC, ETH and SOL) and trade less, so they need only 0.1M USDT of
24h turnover. Contracts that expire within 48 hours are left out, and a paper trade on one closes an
hour before Bybit settles it (08:00 UTC on the expiry day). A weekly contract has only a few weeks of
candles, so strategies on 4h and 1d may not have enough history to read it.

```
docker compose run --rm -T collector pairs gainers --market futures --contract dated
```
