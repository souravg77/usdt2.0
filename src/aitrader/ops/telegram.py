"""Telegram alerts and commands (roadmap Phase 5, step 4, and the kill switch by Telegram).

The ``telegram`` service tails the shared event log and sends trades, errors, disconnects, risk
pauses, daily summaries and weekly reports to one chat. It also answers commands, but only from
that chat:

    /status          mode, live lock, pause state, open positions, today's results
    /pause           stop new entries
    /resume          allow entries again (clears a risk pause or a kill)
    /kill CONFIRM    kill switch: stop entries and sell everything the executors bought
    /help

Set ``TELEGRAM_BOT_TOKEN`` (from @BotFather) and ``TELEGRAM_CHAT_ID`` in ``.env``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .events import ALERT_KINDS, EventLog

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_PER_MINUTE = 18  # Telegram allows about 20 messages a minute to one chat
ICON = {"trade_open": "🟢", "trade_close": "🔵", "error": "⚠️", "crash": "🛑", "disconnect": "📡",
        "reconnect": "✅", "risk_pause": "⏸", "resume": "▶️", "kill": "🛑", "daily_summary": "📊",
        "weekly_report": "📈", "gate": "🚦", "live_switch": "💰"}


class Telegram:
    def __init__(self, token: str, chat_id: str, opener=urllib.request.urlopen):
        if not token or not chat_id:
            raise ValueError("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        self.token, self.chat_id, self._open = token, str(chat_id), opener

    def _call(self, method: str, params: dict, timeout: float = 15) -> dict:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=data)
        with self._open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {body.get('description')}")
        return body

    def send(self, text: str) -> None:
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:4000],
                                   "disable_web_page_preview": "true"})

    def updates(self, offset: int, timeout: int = 20) -> list[dict]:
        return self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "allowed_updates": '["message"]'}, timeout=timeout + 10)["result"]


class TelegramService:
    def __init__(self, bot: Telegram, events: EventLog, control, status_fn, summary_fn=None,
                 clock=time.time):
        self.bot, self.events, self.control = bot, events, control
        self.status_fn, self.summary_fn, self.clock = status_fn, summary_fn, clock
        self._offset_events = None
        self._offset_updates = 0
        self._sent: list[float] = []
        self._last_summary_day = None

    # ---- alerts ---------------------------------------------------------------------------------

    def pump_alerts(self) -> int:
        if self._offset_events is None:  # start at the end: don't replay history on restart
            try:
                self._offset_events = os.path.getsize(self.events.path)
            except OSError:
                self._offset_events = 0
        evs, self._offset_events = self.events.tail(self._offset_events)
        sent = 0
        for ev in evs:
            if ev.get("kind") not in ALERT_KINDS:
                continue
            if ev["kind"] == "trade_open" and ev.get("service") == "paper":
                continue  # paper entries are frequent; closes and everything else are sent
            if self._rate_limited():
                continue
            self.bot.send(f"{ICON.get(ev['kind'], '•')} {ev['text']}")
            sent += 1
        return sent

    def _rate_limited(self) -> bool:
        now = self.clock()
        self._sent = [t for t in self._sent if now - t < 60]
        if len(self._sent) >= MAX_PER_MINUTE:
            return True
        self._sent.append(now)
        return False

    def daily(self) -> None:
        """Once a day just after 00:00 UTC, post yesterday's summary."""
        if self.summary_fn is None:
            return
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        day = now.date()
        if self._last_summary_day is None:
            self._last_summary_day = day  # first run: wait for the next day
            return
        if day != self._last_summary_day and now.hour == 0:
            self._last_summary_day = day
            text = self.summary_fn(day - timedelta(days=1))
            self.events.emit("daily_summary", text)

    # ---- commands -------------------------------------------------------------------------------

    def handle(self, update: dict) -> str | None:
        msg = update.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        if chat != self.bot.chat_id:
            log.warning("ignored telegram command from chat %s", chat)
            return None
        who = (msg.get("from") or {}).get("username") or "telegram"
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/status":
            reply = self.status_fn()
        elif cmd == "/pause":
            self.control.pause("paused from Telegram", by=who)
            self.events.emit("risk_pause", f"paused from Telegram by {who}", level="warning")
            reply = "Paused: no new entries. /resume to continue."
        elif cmd == "/resume":
            self.control.resume(by=who)
            self.events.emit("resume", f"resumed from Telegram by {who}")
            reply = "Resumed."
        elif cmd == "/kill":
            if arg.strip().upper() != "CONFIRM":
                reply = "This sells every position the executors opened and stops trading. Send /kill CONFIRM"
            else:
                self.control.kill("kill switch from Telegram", by=who)
                self.events.emit("kill", f"kill switch pulled from Telegram by {who}", level="warning")
                reply = "Kill switch on: entries stopped, executors are selling. /resume when ready."
        else:
            reply = "/status, /pause, /resume, /kill CONFIRM"
        self.bot.send(reply)
        return reply

    def poll_commands(self, timeout: int = 20) -> None:
        for upd in self.bot.updates(self._offset_updates, timeout):
            self._offset_updates = max(self._offset_updates, upd["update_id"] + 1)
            try:
                self.handle(upd)
            except Exception:
                log.exception("telegram command failed")

    def run(self, stop_after: int | None = None, poll_timeout: int = 20) -> None:
        n = 0
        self.bot.send("aitrader alerts connected. /help for commands.")
        while stop_after is None or n < stop_after:
            try:
                self.pump_alerts()
                self.daily()
                self.poll_commands(poll_timeout)
            except Exception as exc:  # network trouble: wait and retry
                log.warning("telegram loop: %r", exc)
                time.sleep(10)
            n += 1
