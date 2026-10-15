"""Login for the dashboard: the admin account from the environment plus user accounts
(``aitrader.accounts``), signed session cookies, CSRF tokens on every form, and a lockout after
repeated wrong passwords. A session's subject is the admin's name or ``u:<user id>``.

The dashboard runs on a public VPS, so it refuses to start without a strong password
(``AITRADER_WEB_PASSWORD``, at least 12 characters).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque

MIN_PASSWORD = 12
COOKIE = "aitrader_session"


class AuthError(RuntimeError):
    pass


class Auth:
    def __init__(self, user: str, password: str, secret: str = "", session_hours: float = 12,
                 max_failures: int = 5, lockout_s: float = 900, revoked_path: str = ""):
        if len(password or "") < MIN_PASSWORD:
            raise AuthError(f"set AITRADER_WEB_PASSWORD in .env to at least {MIN_PASSWORD} "
                            "characters before starting the dashboard")
        self.user = user or "admin"
        if ":" in self.user or "@" in self.user or "|" in self.user:
            raise AuthError("AITRADER_WEB_USER must be a plain name (no @, : or |)")
        self._pw = hashlib.sha256(password.encode()).digest()
        # without a fixed secret, sessions end when the service restarts (log in again)
        self._key = (secret or secrets.token_hex(32)).encode()
        self.session_s = session_hours * 3600
        self.max_failures, self.lockout_s = max_failures, lockout_s
        self._fails: dict[str, deque] = defaultdict(deque)
        self._revoked: set[str] = set()
        # logged-out sessions, kept on disk so a restart doesn't bring them back: hash -> expiry
        self._revoked_path = revoked_path
        self._revoked_disk: dict[str, int] = {}
        if revoked_path:
            try:
                with open(revoked_path, encoding="utf-8") as fh:
                    self._revoked_disk = {k: int(v) for k, v in json.load(fh).items() if int(v) > time.time()}
            except (OSError, ValueError, AttributeError):
                pass

    # ---- passwords ------------------------------------------------------------------------------

    def locked_out(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._fails[ip]
        while q and q[0] < now - self.lockout_s:
            q.popleft()
        return len(q) >= self.max_failures

    def login(self, ip: str, user: str, password: str, now: float | None = None) -> str | None:
        """Return a session token, or None (wrong details or locked out)."""
        if self.locked_out(ip, now):
            return None
        ok_user = hmac.compare_digest(user.encode(), self.user.encode())
        ok_pw = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self._pw)
        if ok_user and ok_pw:
            self._fails.pop(ip, None)
            return self._token(self.user, now)
        self.record_failure(ip, now)
        return None

    def record_failure(self, ip: str, now: float | None = None) -> None:
        self._fails[ip].append(now or time.time())

    def issue(self, ip: str, subject: str, now: float | None = None) -> str:
        """Session for a user account that the account store has already checked."""
        if "|" in subject:
            raise ValueError("bad subject")
        self._fails.pop(ip, None)
        return self._token(subject, now)

    def is_admin(self, subject: str | None) -> bool:
        return bool(subject) and hmac.compare_digest(subject.encode(), self.user.encode())

    # ---- sessions -------------------------------------------------------------------------------

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def _token(self, user: str, now: float | None = None) -> str:
        expires = int((now or time.time()) + self.session_s)
        payload = f"{user}|{expires}|{secrets.token_hex(8)}"
        raw = f"{payload}|{self._sign(payload)}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")  # cookie-safe

    def verify(self, token: str | None, now: float | None = None) -> str | None:
        """The user name for a valid, unexpired token."""
        if not token or token in self._revoked or _digest(token) in self._revoked_disk:
            return None
        try:
            raw = base64.urlsafe_b64decode(token.encode() + b"=" * (-len(token) % 4)).decode()
            user, expires, nonce, sig = raw.split("|")
        except Exception:
            return None
        if not hmac.compare_digest(sig, self._sign(f"{user}|{expires}|{nonce}")):
            return None
        if int(expires) < (now or time.time()):
            return None
        ended = self._revoked_disk.get("nb:" + user)  # sessions ended (password changed): their expiry
        if ended and int(expires) <= ended:
            return None
        return user

    def end_sessions(self, subject: str, now: float | None = None) -> None:
        """Log out every session of ``subject`` issued before this second (after a password change);
        one issued from this second on still works."""
        self._revoked_disk["nb:" + subject] = int((now or time.time()) + self.session_s) - 1
        self._save_revoked()

    def revoke(self, token: str) -> None:
        """Log a session out. With ``revoked_path`` it stays logged out after a restart too."""
        self._revoked.add(token)
        if not self._revoked_path:
            return
        now = time.time()
        expires = now + self.session_s
        try:
            raw = base64.urlsafe_b64decode(token.encode() + b"=" * (-len(token) % 4)).decode()
            expires = int(raw.split("|")[1])
        except Exception:
            pass
        self._revoked_disk = {k: v for k, v in self._revoked_disk.items() if v > now}
        self._revoked_disk[_digest(token)] = int(expires)
        self._save_revoked()

    def _save_revoked(self) -> None:
        if not self._revoked_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._revoked_path)), exist_ok=True)
            tmp = self._revoked_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._revoked_disk, fh)
            os.replace(tmp, self._revoked_path)
        except OSError:
            pass  # still logged out in memory

    def csrf(self, token: str) -> str:
        """Form token tied to the session, checked on every POST."""
        return self._sign("csrf|" + token)[:32]

    def check_csrf(self, token: str | None, value: str | None) -> bool:
        return bool(token and value) and hmac.compare_digest(self.csrf(token), value)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
