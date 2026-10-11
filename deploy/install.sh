#!/usr/bin/env bash
# One-command install on a fresh Ubuntu 22.04 / 24.04 server (docs/install-ubuntu.md).
#
#   curl -fsSL https://raw.githubusercontent.com/souravg77/usdt2.0/main/deploy/install.sh -o install.sh
#   bash install.sh                                 # local server: HTTPS on its LAN IP
#   bash install.sh --site trader.example.com --email you@example.com   # with a domain
#
# Run it as your normal user (with sudo rights), not as root. It:
#   1. updates Ubuntu and turns on automatic security updates, fail2ban and the firewall (ufw)
#   2. installs Docker from Docker's own repository
#   3. downloads the code into ~/AI_crypto_trader
#   4. creates .env with strong random secrets (the dashboard password is printed once)
#   5. builds the image, prepares the database, starts collector, context, paper, web and caddy
#   6. runs `aitrader doctor`
# Live trading stays locked. Running it again is safe: existing settings are kept.
set -euo pipefail

REPO="${AITRADER_REPO:-https://github.com/souravg77/usdt2.0.git}"
BRANCH="main"
DIR="$HOME/AI_crypto_trader"
SITE=""
SITE_GIVEN=0
EMAIL=""
FIREWALL=1

while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE="$2"; SITE_GIVEN=1; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --no-firewall) FIREWALL=0; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user (it uses sudo when needed), not as root." >&2; exit 1; }
. /etc/os-release
[ "${ID:-}" = ubuntu ] || echo "warning: this script is written for Ubuntu; you have ${PRETTY_NAME:-unknown}" >&2

LAN_IF="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
LAN_IP="$(ip -4 -o addr show dev "$LAN_IF" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
LAN_NET="$(ip -4 route show dev "$LAN_IF" scope link 2>/dev/null | awk '{print $1; exit}')"
if [ -z "$SITE" ]; then
  [ -n "$LAN_IP" ] || { echo "Couldn't find this server's LAN IP; pass --site <ip>" >&2; exit 1; }
  SITE="https://$LAN_IP"
fi
if [ -n "$EMAIL" ]; then TLS="$EMAIL"; BIND="0.0.0.0"; else TLS="internal"; BIND="${LAN_IP:-0.0.0.0}"; fi

say "Updating Ubuntu and installing base packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git ufw fail2ban \
  unattended-upgrades openssl rsync
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
sudo systemctl enable --now fail2ban
sudo timedatectl set-ntp true || true  # trading needs a correct clock

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
if ! sudo docker compose version >/dev/null 2>&1; then  # Ubuntu's docker.io comes without it
  say "Installing docker compose"
  sudo apt-get install -y docker-compose-v2 || sudo apt-get install -y docker-compose-plugin
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
DOCKER="sudo docker"  # the new group only applies after logging in again

if [ "$FIREWALL" = 1 ]; then
  say "Firewall: SSH, plus HTTPS from ${LAN_NET:-anywhere}${EMAIL:+ and the internet}"
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow OpenSSH
  if [ -n "$EMAIL" ] || [ -z "$LAN_NET" ]; then
    sudo ufw allow 80/tcp; sudo ufw allow 443/tcp; sudo ufw allow 443/udp
  else
    sudo ufw allow from "$LAN_NET" to any port 80 proto tcp
    sudo ufw allow from "$LAN_NET" to any port 443
  fi
  sudo ufw --force enable
fi

say "Getting the code into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --quiet origin "$BRANCH"
  git -C "$DIR" merge --quiet --ff-only "origin/$BRANCH" || echo "local changes: left the code as it is"
else
  git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"
mkdir -p data
# the containers run with your uid (AITRADER_UID below), so ./data must be yours
[ -w data ] && [ -z "$(find data -not -user "$(id -u)" -print -quit 2>/dev/null)" ] || sudo chown -R "$(id -u):$(id -g)" data

setkey() {  # setkey NAME VALUE: set NAME in .env if it is empty or missing
  local name="$1" value="$2"
  if grep -qE "^${name}=.+" .env; then return; fi
  if grep -qE "^${name}=" .env; then
    sed -i "s|^${name}=.*|${name}=${value}|" .env
  else
    printf '%s=%s\n' "$name" "$value" >> .env
  fi
}

say "Settings (.env)"
NEW_PW=""
[ -f .env ] || cp .env.example .env
chmod 600 .env
if ! grep -qE '^AITRADER_WEB_PASSWORD=.{12,}' .env; then
  NEW_PW="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  setkey AITRADER_WEB_PASSWORD "$NEW_PW"
fi
setkey AITRADER_WEB_SECRET "$(openssl rand -hex 32)"
setkey AITRADER_KEYS_SECRET "$(openssl rand -hex 32)"
if [ "$SITE_GIVEN" = 1 ] || ! grep -qE '^AITRADER_SITE=.+' .env; then
  for kv in "AITRADER_SITE=$SITE" "AITRADER_TLS=$TLS" "AITRADER_HTTPS_BIND=$BIND"; do
    name="${kv%%=*}"; sed -i "/^${name}=/d" .env; echo "$kv" >> .env
  done
fi
SITE="$(grep -E '^AITRADER_SITE=' .env | cut -d= -f2-)"
TLS="$(grep -E '^AITRADER_TLS=' .env | cut -d= -f2-)"
for kv in AITRADER_WEB_SECURE_COOKIE=true AITRADER_WEB_TRUST_PROXY=true "AITRADER_UID=$(id -u)"; do
  name="${kv%%=*}"; sed -i "/^${name}=/d" .env; echo "$kv" >> .env
done
grep -q '^AITRADER_LIVE_UNLOCKED=true' .env && echo "NOTE: live trading is unlocked in .env" || true

say "Building and starting (the first build takes a few minutes)"
$DOCKER compose build
$DOCKER compose run --rm -T collector migrate
$DOCKER compose up -d collector context paper web
$DOCKER compose --profile https up -d caddy

say "Health check"
sleep 10
$DOCKER compose run --rm -T collector doctor || true

cat <<EOF

Done. Open the dashboard: ${SITE%/}
  user:     $(grep -E '^AITRADER_WEB_USER=' .env | cut -d= -f2- || echo admin)
$( [ -n "$NEW_PW" ] && echo "  password: $NEW_PW    <- save it now in a password manager; it is shown only once" || echo "  password: the one already in .env (AITRADER_WEB_PASSWORD)" )
$( [ "$TLS" = internal ] && echo "Your browser will warn about the certificate the first time: see 'Trust the certificate' in docs/install-ubuntu.md." )
Live trading is locked. Next steps: docs/install-ubuntu.md, from "First login".
Log out and back in once so you can run docker without sudo.
EOF
