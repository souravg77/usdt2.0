# Dashboard

A small web app for the agent, behind a login (the Phase 5 dashboard). It cannot open a trade,
approve going live, unlock live trading, or move a strategy to a new status by hand. The only
order it can cause is the kill switch selling what the executors bought.

## What it does

- **Submit a book.** Upload a PDF, Markdown or text file (up to 50 MB). `kb build` makes it
  searchable in the knowledge base straight away (the job page shows live output). The book then
  waits in the "Books to read" list until night (`AITRADER_BOOK_HOURS`, default 00:00 to 06:00),
  when the `books` service has the local model read it (`learn read --test`): each strategy becomes
  checked rules and is backtested; the ones that pass move to paper trading.
- **Overview.** Shows the mode and whether live orders are locked. It lists every strategy with
  its status, out-of-sample backtest result, paper result and last status change; click one to
  see its rules, sources and history. It also shows the AI trade filter (active model and
  candidates waiting for approval), paper-trading totals, and recent jobs.

- **Trading.** Pause, resume and the kill switch (type `KILL` to confirm). Your real Bybit balance
  (read only, once you save the key on Settings). Open positions from paper and live, with the AI filter's probability; the paper P&L curve and the
  executors' equity curves; the last 40 closed trades with their result in R, USDT and the paper
  twin's R; the event feed (trades, errors, disconnects, pauses); and the go/no-go gate with a
  "Check the gate now" button. The page refreshes every minute. See
  [live-trading.md](live-trading.md).

- **Change requests.** Turned off: Claude wrote them through the Anthropic API, which the platform
  no longer uses. The page still lists the requests made before. See
  [change-requests.md](change-requests.md).
- **Research.** The trader-research log: public reports from traders and researchers (Reddit,
  TradingView, blogs, exchange and quant research, papers) about the strategy families the
  platform runs. Each finding has its URL, source, the date found, the source's date when known,
  the family, the claim in one sentence, the evidence type (anecdote, backtest shown, live record
  shown, peer-reviewed, other) and a credibility note; filter by family and evidence type.
  New searches are turned off: they used Claude's web search through the Anthropic API, which
  the platform no longer uses, and the local model can't search the web. Findings are kept in
  `data/news/research.jsonl`, one per URL. **They are research inputs only:** nothing that trades
  reads them, so they never change trading, risk limits or a strategy's status. Votes,
  testimonials and win-rate screenshots are not evidence of profit; an idea from here still has
  to be written as a strategy and pass the backtest, out-of-sample and paper gates. Everything on
  the page comes from web pages, so it is escaped and only http(s) links are shown.
- **Pairs.** The Bybit spot pair ranking with the reasons for each rank, the pairs it left out,
  each pair's current market regime (1h, 4h, 1d), and paper and backtest results split by regime.
  "Rank pairs now" queues `aitrader pairs rank`. See [regime-and-pairs.md](regime-and-pairs.md).

- **Users** (admin) and **My account** (users): sign-up, approval, each user's exchange keys,
  positions, trades and kill switch. See [user-accounts.md](user-accounts.md).

Uploaded books are saved in `data/books/` under a generated name and are never committed.

## Controls page

**Controls** (`/controls`, next to Trading) puts the user controls in one place. Each one writes
a setting the engines already read, so the page and the Trading page always agree:

| Control | Where it is kept | What it does |
|---|---|---|
| Pause, resume, kill switch | the trading control file | same buttons as the Trading page |
| Risk level: Conservative, Balanced, Aggressive | paper guards and paper leverage | Conservative: 0.25% per trade, 2% day pause, 5% week close, 2% open risk, 1x. Balanced: 0.5%, 3%, 6%, 4%, 2x. Aggressive: 1%, 5%, 10%, 8%, 3x. The level reads "Custom" after any single change |
| Maximum leverage | paper leverage | paper perpetuals, 1x to 100x (live stays capped at 3x in code) |
| Maximum daily loss | each engine's day pause | opens nothing new for the pause hours once reached |
| Trading timeframes | each engine's timeframes | which candles new trades open on |
| Allowed trading pairs | `data/settings/controls.json` | unticked or listed pairs are never opened, from any list (fixed, ranked, top gainers, squeeze); open positions run to their exits; takes effect within a minute |
| AI automation | `data/settings/controls.json` | automatic strategy generation and/or evolution, candidates per run, how often, which timeframes, short twins; the `factory` service runs it and backtests what it made |

The page shows the mode and whether live orders are locked, but **cannot unlock live trading**.
That still needs `.env` and `golive approve` at a terminal. Automation never trades and never
changes risk settings. Set `AITRADER_CONTROLS_FILE` to keep the controls file elsewhere.

## Strategy Factory, Evolution Lab and Strategy Arena

