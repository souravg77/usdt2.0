# Install guide: from a fresh Ubuntu server to a running platform

This guide takes a new Ubuntu server, on your own network or rented, to a running trading
platform with the dashboard on HTTPS. It ends with paper trading running. Live trading stays
locked the whole way through.

It assumes **a local server with no domain** (a mini PC or spare computer at home or the office).
Section 12 explains how to add a domain later.

Takes about 30 minutes, plus a few hours for the first data download.

---

## 1. What you need

| | Minimum | Better |
|---|---|---|
| CPU / RAM | 2 cores, 4 GB | 4 cores, 8 GB |
| Disk | 40 GB SSD | 100 GB SSD |
| OS | Ubuntu Server 22.04 or 24.04 LTS, 64-bit | 24.04 LTS |
| Network | wired connection, always on | a UPS for power cuts |

You also need:

- **A fixed LAN IP for the server.** In your router, under DHCP reservation (or static lease),
  give it a fixed address such as `192.168.1.50`. The dashboard address and the Binance key's
  IP whitelist depend on it.
- **Your public IP should not change** when you use real Binance keys later. Binance locks each
  key to one IP. Many home connections get a new public IP now and then; ask your provider for a
  static IP, or be ready to update the key's IP whitelist. Testnet and paper trading don't care.
- A computer on the same network to open the dashboard.

## 2. Install Ubuntu Server

1. Download Ubuntu Server 24.04 LTS from ubuntu.com and write it to a USB stick (balenaEtcher
   or Rufus).
2. Boot the server from it and follow the installer:
   - **Network:** use the wired connection.
   - **Profile:** your name, a server name (e.g. `trader`), a user name (e.g. `shawn`) and a
     strong password.
   - **SSH:** tick **Install OpenSSH server**.
   - Skip the snaps.
3. Reboot, log in, and note the IP with `ip -4 addr` (it should match the reservation from
   step 1).

From now on you can work from your own computer: `ssh shawn@192.168.1.50`.

**Recommended: log in with an SSH key instead of a password.**

On your own computer:

```bash
ssh-keygen -t ed25519            # press Enter for the defaults
ssh-copy-id shawn@192.168.1.50
```

Then, on the server, turn off password logins:

```bash
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

Check that `ssh shawn@192.168.1.50` still works from a second terminal before you close the
first one.

## 3. Run the installer

**First, merge the pull requests on GitHub**, in order (#1 up to the newest). The installer
installs the `main` branch, and until they are merged `main` doesn't have this installer or the
features in this guide. To try it before merging, add `--branch phase-6b-hosting` to the
`bash ... install.sh` lines below.

On the server, as your normal user (not root):

```bash
curl -fsSL https://raw.githubusercontent.com/souravg77/usdt2.0/main/deploy/install.sh -o install.sh
bash install.sh
```

The repository is private, so that download needs a login. If it fails, copy `deploy/install.sh`
from GitHub to the server instead, or clone first:

```bash
git clone https://github.com/souravg77/usdt2.0.git ~/AI_crypto_trader
bash ~/AI_crypto_trader/deploy/install.sh
```

For a private repository, `git clone` asks for your GitHub user name and a personal access
token (GitHub, Settings, Developer settings, Fine-grained tokens, read-only access to this
repository), not your password.

What the installer does:

1. Updates Ubuntu and turns on automatic security updates and **fail2ban**, which blocks
   addresses that keep guessing SSH passwords.
2. Turns on the **firewall** (ufw): only SSH and the dashboard's HTTPS port are open. On a
   computer at home, only your own network can reach it, because your router doesn't let the
   internet in. On a **rented server** (a VPS with a public IP), the dashboard is reachable from
   the internet, protected by its password and the login lockout.
3. Installs **Docker** from Docker's own repository.
4. Downloads the code into `~/AI_crypto_trader`.
5. Creates `.env` (readable only by you) with strong random secrets: the dashboard password,
   the session secret and the key-encryption secret. **The dashboard password is printed once:
   save it in a password manager.**
6. Builds the app, prepares the database, and starts the core services (`collector`,
   `context`, `paper`, `web`) and **Caddy**, which serves the dashboard over HTTPS on
   `https://<server-ip>`.
7. Runs the health check.

Log out and back in once, so you can run `docker` without `sudo`.

Running the installer again is safe. It keeps your settings and passwords.

