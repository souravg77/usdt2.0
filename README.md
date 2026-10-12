# AI_crypto_trader

AI-driven cryptocurrency trading agent for Bybit: spot longs, and shorts on USDT perpetual futures
(isolated margin, leverage 1x by default, at most 3x; see [docs/futures.md](docs/futures.md)), built
phase by phase from the project roadmap.

**Safety defaults:** the agent runs in `paper` mode. Real-money orders are blocked in code unless
`AITRADER_MODE=live`, `AITRADER_LIVE_UNLOCKED=true` **and** `AITRADER_LIVE_CONFIRM` holds the exact
confirmation phrase. That gate should only be opened after the Phase 6 go/no-go criteria pass.
API keys are read from environment variables (a `.env` file), never from the repository.

## Modes

| Mode | Money at risk | What happens |
|---|---|---|
| backtest | none | rules run on stored historical candles (Phase 3) |
| paper | none | `PaperExchange` simulates fills, fees and slippage on live prices |
| live | real | locked by default |

Market data (candles, tickers) comes from Bybit's public API and needs no key. Fees are modelled
at 0.1% taker and maker (Bybit non-VIP spot). Check that Bybit serves your country and the
server's country: it restricts some (for example the USA, the UK, Canada/Ontario and Singapore)
and blocks API calls from them.

**Switched from Binance.** Positions recorded on Binance before the switch are no longer managed:
paper trading drops them, and the executors move them to `other_exchange` in their state file and
log a warning. Old Binance candles stay in the database unused. The collector backfills 30 days of
Bybit candles, and **Turn paper trading on** (Trading page) downloads a year.

## Roadmap progress

- [x] **Phase 1: data and exchange adapters**: one `ExchangeAdapter` interface, a Bybit spot adapter (API V5 via ccxt), `PaperExchange`, SQLite candle store, resumable downloader, websocket feed with reconnect and stale-price heartbeat, request throttling, data-quality checks
- [~] **Phase 2: strategies and filters**: technical indicators (incl. StochRSI, VWAP, Parabolic SAR, Ichimoku, Fibonacci), 4 book strategies as explicit rules ([docs/strategies.md](docs/strategies.md)), liquidity/spread check, signal log, economic-calendar no-trade filter, market-context filter (funding, open interest, total market cap; [docs/market-context.md](docs/market-context.md)). Still to do: crypto event feeds
- [x] **Knowledge base**: notes on blockchain, Bitcoin, money, the crypto ecosystem, risks and trading basics from the supplied books ([docs/knowledge](docs/knowledge/README.md)), plus `aitrader kb build` / `aitrader kb search` over your own copies of the PDFs
- [x] **Learning from books**: `aitrader learn` has Claude read a book and write its strategies as checked rules, backtests them, paper-trades the ones that pass and retires the ones that stop working ([docs/learning.md](docs/learning.md)). 54 strategies from eight strategy books plus Carli's CCI divergence are ready to test. Never trades real money
- [x] **Phase 3: backtest lab**: next-open fills, fees, slippage, funding, stop-first ambiguity, R metrics, in/out/final splits, walk-forward, parameter sensitivity, results calendar ([docs/backtesting.md](docs/backtesting.md))
- [x] **Phase 4: AI trade filter and model registry**: gradient boosting predicts whether each signal reaches 1R before its stop, skips weak signals and scales size by confidence within the risk cap. Chronological train/validation/test, used only if it beats the unfiltered rules out of sample, versioned registry with human promotion and instant rollback, monthly `ai retrain` ([docs/ai-filter.md](docs/ai-filter.md)). Needs real backtest data on the VPS to train; funding/open-interest features and the news LLM layer wait on their data feeds
- [x] **Backtest teacher**: learns from the backtests which market situations (trend, volatility, volume) each strategy wins and loses in, and where a 1R profit-take catches more trades. Checked on signals it never saw; paper trading follows it only after you press Turn on ([docs/teacher.md](docs/teacher.md))
- [x] **Phase 5: paper loop, dashboard, Telegram alerts**: login-protected dashboard where you submit an e-book for the agent to read, turn into strategies and test, with a Trading page (positions, equity curve, trade log with AI probability, event feed, pause/resume/kill switch). Live executor that mirrors paper trades with idempotent orders, partial-fill and restart handling, and slippage measured against paper. Risk engine (0.5% per trade, hard cap 1%, daily/weekly loss caps, exposure limits). Telegram alerts, daily summary and `/status /pause /resume /kill`. Weekly paper-vs-backtest report ([docs/live-trading.md](docs/live-trading.md), [docs/dashboard.md](docs/dashboard.md))
- [x] **User accounts**: sign-up with email and password (admin approves each account), each user connects their own Bybit key (encrypted at rest; real-account keys with withdrawal, transfer, margin, derivatives or options rights are refused), and a `users` service trades for each account on its own balance once live is on for it (Bybit demo trading was removed in Oct 2026) ([docs/user-accounts.md](docs/user-accounts.md)). Check the legal side before inviting other people
- [x] **Phase 6: go/no-go gate and live pilot**: `golive check` scores every roadmap criterion (100+ paper trades, positive expectancy in paper and out-of-sample backtest, drawdown, paper-vs-backtest gap, 14 days stable, key and kill-switch checks). Live orders need the gate, your `golive approve` for 1-2 strategies (tied to their rules and the AI model), a pilot budget and the live lock you open in `.env`. Live results are monitored against paper and trading pauses when they drift ([docs/live-trading.md](docs/live-trading.md)). The pilot itself starts only when you decide

