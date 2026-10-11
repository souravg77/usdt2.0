"""User accounts and their exchange keys (``data/accounts.db``).

- Passwords are stored as salted scrypt hashes, never in plain text.
- Exchange keys are encrypted at rest (Fernet, AES-128 + HMAC) with a key derived from
  ``AITRADER_KEYS_SECRET`` in ``.env``. Without that secret no key can be stored or read, and
  a copy of the database alone reveals nothing.
- New accounts start ``pending``: the admin approves them before they can connect keys. Every
  account trades on the testnet until the admin allows live trading for it and the user turns it
  on with their own pilot budget, and even then only while the platform's live lock is open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

DEFAULT_PATH = "data/accounts.db"
MIN_PASSWORD = 12
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
STATUSES = ("pending", "active", "disabled")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    live_allowed INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'testnet',
    pilot_max_usdt REAL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS api_keys (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exchange TEXT NOT NULL,
    testnet INTEGER NOT NULL,
    key_enc BLOB NOT NULL,
    secret_enc BLOB NOT NULL,
    key_hint TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT '{}',
    added REAL NOT NULL,
    PRIMARY KEY (user_id, exchange, testnet)
);
CREATE TABLE IF NOT EXISTS audit (
    ts REAL NOT NULL,
    user_id INTEGER,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id, ts);
"""


class AccountError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _algo, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=32,
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # same work for unknown emails


@dataclass
class User:
    id: int
    email: str
    status: str
    live_allowed: bool
    mode: str
    pilot_max_usdt: float | None
    created: float
    last_login: float | None

    @property
    def subject(self) -> str:
        """Session subject for this user (the admin's subject is a bare name, never with ':')."""
        return f"u:{self.id}"


class Vault:
    """Encrypts exchange keys with a key derived from ``AITRADER_KEYS_SECRET``."""

    def __init__(self, secret: str):
        from cryptography.fernet import Fernet

        if len(secret or "") < 32:
            raise AccountError("set AITRADER_KEYS_SECRET in .env (at least 32 characters, e.g. "
                               "`openssl rand -hex 32`) before users can connect exchange keys")
        key = base64.urlsafe_b64encode(hashlib.sha256(b"aitrader-keys|" + secret.encode()).digest())
        self._f = Fernet(key)

    def seal(self, text: str) -> bytes:
        return self._f.encrypt(text.encode())

    def open(self, blob: bytes) -> str:
        return self._f.decrypt(blob).decode()


