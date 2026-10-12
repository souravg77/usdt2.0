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
import time
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
    ("x", "X (Twitter)", "Lets the agent read X: the accounts on its watch list and posts about each "
     "coin. Read-only: it never posts, likes or follows. At console.x.com create an app, add credits, "
     "then copy the app's Bearer token. X charges per post read; the daily cap is "
     "AITRADER_X_DAILY_READS (800 posts, about 4 USD)."),
]


def _api_error(exc) -> str:
    """Anthropic's own error message from an HTTPError body (never contains the key)."""
    try:
        return str(json.loads(exc.read())["error"]["message"])[:200]
    except Exception:
        return ""


def check_anthropic(key: str, workspace: str = "", opener=urllib.request.urlopen) -> None:
    """Reject only when Anthropic says the key is bad; anything else means 'couldn't check'."""
    from ..learn.extract import anthropic_headers

    if not key.startswith("sk-ant-"):
        raise KeyRejected("that isn't an Anthropic API key: it should start with sk-ant- (create one "
                          "at console.anthropic.com, Settings, API keys)")
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
                                 headers=anthropic_headers(key, workspace))
    try:
        with opener(req, timeout=15) as resp:
            json.loads(resp.read())
    except Exception as exc:
        code = getattr(exc, "code", None)
        detail = _api_error(exc) if code else ""
        if code == 401:
            raise KeyRejected("Anthropic says this key is invalid; copy it again, or create a new "
                              "one at console.anthropic.com") from None
        if code == 400 and "workspace" in detail.lower():
            raise KeyRejected("this key isn't tied to a workspace, so the agent can't use it as is. "
                              "Either create a new key inside a workspace (console.anthropic.com, "
                              "Settings, Workspaces, open Default, API keys), or paste the workspace "
                              "ID (starts with wrkspc_) in the Workspace ID box with this key"
                              + (" (the workspace ID given was refused)" if workspace else "")) from None
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


def check_x(token: str, opener=urllib.request.urlopen) -> None:
    req = urllib.request.Request("https://api.x.com/2/usage/tweets",
                                 headers={"authorization": f"Bearer {token}", "user-agent": "aitrader"})
    try:
        with opener(req, timeout=15) as resp:
            json.loads(resp.read())
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == 401:
            raise KeyRejected("X says this Bearer token is invalid; copy it again from your app at "
                              "console.x.com (Keys and tokens)") from None
        what = f"HTTP {code}" if code else type(exc).__name__
        raise KeyUnverified(f"X didn't confirm the token ({what})") from None