Three pages for the strategies the agent makes itself ([factory.md](factory.md)). Every number
comes from the registry the backtests wrote; nothing is simulated for display, and none of these
pages can move a strategy to paper or live by hand.

- **Strategy Factory** (`/factory`): how many machine-made strategies and families exist, their
  statuses, and the out-of-sample t bar that sets. **Generate** (how many, which timeframes,
  short twins, backtest straight away) and **Evolve** (children, crossover share) each run as a
  background job: `learn factory` or `learn evolve`, then `learn test` and, with short twins,
  `learn test --market futures`. The newest 25 are listed with why each was rejected.
- **Evolution Lab** (`/evolution`): each generation's count, tested, paper, under review,
  rejected and best in-sample result; the survivors with how each was made (mutation or
  crossover, with links to the parents), its luck bar and stress test; a count of rejection
  reasons; and any strategy's family tree (`/evolution?id=<id>`).
- **Strategy Arena** (`/arena`): up to four strategies side by side (`/arena?ids=a,b,c`; by
  default the four best by out-of-sample result): status, out-of-sample trades, R per trade, t,
  win rate, profit factor, drawdown, walk-forward, Monte Carlo, holdout, luck bar, stress test,
  paper results and why rejected.

## Monitor page

**Monitor** (`/monitor`, refreshes every 30 s) has four panels, each also served as JSON for other
tools (signed in):

| Panel | JSON | What it shows |
|---|---|---|
| Feed health | `/api/monitor/feed` | the live executor's Bybit price stream (connected or not, ping round trip, reconnects, the connection log, pairs quiet for over 15 s) from `data/ops/feed.json`, which `trade run --mode live` writes every 5 s; and missing candles per pair and timeframe in the last 7 days of the collector's database |
| Derivatives | `/api/monitor/derivatives` | each pair's USDT perpetual: last, mark and index price, basis, funding per 8 h and per year, time to the next payment (Bybit public API, cached 5 s) |
| Signals and abstentions | `/api/monitor/signals` | agents A–G on the newest closed 1h candle of the first 6 pairs: the regime, the candidate signals with entry, stop, target and confidence, and why every other strategy abstained (bad data, regime, missing candles, no setup) |
| Risk and exposure | `/api/monitor/risk` | open live positions and whether each has its stop resting on Bybit, exposure, margin used by shorts, today's realised loss against the 1% daily cap, a one-day 95% historical value at risk of the open positions (last 90 days of hourly candles), and the emergency kill switch (type KILL) |

The feed panel says "not running" until the live executor runs, which only happens after you
open the live lock. Confidence is a description of how many views agree, not a chance of winning.

## Turning it on (VPS)

1. Add a login to `.env`:

   ```
   AITRADER_WEB_USER=admin
   AITRADER_WEB_PASSWORD=<at least 12 characters>
   AITRADER_WEB_SECRET=<output of: openssl rand -hex 32>
   ```

   The dashboard refuses to start without a password of at least 12 characters.
2. Start it with the other services: `docker compose up -d web`.
3. Open it. The `web` service is published on the server's **localhost only** (port 8080), so it
   is not reachable from the internet by default. Pick one of these:
   - **SSH tunnel (simplest):** on your own computer run
     `ssh -L 8080:localhost:8080 you@your-vps`, then open http://localhost:8080.
   - **HTTPS with your own domain:** put a reverse proxy in front, for example Caddy, which
     gets a certificate automatically. Then set `AITRADER_WEB_SECURE_COOKIE=true` and
     `docker compose up -d web`:

     ```
     # /etc/caddy/Caddyfile
     trader.example.com {
         reverse_proxy 127.0.0.1:8080
     }
     ```

     Don't publish port 8080 directly on `0.0.0.0` without HTTPS: the password would cross the
     internet in plain text.

## Security

- One account, set in `.env`. The password is compared in constant time.
- After 5 wrong passwords, logins are refused for 15 minutes.
- Session cookies are signed, HttpOnly, SameSite=Strict, and expire after 12 hours. Logging out
  cancels the session at once.
- Every form carries a CSRF token tied to the session.
- Pages send a strict Content-Security-Policy and refuse to be framed. Everything shown is
  HTML-escaped, including text that came from books.
- Uploads are checked by content (PDF magic bytes or valid UTF-8 text) and size. They are saved
  under a generated name, so a crafted file name can't write outside `data/books`.
- Jobs run as separate `aitrader` processes with fixed arguments (no shell), so a bad book
  can't take the dashboard down.
- Book text is data, not instructions: the agent's output is only accepted as whitelisted JSON
  rules (see [learning.md](learning.md)).

## Running it locally

```bash
export AITRADER_WEB_PASSWORD='a long local password'
aitrader web            # http://127.0.0.1:8080
```
