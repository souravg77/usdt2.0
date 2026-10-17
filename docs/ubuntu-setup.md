# Ubuntu VPS setup (Docker Compose)

Tested target: the latest Ubuntu LTS server. We use **Docker Compose** rather than systemd units
because it pins the Python version, restarts the service automatically, and keeps every later
service (dashboard, database, alerts) in one file.

## 0. Before you start

- **Location matters.** Binance blocks API access from some countries (for example the US returns
  HTTP 451). Pick a VPS region where both Binance and HTX allow API access, and confirm they are
  legally available to you.
- Phase 1 only needs public market data, so **no API keys are required yet**. When you do add keys:
  read-only first; later trade-only; **never enable withdrawals**; whitelist the VPS IP on both exchanges. Keep only the money the bot trades with on each exchange (an exchange can fail or freeze withdrawals).

## 1. Harden the server (once)

```bash
sudo apt update && sudo apt -y upgrade
sudo adduser trader && sudo usermod -aG sudo trader
# copy your SSH key to the new user, then log in as trader
sudo ufw allow OpenSSH && sudo ufw enable
sudo apt -y install unattended-upgrades fail2ban git
sudo timedatectl set-timezone UTC     # all agent timestamps are UTC
timedatectl status                    # "System clock synchronized: yes"
```

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
docker compose version
```

## 3. Get the code and configure

```bash
git clone https://github.com/souravg77/AI_crypto_trader.git
cd AI_crypto_trader
cp .env.example .env
chmod 600 .env
nano .env            # leave AITRADER_MODE=paper and AITRADER_LIVE_UNLOCKED=false
mkdir -p data && sudo chown 1000:1000 data   # container runs as uid 1000
```

## 4. Build and check

```bash
docker compose build
docker compose run --rm collector status
docker compose run --rm collector paper-smoke --exchange binance --symbol BTC/USDT --usdt 100
docker compose run --rm collector paper-smoke --exchange htx --symbol BTC/USDT --usdt 100
docker compose run --rm collector stream --exchange binance --seconds 20
```

`paper-smoke` is the Phase 1 exit check: it opens and closes a test position on the paper
exchange using live prices and prints the fills, fees and final equity.

## 5. Download history and start the collector

```bash
# one-off backfill (adjust dates; HTX only serves its most recent ~2000 bars per timeframe)
docker compose run --rm collector download --since 2023-01-01 --exchange binance
docker compose run --rm collector download --since 2023-01-01 --exchange htx
docker compose run --rm collector check-data

# keep candles up to date in the background (restarts on reboot)
docker compose up -d
docker compose logs -f collector
```

## 6. Load the books into the knowledge base

The notes in `docs/knowledge` are built into the image. To make the full books searchable too,
copy your PDFs to the server (they stay on the server and are never committed):

```bash
mkdir -p data/books
scp *.pdf you@your-vps:~/AI_crypto_trader/data/books/   # run this on your own computer
docker compose run --rm collector kb build
docker compose run --rm collector kb search "how does proof of work stop double spending"
```

Re-run `kb build` whenever you add a book. Identical files are only indexed once.

To have the agent turn a book into strategies and test them (needs `ANTHROPIC_API_KEY` in `.env`,
see [learning.md](learning.md)):

```bash
docker compose run --rm collector learn read data/books/your-book.pdf --test
docker compose run --rm collector learn list
docker compose up -d        # starts the paper service next to the collector
docker compose logs -f paper
```

## 7. Train the AI trade filter (Phase 4)

Once strategies have passed `learn test` and there is at least a year of history, one command
builds the training set and trains and tests a model. It never switches the model on by itself:

```bash
docker compose run --rm collector ai retrain       # prints the side-by-side report
docker compose run --rm collector ai list
docker compose run --rm -it collector ai promote m20261101-030000   # you type the version to approve
docker compose restart paper                        # optional: it also picks the model up on its own
docker compose run --rm collector ai rollback      # back to the previous model at once
```

Retrain monthly with cron (`crontab -e` as your user). Each run adds a new version that waits for
your approval; the active model is never replaced automatically:

```
0 3 1 * * cd ~/AI_crypto_trader && docker compose run --rm collector ai retrain >> data/ai/retrain.log 2>&1
```

See [ai-filter.md](ai-filter.md) for what the model learns and how it is tested.

## 8. Open the dashboard

Set `AITRADER_WEB_PASSWORD` (12+ characters) and `AITRADER_WEB_SECRET` in `.env`, then run
`docker compose up -d web`. It listens on the server's localhost only. From your own computer:

```bash
ssh -L 8080:localhost:8080 you@your-vps     # then open http://localhost:8080
```

There you can upload a book for the agent to read, turn into strategies and test. See
[dashboard.md](dashboard.md) for HTTPS with your own domain.

## 9. Updating

```bash
git pull
docker compose build && docker compose up -d
```

## Useful commands

| Task | Command |
|---|---|
| Status / live-gate check | `docker compose run --rm collector status` |
| Search the knowledge base | `docker compose run --rm collector kb search "stop-market vs stop-limit"` |
| Trade filter models and the active one | `docker compose run --rm collector ai list` |
| Turn the trade filter off | `docker compose run --rm collector ai off` |
| Stop everything | `docker compose down` |
| Back up data | `cp data/aitrader.db ~/aitrader-$(date +%F).db` (or `sqlite3 data/aitrader.db ".backup ..."`) |