def add_settings_routes(app, *, settings, auth, paths, jobs, keys_secret: str, page, guard,
                        checkers: dict | None = None) -> None:
    checkers = {"anthropic": check_anthropic, "telegram": check_telegram, "binance": verify_keys, "x": check_x,
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

    async def check(group: str, values: dict) -> str:
        """Run the group's check. Returns '' when confirmed, or the reason it couldn't run;
        raises KeyRejected when the service refuses, ValueError when something is missing."""
        try:
            if group == "anthropic":
                if not values["ANTHROPIC_API_KEY"]:
                    raise ValueError("paste the Anthropic API key too")
                await asyncio.to_thread(checkers["anthropic"], values["ANTHROPIC_API_KEY"],
                                        values["ANTHROPIC_WORKSPACE_ID"])
            elif group == "telegram":
                if not (values["TELEGRAM_BOT_TOKEN"] and values["TELEGRAM_CHAT_ID"]):
                    raise ValueError("Telegram needs both the bot token and the chat ID")
                await asyncio.to_thread(checkers["telegram"], values["TELEGRAM_BOT_TOKEN"],
                                        values["TELEGRAM_CHAT_ID"])
            elif group == "x":
                if not values["X_BEARER_TOKEN"]:
                    raise ValueError("paste the X Bearer token")
                await asyncio.to_thread(checkers["x"], values["X_BEARER_TOKEN"])
            else:
                testnet = group == "binance_testnet"
                prefix = "BINANCE_TESTNET_API" if testnet else "BINANCE_API"
                key, secret = values[f"{prefix}_KEY"], values[f"{prefix}_SECRET"]
                if not (key and secret):
                    raise ValueError("Binance needs both the API key and the secret key")
                await checkers["binance"]("binance", testnet, key, secret, settings)
        except KeyUnverified as exc:
            if group == "binance":  # a real-money key is saved only once its permissions are confirmed
                raise KeyRejected(f"{exc}. Try again in a minute") from None
            record(group, None, str(exc))
            return str(exc)
        record(group, True, "working")
        return ""

    checks_path = os.path.join(os.path.dirname(store_path) or ".", "checks.json")

    def last_checks() -> dict:
        try:
            with open(checks_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def record(group: str, ok, text: str = "") -> None:
        """Remember the last check result per group (no key material, just the outcome);
        ``ok=None`` with no text forgets it."""
        data = last_checks()
        if ok is None and not text:
            data.pop(group, None)
        else:
            data[group] = {"ok": ok, "text": text[:300], "ts": int(time.time())}
        try:
            os.makedirs(os.path.dirname(checks_path) or ".", exist_ok=True)
            with open(checks_path, "w") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    def current(fields) -> dict:
        return {**{f.name: os.environ.get(f.name, "") for f in fields}, **stored()}

    @app.get("/settings")
    def settings_page(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        saved = stored()
        checked = last_checks()
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
                "shown again (only dots and their last 4 characters). Leave a box empty to keep the " \
                "saved key; \"Check and save\" with empty boxes checks the saved key again. A key saved here " \
                "replaces the same key in .env.</p>"
        for group, title, help_text in GROUPS:
            fields = [f for f in secrets.FIELDS if f.group == group]
            rows = ""
            for f in fields:
                value = saved.get(f.name) or os.environ.get(f.name, "")
                if f.name in saved:
                    state = '<span class="good">saved</span>'
                elif value:
                    state = '<span class="good">set in .env</span>'
                else:
                    state = '<span class="muted">not set</span>'
                if not value:
                    hint = "paste to set"
                elif f.secret:  # dots plus the last 4 characters; the key itself never leaves the server
                    hint = "•" * 12 + (" " + value[-4:] if len(value) > 8 else "")
                else:
                    hint = value
                kind = "password" if f.secret else "text"
                rows += (f'<label>{_e(f.label)} · {state}<input type="{kind}" name="{f.name}" '
                         f'autocomplete="off" spellcheck="false" placeholder="{_e(hint)}"></label>')
            has_value = any(saved.get(f.name) or os.environ.get(f.name) for f in fields)
            last = checked.get(group)
            status = ""
            if last and has_value:
                cls = "good" if last["ok"] else "bad" if last["ok"] is False else "warn"
                word = "Working" if last["ok"] else "Not working" if last["ok"] is False else "Not confirmed"
                when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last["ts"]))
                detail = "" if last["ok"] else f": {last['text']}"
                status = f'<p class="{cls}">Last check: {word}{_e(detail)} ({when})</p>'
            elif has_value:
                status = '<p class=muted>Not checked yet: tap "Test saved key".</p>'
            extra = ""
            if has_value:
                extra += (f'<form method="post" action="/settings/keys/{group}/test" class="inline">'
                          f'{_csrf(auth, token)}<button class="ghost">Test saved key</button></form> ')
            if any(f.name in saved for f in fields):
                extra += (f'<form method="post" action="/settings/keys/{group}/delete" class="inline">'
                          f'{_csrf(auth, token)}<button class="ghost">Remove saved keys</button></form>')
            body += (f'<div class="card stack"><h3>{_e(title)}</h3><p class=muted>{_e(help_text)}</p>'
                     f'{status}<form method="post" action="/settings/keys/{group}">{_csrf(auth, token)}{rows}'
                     f'<button>Check and save</button></form><p>{extra}</p></div>')
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
        now = current(fields)
        given = {f.name: str(form.get(f.name, "")).strip() for f in fields}
        if not any(given.values()):  # nothing typed: check the keys already saved
            if not any(now.values()):
                return done(error="paste the key first")
            return await test_saved(group, fields)
        values = {f.name: given[f.name] or now.get(f.name, "") for f in fields}
        try:
            warning = await check(group, values)
        except KeyRejected as exc:  # the saved key (if any) is unchanged, so its last check stands
            return done(error=f"Not saved: {exc}")
        except ValueError as exc:
            return done(error=str(exc))
        try:
            save({k: v for k, v in values.items() if given[k]})
        except AccountError as exc:
            return done(error=str(exc))
        title = dict((g, t) for g, t, _ in GROUPS)[group]
        if warning:
            return done(msg=f"{title}: saved. (Couldn't confirm it yet: {warning}. Saved anyway; it is "
                            f"used as soon as the service can reach it.)")
        extra = " Telegram sent you a test message." if group == "telegram" else ""
        return done(msg=f"{title}: checked and saved.{extra} Services using it restart by themselves "
                        f"within a minute.")

    @app.post("/settings/keys/{group}/test")
    async def test_keys(request: Request, group: str, csrf: str = Form("")):
        bad = post_guard(request, csrf)
        if bad:
            return bad
        fields = [f for f in secrets.FIELDS if f.group == group]
        if not fields:
            return done(error="unknown key group")
        return await test_saved(group, fields)

    async def test_saved(group: str, fields) -> RedirectResponse:
        title = dict((g, t) for g, t, _ in GROUPS)[group]
        try:
            note = await check(group, current(fields))
        except KeyRejected as exc:
            record(group, False, str(exc))
            return done(error=f"{title}: not working: {exc}")
        except ValueError as exc:
            return done(error=f"{title}: {exc}")
        if note:
            return done(error=f"{title}: couldn't confirm it: {note}. Try again in a minute.")
        extra = " Telegram sent you a test message." if group == "telegram" else ""
        return done(msg=f"{title}: working.{extra}")

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
        record(group, None)
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
    head = f'<span class="{"bad" if fails else "good"}">{fails} problem(s), {warns} warning(s)</span>'
    return (f'<h2>Health check</h2><details class="card health"{" open" if fails else ""}>'
            f'<summary>{head} <span class="muted small">· details</span>'
            f'</summary><table>{rows}</table></details>')


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