class UserStore:
    def __init__(self, path: str = DEFAULT_PATH):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    # ---- users ----------------------------------------------------------------------------------

    @staticmethod
    def _user(row) -> User | None:
        if row is None:
            return None
        return User(id=row[0], email=row[1], status=row[2], live_allowed=bool(row[3]), mode=row[4],
                    pilot_max_usdt=row[5], created=row[6], last_login=row[7])

    _COLS = "id, email, status, live_allowed, mode, pilot_max_usdt, created, last_login"

    def create(self, email: str, password: str, status: str = "pending") -> User:
        email = normalize_email(email)
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"use a password of at least {MIN_PASSWORD} characters")
        if password.lower() == email:
            raise AccountError("the password can't be your email address")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO users (email, pw_hash, created, status) VALUES (?,?,?,?)",
                                        (email, hash_password(password), time.time(), status))
        except sqlite3.IntegrityError:
            raise AccountError("an account with this email already exists") from None
        return self.get(cur.lastrowid)

    def authenticate(self, email: str, password: str) -> User | None:
        try:
            email = normalize_email(email)
        except AccountError:
            check_password(password, _DUMMY_HASH)
            return None
        row = self.conn.execute("SELECT id, pw_hash FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            check_password(password, _DUMMY_HASH)
            return None
        if not check_password(password, row[1]):
            return None
        user = self.get(row[0])
        if user.status == "disabled":
            return None
        with self.conn:
            self.conn.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user.id))
        return user

    def get(self, user_id: int) -> User | None:
        return self._user(self.conn.execute(f"SELECT {self._COLS} FROM users WHERE id=?",
                                            (int(user_id),)).fetchone())

    def by_email(self, email: str) -> User | None:
        return self._user(self.conn.execute(f"SELECT {self._COLS} FROM users WHERE email=?",
                                            (normalize_email(email),)).fetchone())

    def from_subject(self, subject: str | None) -> User | None:
        if not subject or not subject.startswith("u:"):
            return None
        try:
            return self.get(int(subject[2:]))
        except ValueError:
            return None

    def list(self) -> list[User]:
        return [self._user(r) for r in self.conn.execute(f"SELECT {self._COLS} FROM users ORDER BY id")]

    def set_status(self, user_id: int, status: str) -> None:
        if status not in STATUSES:
            raise AccountError(f"status must be one of {STATUSES}")
        with self.conn:
            self.conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
            if status != "active":
                self.conn.execute("UPDATE users SET mode='testnet' WHERE id=?", (user_id,))

    def allow_live(self, user_id: int, allowed: bool) -> None:
        """Admin switch. Turning it off also sends the user back to the testnet."""
        with self.conn:
            self.conn.execute("UPDATE users SET live_allowed=? WHERE id=?", (int(allowed), user_id))
            if not allowed:
                self.conn.execute("UPDATE users SET mode='testnet' WHERE id=?", (user_id,))

    def set_mode(self, user_id: int, mode: str, pilot_max_usdt: float | None = None) -> None:
        """The user's own choice. Live needs the admin's permission, verified live keys and a budget."""
        user = self.get(user_id)
        if mode not in ("testnet", "live"):
            raise AccountError("mode must be testnet or live")
        if mode == "live":
            if user.status != "active" or not user.live_allowed:
                raise AccountError("the admin hasn't allowed live trading for this account")
            if not self.key_info(user_id, "binance", testnet=False):
                raise AccountError("connect and verify your real-account API key first")
            if not pilot_max_usdt or pilot_max_usdt <= 0:
                raise AccountError("set the most USDT the agent may use (your pilot budget)")
        with self.conn:
            self.conn.execute("UPDATE users SET mode=?, pilot_max_usdt=COALESCE(?, pilot_max_usdt) WHERE id=?",
                              (mode, pilot_max_usdt, user_id))

    def set_password(self, user_id: int, password: str) -> None:
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"use a password of at least {MIN_PASSWORD} characters")
        with self.conn:
            self.conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), user_id))

    # ---- exchange keys --------------------------------------------------------------------------

    def save_keys(self, vault: Vault, user_id: int, exchange: str, testnet: bool, api_key: str,
                  secret: str, permissions: dict) -> None:
        api_key, secret = api_key.strip(), secret.strip()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO api_keys VALUES (?,?,?,?,?,?,?,?)",
                (user_id, exchange, int(testnet), vault.seal(api_key), vault.seal(secret),
                 "…" + api_key[-4:], json.dumps(permissions), time.time()))

    def load_keys(self, vault: Vault, user_id: int, exchange: str, testnet: bool) -> tuple[str, str] | None:
        row = self.conn.execute("SELECT key_enc, secret_enc FROM api_keys WHERE user_id=? AND exchange=? "
                                "AND testnet=?", (user_id, exchange, int(testnet))).fetchone()
        return (vault.open(row[0]), vault.open(row[1])) if row else None

    def key_info(self, user_id: int, exchange: str | None = None, testnet: bool | None = None) -> list[dict]:
        """What the UI may show: no key material, only the last 4 characters."""
        rows = self.conn.execute("SELECT exchange, testnet, key_hint, permissions, added FROM api_keys "
                                 "WHERE user_id=?", (user_id,)).fetchall()
        out = [{"exchange": r[0], "testnet": bool(r[1]), "hint": r[2], "permissions": json.loads(r[3]),
                "added": r[4]} for r in rows]
        return [k for k in out if (exchange is None or k["exchange"] == exchange)
                and (testnet is None or k["testnet"] == testnet)]

    def delete_keys(self, user_id: int, exchange: str, testnet: bool) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM api_keys WHERE user_id=? AND exchange=? AND testnet=?",
                              (user_id, exchange, int(testnet)))
            if not testnet:
                self.conn.execute("UPDATE users SET mode='testnet' WHERE id=?", (user_id,))

    # ---- audit log ------------------------------------------------------------------------------

    def log(self, user_id: int | None, actor: str, action: str, detail: str = "") -> None:
        """Record who did what to an account (live switch, approvals, keys, kill switch)."""
        with self.conn:
            self.conn.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                              (time.time(), user_id, actor, action, detail))

    def audit(self, user_id: int | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT a.ts, a.user_id, u.email, a.actor, a.action, a.detail FROM audit a " \
            "LEFT JOIN users u ON u.id = a.user_id"
        args: list = []
        if user_id is not None:
            q += " WHERE a.user_id=?"
            args.append(user_id)
        rows = self.conn.execute(q + " ORDER BY a.ts DESC LIMIT ?", args + [limit]).fetchall()
        return [{"ts": r[0], "user_id": r[1], "email": r[2], "actor": r[3], "action": r[4], "detail": r[5]}
                for r in rows]

    def backup(self, path: str) -> None:
        dest = sqlite3.connect(path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        self.conn.close()


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AccountError("enter a valid email address")
    return email
