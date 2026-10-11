# Ubuntu VPS setup (Docker Compose)

Tested target: the latest Ubuntu LTS server. We use **Docker Compose** rather than systemd units
because it pins the Python version, restarts the service automatically, and keeps every later
service (dashboard, database, alerts) in one file.

## 0. Before you start

- **Location matters.** Binance blocks API access from some countries (for example the US returns
  HTTP 451). Pick a VPS region where both Binance and HTX allow API access, and confirm they are
  legally available to you.
- Phase 1 only needs public market data, so **no API keys are required yet**. When you do add keys:
  read-only first; later trade-only; **never enable withdrawals**; whitelist the VPS IP on both exchanges.

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

## 6. Updating

```bash
git pull
docker compose build && docker compose up -d
```

## Useful commands

| Task | Command |
|---|---|
| Status / live-gate check | `docker compose run --rm collector status` |
| Stop everything | `docker compose down` |
| Back up data | `cp data/aitrader.db ~/aitrader-$(date +%F).db` (or `sqlite3 data/aitrader.db ".backup ..."`) |
