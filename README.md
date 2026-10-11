# AI_crypto_trader

AI-driven cryptocurrency trading agent for Binance and HTX, built phase by phase from the project roadmap.

**Safety defaults:** the agent runs in `paper` mode. Real-money orders are blocked in code unless
`AITRADER_MODE=live`, `AITRADER_LIVE_UNLOCKED=true` **and** `AITRADER_LIVE_CONFIRM` holds the exact
confirmation phrase. That gate should only be opened after the Phase 6 go/no-go criteria pass.
API keys are read from environment variables (a `.env` file), never from the repository.

## Modes

| Mode | Money at risk | What happens |
|---|---|---|
| backtest | none | rules run on stored historical candles (Phase 3) |
| paper | none | `PaperExchange` simulates fills, fees and slippage on live prices |
| testnet | none | real order flow on Binance Spot testnet (HTX has no usable testnet, so it stays on paper) |
| live | real | locked by default |

## Roadmap progress

- [x] **Phase 1: data and exchange adapters**: one `ExchangeAdapter` interface, Binance and HTX adapters (ccxt), `PaperExchange`, SQLite candle store, resumable downloader, websocket feed with reconnect and stale-price heartbeat, request throttling, data-quality checks
- [~] **Phase 2: strategies and filters**: technical indicators (incl. StochRSI, VWAP, Parabolic SAR, Ichimoku, Fibonacci), 4 book strategies as explicit rules ([docs/strategies.md](docs/strategies.md)), liquidity/spread check, signal log, economic-calendar no-trade filter. Still to do: funding/open-interest context and crypto event feeds
- [x] **Knowledge base**: notes on blockchain, Bitcoin, money, the crypto ecosystem, risks and trading basics from the supplied books ([docs/knowledge](docs/knowledge/README.md)), plus `aitrader kb build` / `aitrader kb search` over your own copies of the PDFs
- [x] **Learning from books**: `aitrader learn` has Claude read a book and write its strategies as checked rules, backtests them, paper-trades the ones that pass and retires the ones that stop working ([docs/learning.md](docs/learning.md)). 41 strategies from six strategy books plus Carli's CCI divergence are ready to test. Never trades real money
- [x] **Phase 3: backtest lab**: next-open fills, fees, slippage, funding, stop-first ambiguity, R metrics, in/out/final splits, walk-forward, parameter sensitivity, results calendar ([docs/backtesting.md](docs/backtesting.md))
- [ ] Phase 4: AI signal filter and model registry
- [ ] Phase 5: paper/testnet loop, dashboard, Telegram alerts
- [ ] Phase 6: go/no-go gate and live pilot

## Layout

```
src/aitrader/
  config.py          settings from environment variables
  safety.py          live-trading lock
  models.py          Candle, OrderBook, Order, Position, Balance
  throttle.py        token-bucket rate limiter
  exchanges/
    base.py          ExchangeAdapter interface
    ccxt_adapter.py  BinanceAdapter, HtxAdapter
    paper.py         PaperExchange simulator
  indicators/        EMA/SMA, RSI, MACD, ATR, Bollinger, ADX, structure, S/R, volume profile
  strategies/        rule cards + signal generators for the ebook strategies
  events/            economic-calendar no-trade windows
  backtest/          engine, metrics, walk-forward, sensitivity
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
aitrader paper-smoke --exchange binance --symbol BTC/USDT --usdt 100
aitrader download --since 2024-01-01 --exchange binance --symbol BTC/USDT --timeframe 1h
aitrader check-data
aitrader strategies
aitrader signals --strategy trend_pullback --symbol BTC/USDT --timeframe 1h
aitrader calendar
aitrader backtest --strategy trend_pullback --symbol BTC/USDT --timeframe 1h
aitrader stream --exchange binance --symbol BTC/USDT --seconds 20
```

## Deploying on an Ubuntu VPS

We deploy with **Docker Compose**. See [docs/ubuntu-setup.md](docs/ubuntu-setup.md).
