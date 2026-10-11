# Market context (Phase 2, step 3)

Funding, open interest and the state of the whole crypto market. These are **filters only**: they
can veto a new entry or shrink its size, but they never create one.

## What is collected

| metric | source | refresh | stored as |
|---|---|---|---|
| `funding_rate` | Binance USD-M perpetual, ccxt `fetch_funding_rate_history` | 8h | fraction per 8h (0.0001 = 0.01%) |
| `open_interest` | Binance USD-M, ccxt `fetch_open_interest_history` (1h; Binance keeps 30 days) | 1h | coins |
| `long_short_ratio` | Binance global long/short account ratio (skipped if ccxt does not expose it) | 1h | ratio |
| `btc_dominance` | CoinGecko `/api/v3/global` | 1h | percent |
| `total_mcap` | CoinGecko `/api/v3/global` | 1h | USD |
| `total_mcap_change_24h` | CoinGecko `/api/v3/global` | 1h | fraction (-0.08 = -8%) |
| `stable_mcap` | CoinGecko `/api/v3/coins/markets?ids=tether,usd-coin` | 1h | USD (USDT + USDC) |

Spot pairs map to the perpetual (`BTC/USDT` -> `BTC/USDT:USDT`), but rows are stored under the
spot symbol and `exchange="binance"`. Global metrics use `exchange="global", symbol="*"`. All of it
lives in the `context` table of the candle database (schema migration v2). Neither source needs
an API key.

```
aitrader context collect --once           # one top-up (backfills 30 days the first time)
aitrader context collect --interval 900   # service: top up every 15 minutes
aitrader context show --symbol BTC/USDT   # latest values and the filter's decision now
```

## The filter rules

`strategies/context_filter.py`, `ContextFilter(store).decide(exchange, symbol, now_ms)` returns
`allow_entries`, `size_multiplier` and `reasons`:

- funding above **+0.05% per 8h**: size x0.5; above **+0.10%**: no new longs (the agent only buys
  spot, so no new entries).
- open interest up more than **15% in 24h while price fell**: size x0.5.
- total market cap down more than **8% in 24h**: no new entries.
- size cuts from several rules multiply (x0.5 and x0.5 give x0.25).
- a series older than **twice its refresh interval** is ignored and its rule skipped. Missing or
  stale data never blocks a trade.

HTX markets use the Binance perpetual's funding and open interest (they track the same coin).

### Why these are conservative

The thresholds sit well outside normal conditions (typical BTC funding is +0.01% per 8h), so the
filter acts only on clear extremes: crowded longs, leverage piling into a falling market, or a
market-wide sell-off. It only ever reduces risk. It is not a signal and has not been tuned on the
backtests, which would overfit it. Missing data is ignored rather than treated as danger, because a
flaky free API should not stop the agent trading; the economic-calendar filter already cuts size
when its own feed fails.

## AI features

`data.context.attach_context(df, store, exchange, symbol)` adds `funding_rate` and
`open_interest` columns to a candle frame. Each bar sees only the newest value stamped at or
before its close (`ts + timeframe`), and only while that value is fresh. `ai dataset` / `ai retrain`
call it automatically once the context table has rows, so the Phase 4 model's `funding_rate` and
`oi_change` features stop being empty after the next retrain.

## Not covered

On-chain exchange flows, active addresses, liquidation data and news sentiment need paid or keyed
sources (Glassnode, CryptoQuant, Coinglass, news APIs), so they are left out for now. Open-interest
history only goes back 30 days on Binance, so older backtests have no open interest.
