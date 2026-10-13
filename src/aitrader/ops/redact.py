"""Keep secrets out of logs, the event log, alerts and crash reports.

An exchange error can carry the whole signed request (ccxt puts the URL, with its query string
and signature, in the exception text), so every text that leaves a service passes through
``redact``:

- the value of every secret in the environment (names with KEY, SECRET, TOKEN or PASSWORD);
- URL query strings (Bybit signs them);
- Bybit's signing headers (``X-BAPI-SIGN``, ``X-BAPI-API-KEY``, ...) and ``key=value`` pairs
  named like a credential;
- token shapes: Anthropic (``sk-ant-``), GitHub (``ghp_``, ``github_pat_``), Telegram bot URLs.

``install()`` puts it on every log handler (messages and tracebacks) and on uncaught
exceptions; ``EventLog.emit`` calls it on each event.
"""

from __future__ import annotations

import logging
import os
import re
import sys

MASK = "[redacted]"
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD", re.I)
MIN_SECRET_LEN = 8

_PATTERNS = [
    # a URL's query string: Bybit's signed GET requests carry the signature there
    (re.compile(r"(https?://[^\s'\"?<>]+)\?[^\s'\"<>]*"), r"\1?" + MASK),
    # Bybit signing headers, as text or a dict
    (re.compile(r"(X-BAPI-[A-Z-]+['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", re.I), r"\1" + MASK),
    # credential-like key=value / "key": "value" pairs
    (re.compile(r"((?:api[_-]?key|api[_-]?secret|apikey|secret|password|passphrase|token|signature|sign)"
                r"['\"]?\s*[:=]\s*['\"]?)([^'\",\s&}]{4,})", re.I), r"\1" + MASK),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + MASK),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot" + MASK),
]


def _secret_values() -> list[str]:
    vals = {v for k, v in os.environ.items() if SECRET_NAME.search(k) and v and len(v) >= MIN_SECRET_LEN}
    return sorted(vals, key=len, reverse=True)  # longest first, so one secret can't half-hide another


def redact(text) -> str:
    """``text`` with every secret it might contain replaced by ``[redacted]``."""
    s = str(text)
    for v in _secret_values():
        if v in s:
            s = s.replace(v, MASK)
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    return s


def redact_data(value):
    """``redact`` applied to every string inside dicts and lists (event data)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(v) for v in value]
    return value


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's formatter: the finished line, traceback included, is redacted."""

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


def install(logger: logging.Logger | None = None) -> None:
    """Redact every log record (any logger and handler, set up now or later, e.g. the web
    server's), the root handlers' finished lines, and uncaught exceptions."""
    factory = logging.getLogRecordFactory()
    if not getattr(factory, "_redacting", False):
        def make(*args, **kwargs):
            record = factory(*args, **kwargs)
            if record.name.startswith("uvicorn.access"):
                return record  # its formatter reads the args; dashboard URLs carry no secrets
            try:
                msg = record.getMessage()
            except Exception:  # a message that can't be formatted is left for logging to report
                return record
            clean = redact(msg)
            if clean != msg:
                record.msg, record.args = clean, ()
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            return record

        make._redacting = True
        logging.setLogRecordFactory(make)
    logger = logger or logging.getLogger()
    for h in logger.handlers:
        if not isinstance(h.formatter, RedactingFormatter):
            h.setFormatter(RedactingFormatter(h.formatter))
    previous = sys.excepthook
    if getattr(previous, "_redacting", False):
        return

    def hook(exc_type, exc, tb):
        import traceback

        sys.stderr.write(redact("".join(traceback.format_exception(exc_type, exc, tb))))

    hook._redacting = True
    sys.excepthook = hook
