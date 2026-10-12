"""API keys entered on the dashboard's Settings page.

They are stored encrypted (with ``AITRADER_KEYS_SECRET``) in ``data/settings/keys.enc`` and put
into the environment when any ``aitrader`` command starts, so every service sees them just like
values from ``.env``. A value saved on the website wins over the same name in ``.env``.

Only the names in ``FIELDS`` can be stored. The live-trading lock (AITRADER_MODE,
AITRADER_LIVE_UNLOCKED, AITRADER_LIVE_CONFIRM) can never be set here: it stays in ``.env`` on
the server.

Services that use these keys (Telegram) restart themselves when the file
changes, so a saved key takes effect within a minute.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_PATH = "data/settings/keys.enc"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    group: str
    secret: bool = True


FIELDS = [
    Field("ANTHROPIC_API_KEY", "Anthropic API key", "anthropic"),
    Field("ANTHROPIC_WORKSPACE_ID", "Workspace ID (only if Anthropic asks for one)", "anthropic", secret=False),
    Field("BYBIT_API_KEY", "Real-account API key", "bybit"),
    Field("BYBIT_API_SECRET", "Real-account API secret", "bybit"),
    Field("TELEGRAM_BOT_TOKEN", "Bot token", "telegram"),
    Field("TELEGRAM_CHAT_ID", "Chat ID", "telegram", secret=False),
    Field("X_BEARER_TOKEN", "Bearer token", "x"),
    Field("GITHUB_TOKEN", "Fine-grained token (Contents + Pull requests, this repository only)", "github"),
    Field("GITHUB_REPO", "Repository (owner/name)", "github", secret=False),
]
NAMES = {f.name for f in FIELDS}
FORBIDDEN = {"AITRADER_MODE", "AITRADER_LIVE_UNLOCKED", "AITRADER_LIVE_CONFIRM"}
assert not NAMES & FORBIDDEN


class SecretStore:
    def __init__(self, secret: str, path: str = DEFAULT_PATH):
        from ..accounts.store import Vault

        self.path = path
        self._vault = Vault(secret)  # raises AccountError when the secret is missing or short

    def load(self) -> dict[str, str]:
        try:
            with open(self.path, "rb") as fh:
                blob = fh.read()
        except FileNotFoundError:
            return {}
        data = json.loads(self._vault.open(blob))
        return {k: v for k, v in data.items() if k in NAMES and v}

    def save(self, values: dict[str, str]) -> None:
        """Set (non-empty value) or remove (empty value) the given names."""
        bad = set(values) - NAMES
        if bad:
            raise ValueError(f"can't store {', '.join(sorted(bad))} here")
        data = self.load()
        for k, v in values.items():
            v = (v or "").strip()
            if v:
                data[k] = v
            else:
                data.pop(k, None)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(self._vault.seal(json.dumps(data)))
        os.replace(tmp, self.path)


def apply_to_env(path: str = DEFAULT_PATH, environ=None) -> list[str]:
    """Put stored keys into the environment; returns the names applied. Never raises: a missing
    or unreadable store just means the values from .env are used."""
    environ = os.environ if environ is None else environ
    secret = environ.get("AITRADER_KEYS_SECRET", "")
    if not os.path.exists(path) or len(secret) < 32:
        return []
    try:
        values = SecretStore(secret, path).load()
    except Exception as exc:  # wrong secret or a damaged file: keep going on .env alone
        log.warning("couldn't read the keys saved on the dashboard (%s); using .env only", type(exc).__name__)
        return []
    for k, v in values.items():
        environ[k] = v
    return sorted(values)


def restart_on_change(path: str = DEFAULT_PATH, interval: float = 20.0) -> threading.Thread:
    """Stop this process (Docker starts it again with the new keys) when the saved keys change."""

    def stamp():
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    start = stamp()

    def loop():
        while True:
            time.sleep(interval)
            if stamp() != start:
                log.warning("API keys were changed on the dashboard; restarting to use them")
                os.kill(os.getpid(), signal.SIGTERM)
                return

    t = threading.Thread(target=loop, daemon=True, name="keys-watch")
    t.start()
    return t