## 4. First login

1. On a computer on the same network, open `https://192.168.1.50` (your server's IP).
2. The browser warns that the connection is not private. That is expected: Caddy made its own
   certificate because there is no domain. Click **Advanced**, then **Proceed**, or better,
   trust the certificate once (next section).
3. Log in as `admin` with the password the installer printed.

### Trust the certificate (removes the warning)

On the server:

```bash
cd ~/AI_crypto_trader
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Copy it to your computer (`scp shawn@192.168.1.50:AI_crypto_trader/caddy-root.crt .`) and
install it:

- **Windows:** double-click the file, choose **Install Certificate**, then **Local Machine**,
  then **Place all certificates in the following store**, and pick **Trusted Root Certification
  Authorities**.
- **macOS:** double-click to open it in Keychain Access, then set it to **Always Trust**.
- **Android / iPhone:** send it to the phone and install it under Settings, Security (Android)
  or Settings, General, Profiles (iPhone). On iPhone, also turn it on under Settings, General,
  About, Certificate Trust Settings.

Delete `caddy-root.crt` from the server afterwards: `rm ~/AI_crypto_trader/caddy-root.crt`.

## 5. Fill in the rest of the settings

**Easiest: the dashboard's Settings page.** Paste your Anthropic key, Binance testnet keys and
Telegram bot there. Each key is checked before it is saved, stored encrypted, and the services
that use it restart by themselves. The same page shows the health check and has a **Start
setup** button that does step 6 for you.

Or edit `.env` on the server (a key saved on the Settings page wins over `.env`):

```bash
cd ~/AI_crypto_trader
nano .env        # save with Ctrl+O, Enter; exit with Ctrl+X
```

| Setting | When | What |
|---|---|---|
| `ANTHROPIC_API_KEY` | to let the agent read books | from console.anthropic.com |
| `BINANCE_TESTNET_API_KEY` / `_SECRET` | Phase 5 testing | see the "connect Binance" guide, stage 1 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | alerts on your phone | docs/live-trading.md, step 2 |
| `AITRADER_SYMBOLS` | which coins | default: the 20 most traded coins against USDT (see `TOP_PAIRS` in src/aitrader/config.py) |

**Never** put real Binance keys in `.env` until the go/no-go gate passes (Phase 6). Leave
`AITRADER_LIVE_UNLOCKED=false`.

After editing `.env`, restart so the services read it: `docker compose up -d`.

## 6. Load the market data and strategies

```bash
cd ~/AI_crypto_trader
# 1. history: candles since 2024 (takes a few hours; safe to stop and run again)
docker compose run --rm collector download --since 2024-01-01
# 2. the strategies already read from the books, backtested; survivors go to paper trading
docker compose run --rm collector learn add-library --test
# 3. the AI trade filter: train it, read the report, then decide
docker compose run --rm collector ai retrain
docker compose run --rm collector ai report
docker compose run --rm collector ai promote <version> --by Shawn   # only if you like the report
```

The `paper` service starts trading the passed strategies on its next tick. Watch it on the
dashboard's **Trading** page.

## 7. Health check

```bash
docker compose run --rm collector doctor
```

It checks:

- the settings and secrets;
- that live trading is locked;
- the database schema and how fresh the candles are;
- that each service is alive (its heartbeat);
- disk space and backups.

`FAIL` lines must be fixed; `WARN` lines say what to look at. Run it after any change, and
whenever something looks wrong.

Other useful views:

```bash
docker compose ps                        # which services run
docker compose logs -f --tail 100 paper  # one service's log (Ctrl+C to stop)
docker compose run --rm collector trade status
```

## 8. Backups

```bash
./deploy/backup.sh      # database, strategy and model registries, trade state, user accounts
```

Make it daily (`crontab -e`, then add):

```
30 3 * * * cd ~/AI_crypto_trader && ./deploy/backup.sh >> data/backup.log 2>&1
```

The newest 14 backups are kept under `data/backups/`. A backup on the same disk doesn't survive
a dead disk, so copy them off the machine too. Set `AITRADER_BACKUP_COPY_TO` in your crontab
line to another disk or to `user@other-pc:/backups`.

**Also keep a copy of `.env`** somewhere safe and separate, such as a password manager or an
encrypted USB stick. It holds `AITRADER_KEYS_SECRET`; without it, stored user keys can't be
read and every user must connect their keys again.

## 9. Updates

```bash
./deploy/update.sh --check   # anything new on GitHub? (changes nothing)
./deploy/update.sh           # update: backup, rebuild, migrate, restart, health check
```

It rolls back by itself if anything fails, and it can never unlock live trading. For automatic
weekly updates, add to `crontab -e`:

```
0 4 * * 0 cd ~/AI_crypto_trader && ./deploy/update.sh >> data/update.log 2>&1
```

Ubuntu's own security updates install automatically. Reboot now and then (`sudo reboot`); all
services come back by themselves.

## 10. Optional services

| What | How | Guide |
|---|---|---|
| Testnet executor (fake money, real orders) | `docker compose --profile testnet up -d executor-testnet` | docs/live-trading.md |
| Telegram alerts and commands | `docker compose --profile telegram up -d telegram` | docs/live-trading.md |
| User accounts and sign-up | set `AITRADER_WEB_SIGNUP=open`, then `docker compose --profile users up -d users` | docs/user-accounts.md |
| Live pilot (real money) | only after the go/no-go gate and your approval | docs/live-trading.md |

## 11. Reaching the dashboard from outside your network

On a computer at home, only devices on your local network reach the dashboard. To use it from your
phone on mobile data, **don't open ports on your router**. Use one of these instead:

- **Tailscale** (simplest, free for personal use). Install it on the server
  (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and on your phone,
  then open `https://<server's Tailscale IP>`. Allow that address in the firewall once:
  `sudo ufw allow in on tailscale0 to any port 443`, and re-run the installer with
  `--site https://<tailscale-ip>`.
- **An SSH tunnel** from a laptop: `ssh -L 8443:localhost:443 shawn@your-server`, then open
  `https://localhost:8443`.

## 12. Later: a domain

When you have a domain (e.g. `trader.example.com`), point its DNS **A record** at your public
IP. Forward ports 80 and 443 on your router to the server, then run:

```bash
bash ~/AI_crypto_trader/deploy/install.sh --site trader.example.com --email you@example.com
```

Caddy gets a free Let's Encrypt certificate (no browser warning), and the firewall opens 80 and
443 to the internet. Before exposing the dashboard to the internet:

- keep the admin password strong;
- keep `AITRADER_WEB_SIGNUP=closed` unless you mean to take sign-ups;
- read the legal note in docs/user-accounts.md.

## 13. Security checklist

- [ ] SSH works with a key, and password login is off (section 2)
- [ ] `sudo ufw status` shows only OpenSSH and 80/443 (local network only, unless you have a domain)
- [ ] `.env` is `-rw-------` (`ls -l .env`) and has a copy kept somewhere safe
- [ ] The dashboard password is in a password manager
- [ ] `doctor` shows no FAIL, and live trading is locked
- [ ] Daily backups run and are copied off the machine
- [ ] Binance keys (Phase 6 only): trade-only, withdrawals off, IP-whitelisted to your public IP
- [ ] The router has no port forwarding to the server (unless you set up a domain)

## 14. Troubleshooting

| Problem | Fix |
|---|---|
| The page doesn't load | `docker compose ps`: are `web` and `caddy` up? `sudo ufw status`: is 443 allowed from your network? Is the server IP still the same? |
| Login says wrong password | `grep AITRADER_WEB_PASSWORD .env` on the server; after five wrong tries, wait 15 minutes |
| Logged out after every restart | `AITRADER_WEB_SECRET` must be set (doctor warns) |
| `permission denied` writing `data/` | `sudo chown -R $(id -u):$(id -g) ~/AI_crypto_trader/data`, check `AITRADER_UID` in `.env` equals `id -u`, then `docker compose build && docker compose up -d` |
| `permission denied` running docker | log out and back in once after installing (or use `sudo docker ...`) |
| doctor: candles are old | `docker compose logs --tail 50 collector`; check the internet connection |
| doctor: a service stopped | `docker compose logs --tail 100 <service>`, then `docker compose up -d <service>` |
| Disk full | `docker system prune` removes old images; old backups are pruned automatically |
| An update failed | it rolled back by itself; `tail -50 data/update.log` says why |
| Binance refuses the key | wrong key type (testnet vs real), or your public IP changed: update the key's IP whitelist on Binance |

The detailed manual reference for every command is docs/ubuntu-setup.md.
