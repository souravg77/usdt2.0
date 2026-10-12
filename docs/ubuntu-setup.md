# Ubuntu VPS setup (Docker Compose)

> **New install? Use [install-ubuntu.md](install-ubuntu.md)** instead: one script does all of
> this plus HTTPS, the firewall and backups. This page is the manual reference.

Tested target: the latest Ubuntu LTS server. We use **Docker Compose** rather than systemd units
because it pins the Python version, restarts the service automatically, and keeps every later
service (dashboard, database, alerts) in one file.

## 0. Before you start

- **Location matters.** Bybit restricts some countries (for example the USA, the UK,
  Canada/Ontario and Singapore) and blocks API calls from restricted regions. Check Bybit's terms
  for your own country and the VPS's country, and pick a VPS region Bybit serves.
- Phase 1 only needs public market data, so **no API keys are required yet**. When you do add keys:
  Bybit Demo Trading keys first; later a real key with spot trading only; **never give it Wallet
  permissions** (withdraw or transfer); bind it to the VPS IP. Use a dedicated sub-account. Keep
  only the money the bot trades with on Bybit (an exchange can fail or freeze withdrawals).

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
git clone https://github.com/souravg77/usdt2.0.git AI_crypto_trader
cd AI_crypto_trader
cp .env.example .env
chmod 600 .env
nano .env            # leave AITRADER_MODE=paper and AITRADER_LIVE_UNLOCKED=false
mkdir -p data
echo "AITRADER_UID=$(id -u)" >> .env   # the containers run with your uid, so both can write data/
```

## 4. Build and check

```bash
docker compose build
docker compose run --rm collector status
docker compose run --rm collector paper-smoke --exchange bybit --symbol BTC/USDT --usdt 100
docker compose run --rm collector stream --exchange bybit --seconds 20
```

`paper-smoke` is the Phase 1 exit check: it opens and closes a test position on the paper
exchange using live prices and prints the fills, fees and final equity.

## 5. Download history and start the collector

```bash
# one-off backfill (adjust dates). The collector backfills 30 days by itself; this goes further.
docker compose run --rm collector download --since 2023-01-01 --exchange bybit
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

One command brings the server to the latest code on GitHub (the `main` branch, which only
changes when a pull request is merged):

```bash
cd ~/AI_crypto_trader
./deploy/update.sh --check    # is there anything new? (changes nothing)
./deploy/update.sh            # update
```

What it does, in order:

1. Refuses to run if files tracked by git were edited on the server. Your `.env` and `data/`
   are not tracked and are never touched.
2. Backs up the database, the strategy registry, the paper state and the model registry to
   `data/backups/<time>/` (the last 5 are kept). It also keeps the current Docker image as
   `aitrader:rollback`.
3. Pulls the new code, rebuilds the image and migrates the database (`aitrader migrate`).
4. Restarts only the services that were running before.
5. Health check after 30 seconds: every service must be running (not crash-looping),
   `aitrader status` must work, live trading must still be locked if it was locked before, and
   the dashboard must answer if it was running.
6. If any step fails, it **rolls back**: previous code, previous image, database restored from
   the backup, and the services restarted. It exits with code 1 and says what failed.

An update can't unlock live trading. Unlocking only happens through `.env`, which the script
never edits, and the health check rolls back any update after which live trading isn't locked
anymore.

To update automatically, add a weekly cron job (`crontab -e` as your user; Sundays at 04:00 UTC):

```
0 4 * * 0 cd ~/AI_crypto_trader && ./deploy/update.sh >> data/update.log 2>&1
```

Check `data/update.log` afterwards, or run `./deploy/update.sh --check` by hand. If you'd
rather approve each update, leave the cron job out and run the script yourself after a pull
request is merged.

## 10. Testnet, Telegram and the go/no-go gate (Phases 5 and 6)

Start the market-context service once (later updates keep it running):
`docker compose up -d context`. Then, step by step, as described in
[live-trading.md](live-trading.md):

1. Bybit Demo Trading keys (`BYBIT_DEMO_API_KEY`, `BYBIT_DEMO_API_SECRET`) in `.env` or on the
   Settings page, then `docker compose --profile testnet up -d executor-testnet`.
2. Test the kill switch on demo trading: `docker compose run --rm collector trade kill --confirm`,
   then `trade resume`.
3. Telegram: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, then
   `docker compose --profile telegram up -d telegram`.
4. Add the weekly report to cron (Mondays 06:00 UTC):
   `0 6 * * 1 cd ~/AI_crypto_trader && docker compose run --rm collector report weekly >/dev/null 2>&1`
5. After about two weeks and 100+ paper trades: `docker compose run --rm collector golive check`.
6. Only if it passes and you decide to go live: `golive attest`, `golive approve`, the pilot
   budget and the live lock in `.env`, then `docker compose --profile live up -d executor-live`.

## 11. User accounts (optional)

To let other people sign up and connect their own exchange keys, follow
[user-accounts.md](user-accounts.md): set `AITRADER_KEYS_SECRET` and `AITRADER_WEB_SIGNUP`,
then `docker compose --profile users up -d users`. Check the legal side first.

## Useful commands

| Task | Command |
|---|---|
| Status / live-gate check | `docker compose run --rm collector status` |
| Trading status, positions, today | `docker compose run --rm collector trade status` |
| Pause / resume / kill switch | `docker compose run --rm collector trade pause` · `trade resume` · `trade kill --confirm` |
| Go/no-go gate | `docker compose run --rm collector golive check` |
| Search the knowledge base | `docker compose run --rm collector kb search "stop-market vs stop-limit"` |
| Trade filter models and the active one | `docker compose run --rm collector ai list` |
| Turn the trade filter off | `docker compose run --rm collector ai off` |
| Stop everything | `docker compose down` |
| Back up data | `cp data/aitrader.db ~/aitrader-$(date +%F).db` (or `sqlite3 data/aitrader.db ".backup ..."`) |
