"""The admin's Settings page: API keys, first-time setup and the health check.

Keys entered here are checked first (Binance keys with the exchange, the Anthropic key and the
Telegram bot with their APIs), then stored encrypted (``ops.secrets``) and never shown again,
only their last 4 characters. A real-account Binance key is accepted only without withdrawal
rights and with an IP whitelist, the same rule as for user accounts.

Live trading can't be unlocked here: that stays in ``.env`` on the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..accounts import AccountError, KeyRejected, KeyUnverified, verify_keys
from ..ops import secrets
from .app import _csrf, _e, _q

GROUPS = [
    ("anthropic", "Anthropic (Claude)", "Lets the agent read trading books and write strategies. "
     "Get a key at console.anthropic.com, under API keys."),
    ("binance_testnet", "Binance testnet", "Fake-money trading on testnet.binance.vision, for Phase 5. "
     "See the 'connect Binance' guide, stage 1."),
    ("binance", "Binance real account", "Only for the Phase 6 live pilot. The key must have withdrawals "
     "OFF and be restricted to this server's IP. Saving it does not start live trading: that stays "
     "locked in .env until you open it on the server."),
    ("telegram", "Telegram alerts", "Trade alerts and /status, /pause, /kill from your phone. Create a "
     "bot with @BotFather for the token; send it a message, then get your chat ID from @userinfobot."),
]


def _api_error(exc) -> str:
    """Anthropic's own error message from an HTTPError body (never contains the key)."""
    try:
        return str(json.loads(exc.read())["error"]["message"])[:200]
    except Exception:
        return ""


def check_anthropic(key: str, opener=urllib.request.urlopen) -> None:
    """Reject only when Anthropic says the key is bad; anything else means 'couldn't check'."""
    if not key.startswith("sk-ant-"):
        raise KeyRejected("that isn't an Anthropic API key: it should start with sk-ant- (create one "
                          "at console.anthropic.com, Settings, API keys)")
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "user-agent": "aitrader"})
    try:
        with opener(req, timeout=15) as resp:
            json.loads(resp.read())
    except Exception as exc:
        code = getattr(exc, "code", None)
        detail = _api_error(exc) if code else ""
        if code == 401:
            raise KeyRejected("Anthropic says this key is invalid; copy it again, or create a new "
                              "one at console.anthropic.com") from None
        what = f"HTTP {code}" if code else type(exc).__name__
        raise KeyUnverified(f"Anthropic didn't confirm the key ({what}{': ' + detail if detail else ''})") from None


def check_telegram(token: str, chat_id: str, opener=urllib.request.urlopen) -> None:
    from ..ops.telegram import Telegram

    try:
        Telegram(token, chat_id, opener=opener).send("aitrader: Telegram alerts are connected.")
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code in (400, 401, 403, 404):
            raise KeyRejected("Telegram refused it: check the bot token and chat ID, and that you sent "
                              f"your bot a message first so it may write to you (HTTP {code})") from None
        raise KeyUnverified(f"couldn't reach Telegram to check it ({type(exc).__name__})") from None