## Layout

```
src/aitrader/
  config.py          settings from environment variables
  safety.py          live-trading lock
  models.py          Candle, OrderBook, Order, Position, Balance
  throttle.py        token-bucket rate limiter
  exchanges/
    base.py          ExchangeAdapter interface
    ccxt_adapter.py  BybitAdapter
    paper.py         PaperExchange simulator
  indicators/        EMA/SMA, RSI, MACD, ATR, Bollinger, ADX, structure, S/R, volume profile
  strategies/        rule cards + signal generators for the ebook strategies
  events/            economic-calendar no-trade windows
  backtest/          engine, metrics, walk-forward, sensitivity
  ai/                Phase 4 trade filter: features, training set, model, registry
  trade/             Phases 5-6: risk engine, pause/kill, executor, go-live gate, reports
  ops/               event log, heartbeats, Telegram service
  accounts/          user accounts, encrypted exchange keys, per-user trading
  web/               dashboard: login, book uploads, background jobs, overview
  data/
    store.py         SQLite candle store
    downloader.py    paginated, resumable history download
    quality.py       gap / duplicate / alignment / bad-bar checks
    feeds.py         live websocket prices with reconnect + heartbeat
  cli.py             `aitrader` command
```

## Quick start (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest -q
aitrader status
aitrader paper-smoke --exchange bybit --symbol BTC/USDT --usdt 100
aitrader download --since 2024-01-01 --exchange bybit --symbol BTC/USDT --timeframe 1h
aitrader check-data
aitrader strategies
aitrader signals --strategy trend_pullback --symbol BTC/USDT --timeframe 1h
aitrader calendar
aitrader backtest --strategy trend_pullback --symbol BTC/USDT --timeframe 1h
aitrader ai retrain && aitrader ai list
aitrader stream --exchange bybit --symbol BTC/USDT --seconds 20
aitrader stream --bybit --symbol BTC/USDT --seconds 20   # Bybit V5 spot WebSocket: bid/ask/last
aitrader research update && aitrader research show       # trader-research log (research only)
```

## Deploying on an Ubuntu server

**Start here: [docs/install-ubuntu.md](docs/install-ubuntu.md).** `deploy/install.sh` sets up a
fresh Ubuntu server in one go: firewall, fail2ban, Docker, `.env` with random secrets, the
services, and HTTPS through Caddy (on the server's LAN IP, or a domain with Let's Encrypt).

- Health check: `docker compose run --rm collector doctor`
- Backups: `./deploy/backup.sh` (daily from cron; see the guide)

Updates: `./deploy/update.sh` pulls the latest `main`, rebuilds, migrates the database, restarts
the services and rolls back on its own if the health check fails.

Manual reference for every step: [docs/ubuntu-setup.md](docs/ubuntu-setup.md).