def add_settings_routes(app, *, settings, auth, paths, jobs, keys_secret: str, page, guard,
                        checkers: dict | None = None) -> None:
    checkers = {"anthropic": check_anthropic, "telegram": check_telegram, "binance": verify_keys,
                **(checkers or {})}
    store_path = getattr(paths, "keys_store", secrets.DEFAULT_PATH)

    def store():
        return secrets.SecretStore(keys_secret, store_path)

    def stored() -> dict:
        try:
            return store().load()
        except Exception:
            return {}

    def done(msg: str = "", error: str = "") -> RedirectResponse:
        q = f"?msg={_q(msg)}" if msg else f"?error={_q(error)}" if error else ""
        return RedirectResponse("/settings" + q, status_code=303)

    def post_guard(request: Request, csrf: str):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        return None

    def save(values: dict) -> None:
        store().save(values)
        os.environ.update({k: v.strip() for k, v in values.items() if v.strip()})
        for k, v in values.items():
            if not v.strip():
                os.environ.pop(k, None)

    @app.get("/settings")
    def settings_page(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        saved = stored()
        body = ""
        if msg:
            body += f'<p class="good">{_e(msg)}</p>'
        if error:
            body += f'<p class="bad">{_e(error)}</p>'
        body += _health(settings)
        body += _setup_card(token, auth, jobs)
        if len(keys_secret or "") < 32:
            body += ('<p class="bad">AITRADER_KEYS_SECRET is missing in .env, so keys can\'t be saved '
                     'here. Run the installer again (it creates one), or add it by hand.</p>')
        body += "<h2>API keys</h2><p class=muted>Saved keys are encrypted on the server and never " \
                "shown again (only their last 4 characters). A key saved here replaces the same key " \
                "in .env. Each key is checked before it is saved.</p>"
        for group, title, help_text in GROUPS:
            fields = [f for f in secrets.FIELDS if f.group == group]
            rows = ""
            for f in fields:
                if f.name in saved:
                    state = f'<span class="good">saved here {_e(secrets.masked(saved[f.name]))}</span>'
                elif os.environ.get(f.name):
                    state = '<span class="good">set in .env</span>'
                else:
                    state = '<span class="muted">not set</span>'
                kind = "password" if f.secret else "text"
                rows += (f'<label>{_e(f.label)} · {state}<input type="{kind}" name="{f.name}" '
                         f'autocomplete="off" spellcheck="false" placeholder="paste to set or replace"></label>')
            remove = ""
            if any(f.name in saved for f in fields):
                remove = (f'<form method="post" action="/settings/keys/{group}/delete" class="inline">'
                          f'{_csrf(auth, token)}<button class="ghost">Remove saved keys</button></form>')
            body += (f'<div class="card stack"><h3>{_e(title)}</h3><p class=muted>{_e(help_text)}</p>'
                     f'<form method="post" action="/settings/keys/{group}">{_csrf(auth, token)}{rows}'
                     f'<button>Check and save</button></form>{remove}</div>')
        body += ('<p class=muted>Live trading is unlocked only in .env on the server, never from this '
                 'page.</p>')
        return page("Settings", body, user, token)

    @app.post("/settings/keys/{group}")
    async def save_keys(request: Request, group: str):
        form = await request.form()
        bad = post_guard(request, str(form.get("csrf", "")))
        if bad:
            return bad
        fields = [f for f in secrets.FIELDS if f.group == group]
        if not fields:
            return done(error="unknown key group")
        current = {**{f.name: os.environ.get(f.name, "") for f in fields}, **stored()}
        given = {f.name: str(form.get(f.name, "")).strip() for f in fields}
        if not any(given.values()):
            return done(error="paste the key first")
        values = {f.name: given[f.name] or current.get(f.name, "") for f in fields}
        warning = ""
        try:
            if group == "anthropic":
                await asyncio.to_thread(checkers["anthropic"], values["ANTHROPIC_API_KEY"])
            elif group == "telegram":
                if not (values["TELEGRAM_BOT_TOKEN"] and values["TELEGRAM_CHAT_ID"]):
                    return done(error="Telegram needs both the bot token and the chat ID")
                await asyncio.to_thread(checkers["telegram"], values["TELEGRAM_BOT_TOKEN"],
                                        values["TELEGRAM_CHAT_ID"])
            else:
                testnet = group == "binance_testnet"
                prefix = "BINANCE_TESTNET_API" if testnet else "BINANCE_API"
                key, secret = values[f"{prefix}_KEY"], values[f"{prefix}_SECRET"]
                if not (key and secret):
                    return done(error="Binance needs both the API key and the secret key")
                await checkers["binance"]("binance", testnet, key, secret, settings)
        except KeyUnverified as exc:
            if group == "binance":  # a real-money key is saved only once its permissions are confirmed
                return done(error=f"Not saved: {exc}. Try again in a minute.")
            warning = f" (Couldn't confirm it yet: {exc}. Saved anyway; it is used as soon as the service can reach it.)"
        except KeyRejected as exc:
            return done(error=f"Not saved: {exc}")
        try:
            save({k: v for k, v in values.items() if given[k]})
        except AccountError as exc:
            return done(error=str(exc))
        title = dict((g, t) for g, t, _ in GROUPS)[group]
        if warning:
            return done(msg=f"{title}: saved.{warning}")
        extra = " Telegram sent you a test message." if group == "telegram" else ""
        return done(msg=f"{title}: checked and saved.{extra} Services using it restart by themselves "
                        f"within a minute.")

    @app.post("/settings/keys/{group}/delete")
    def delete_keys(request: Request, group: str, csrf: str = Form("")):
        bad = post_guard(request, csrf)
        if bad:
            return bad
        fields = [f for f in secrets.FIELDS if f.group == group]
        if not fields:
            return done(error="unknown key group")
        try:
            save({f.name: "" for f in fields})
        except AccountError as exc:
            return done(error=str(exc))
        return done(msg="Removed the saved keys (values in .env, if any, are used again after a restart).")

    @app.post("/settings/setup")
    def setup(request: Request, csrf: str = Form(""), since: str = Form("2024-01-01")):
        bad = post_guard(request, csrf)
        if bad:
            return bad
        if any(j["status"] in ("queued", "running") and j.get("meta", {}).get("kind") == "setup"
               for j in jobs.list()):
            return done(error="setup is already running; see Recent jobs on the Overview page")
        since = since if len(since) == 10 and since[4] == "-" and since[7] == "-" else "2024-01-01"
        jobs.submit("Download price history and load the strategy library",
                    [["download", "--since", since, "--exchange", "binance"],
                     ["learn", "add-library", "--test"]], meta={"kind": "setup"})
        return done(msg="Setup started: downloading price history (this takes a while), then the "
                        "strategies are backtested; the ones that pass start paper trading.")


def _health(settings) -> str:
    from ..ops.doctor import FAIL, PASS, run_checks

    try:
        checks = run_checks(settings)
    except Exception as exc:  # never let the health check break the page
        return f'<p class="bad">Health check failed to run: {_e(type(exc).__name__)}</p>'
    rows = "".join(
        f'<tr><td class="{"good" if lvl == PASS else "bad" if lvl == FAIL else "warn"}">{_e(lvl)}</td>'
        f'<td>{_e(name)}</td><td>{_e(text)}</td></tr>' for lvl, name, text in checks)
    fails = sum(1 for c in checks if c[0] == FAIL)
    warns = sum(1 for c in checks if c[0] not in (PASS, FAIL))
    head = (f'<p class="{"bad" if fails else "good"}">{fails} problem(s), {warns} warning(s)</p>')
    return f'<h2>Health check</h2>{head}<table>{rows}</table>'


def _setup_card(token, auth, jobs) -> str:
    running = [j for j in jobs.list() if j.get("meta", {}).get("kind") == "setup"]
    last = running[0] if running else None
    status = ""
    if last:
        status = (f'<p>Last setup: <a href="/jobs/{_e(last["id"])}">{_e(last["status"])}</a></p>')
    return (f'<h2>First-time setup</h2><div class="card"><p>Downloads price history from Binance '
            f'(public data, no key needed), then backtests the strategy library; strategies that '
            f'pass start paper trading. The download takes a few hours the first time; you can '
            f'close this page.</p>{status}<form method="post" action="/settings/setup">'
            f'{_csrf(auth, token)}<label>History from (YYYY-MM-DD)<input name="since" '
            f'value="2024-01-01"></label><button>Start setup</button></form></div>')
