"""Pages for user accounts: sign-up, each user's own page (exchange keys, mode, positions,
trades, pause and kill switch) and the admin's Users page.

A user sees only their own account. Keys are checked with the exchange before they are stored,
stored encrypted, and never shown again (only their last 4 characters).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..accounts import AccountError, KeyRejected, UserStore, Vault, verify_keys
from ..accounts.live import gate_problems, live_stop_control, turn_off, turn_on
from ..accounts.runner import user_control, user_dir
from ..config import EXCHANGE, LIVE_CONFIRM_PHRASE
from ..safety import live_gate_problems
from .app import _csrf, _e, _equity_svg, _load_json, _positions_table, _q, _trades_table, _when

SIGNUPS_PER_HOUR = 3
USER_NAV = '<a href="/me">My account</a>'


def add_user_routes(app, *, settings, auth, paths, users: UserStore, keys_secret: str, signup: bool,
                    verifier, page, session, guard) -> None:
    verifier = verifier or verify_keys
    users_dir = getattr(paths, "users_dir", "data/users")
    signups: dict[str, deque] = defaultdict(deque)

    def vault() -> Vault:
        return Vault(keys_secret)

    def events():
        from ..ops import EventLog

        return EventLog(paths.events, service="dashboard")

    def golive():
        from ..trade import GateConfig, GoLive

        return GoLive(paths.golive_dir, paths.registry, paths.paper_state, paths.models,
                      events=events(), cfg=GateConfig.from_env())

    def me(request: Request):
        subject, token = session(request)
        if auth.is_admin(subject):
            return None, None, RedirectResponse("/", status_code=303)
        user = users.from_subject(subject)
        if user is None or user.status == "disabled":
            return None, None, RedirectResponse("/login", status_code=303)
        return user, token, None

    def back(msg: str = "", error: str = "") -> RedirectResponse:
        q = f"?msg={_q(msg)}" if msg else f"?error={_q(error)}" if error else ""
        return RedirectResponse("/me" + q, status_code=303)

    def checked(request: Request, csrf: str):
        user, token, redirect = me(request)
        if redirect:
            return None, redirect
        if not auth.check_csrf(token, csrf):
            return None, PlainTextResponse("bad form token; reload the page", status_code=403)
        return user, None

    # ---- sign up --------------------------------------------------------------------------------

    @app.get("/signup")
    def signup_form(error: str = ""):
        if not signup:
            return page("Sign up", "<p>Sign-up is closed. Ask the admin for an account.</p>")
        err = f'<p class="bad">{_e(error)}</p>' if error else ""
        return page("Create an account", f"""{err}
<form method="post" action="/signup" class="card narrow">
<label>Email <input name="email" type="email" autocomplete="email" required></label>
<label>Password (at least 12 characters) <input name="password" type="password" autocomplete="new-password" minlength="12" required></label>
<label>Password again <input name="password2" type="password" autocomplete="new-password" required></label>
<label class="row"><input type="checkbox" name="agree" value="1" required> I understand that crypto
trading can lose money, that nothing trades on my Bybit account until live trading is allowed for
my account and I turn it on, and that I keep my exchange key without withdrawal rights.</label>
<button>Create account</button></form>
<p>Already have an account? <a href="/login">Log in</a></p>""")

    @app.post("/signup")
    def signup_submit(request: Request, email: str = Form(""), password: str = Form(""),
                      password2: str = Form(""), agree: str = Form("")):
        if not signup:
            return RedirectResponse("/signup", status_code=303)
        ip = request.client.host if request.client else "?"
        q = signups[ip]
        while q and q[0] < time.time() - 3600:
            q.popleft()
        if len(q) >= SIGNUPS_PER_HOUR:
            return RedirectResponse("/signup?error=" + _q("Too many sign-ups from here; try later."), status_code=303)
        if password != password2:
            return RedirectResponse("/signup?error=" + _q("The two passwords differ."), status_code=303)
        if not agree:
            return RedirectResponse("/signup?error=" + _q("Tick the box to confirm you understand the risk."),
                                    status_code=303)
        try:
            new = users.create(email, password)
            users.log(new.id, new.email, "signed_up", ip)
        except AccountError as exc:
            return RedirectResponse("/signup?error=" + _q(str(exc)), status_code=303)
        q.append(time.time())
        return RedirectResponse("/login?msg=signed_up", status_code=303)

    # ---- the user's own page --------------------------------------------------------------------

    @app.get("/me")
    def my_page(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = me(request)
        if redirect:
            return redirect
        csrf = _csrf(auth, token)
        d = user_dir(users_dir, user.id)
        ctl = user_control(users_dir, user.id).state()
        platform = _load_json(os.path.join(paths.trade_dir, "control.json"))
        execs = {m: _load_json(os.path.join(d, f"{m}_state.json")) for m in ("live",)}
        execs = {m: s for m, s in execs.items() if s}
        live_open = not live_gate_problems(settings)
        notes = (f'<p class="good">{_e(msg)}</p>' if msg else "") + (f'<p class="bad">{_e(error)}</p>' if error else "")

        if user.status == "pending":
            status = '<b class="bad">waiting for the admin to approve your account</b>'
        else:
            status = '<b class="good">active</b>'
        mode = ('<b class="bad">LIVE (real money)</b>' if user.mode == "live" and user.live_allowed and live_open
                else "<b>paper</b> (no orders on your account)")
        if ctl["killed"]:
            sw = '<b class="bad">KILL SWITCH ON</b>'
        elif ctl["paused"]:
            sw = '<b class="bad">paused</b>'
        elif platform.get("paused") or platform.get("killed"):
            sw = '<b class="bad">paused by the platform</b>'
        else:
            sw = '<b class="good">running</b>'
        acct = (f'<div class="card"><h3>Account</h3><p>{_e(user.email)}<br>Status {status}<br>'
                f'Trading on {mode} · {sw}</p><div class="row">'
                f'<form method="post" action="/me/pause">{csrf}<button>Pause new entries</button></form>'
                f'<form method="post" action="/me/resume">{csrf}<button class="ghost">Resume</button></form></div>'
                f'<form method="post" action="/me/kill" class="kill">{csrf}<label>Kill switch: sells everything '
                'the agent bought on your account. Type KILL to confirm <input name="confirm" autocomplete="off" '
                'placeholder="KILL"></label><button class="danger">Pull the kill switch</button></form></div>')

        rows = "".join(
            f'<tr><td>{_e(k["exchange"])} {"demo trading (no longer used)" if k["testnet"] else "real account"}</td>'
            f'<td><code>{_e(k["hint"])}</code></td><td>{_when(k["added"])}</td><td>'
            f'<form method="post" action="/me/keys/delete" class="inline">{csrf}'
            f'<input type="hidden" name="net" value="{"testnet" if k["testnet"] else "live"}">'
            f'<button>Remove</button></form></td></tr>' for k in users.key_info(user.id))
        keys_table = (f"<table><tr><th>Key</th><th>Ends with</th><th>Added</th><th></th></tr>{rows}</table>"
                      if rows else "<p class=muted>No exchange connected yet.</p>")
        if user.status != "active":
            key_form = "<p class=muted>You can connect an exchange once the admin approves your account.</p>"
        elif not keys_secret:
            key_form = "<p class=bad>The platform isn't set up to store keys yet (the admin must set AITRADER_KEYS_SECRET).</p>"
        else:
            key_form = f"""<form method="post" action="/me/keys">{csrf}
<label>Exchange <select name="exchange"><option value="bybit">Bybit</option></select></label>
<input type="hidden" name="net" value="live">
<label>API key <input name="api_key" autocomplete="off" required></label>
<label>Secret key <input name="secret" type="password" autocomplete="off" required></label>
<p class="small muted">Use a key from your real Bybit account. It is refused unless the key is Read-Write with only SPOT
trade ticked (no withdrawals, transfers or contracts) and bound to the server's IP. Keys are
stored encrypted and never shown again.</p>
<button>Check and connect</button></form>"""
        keys_card = f'<div class="card"><h3>Exchange keys</h3>{keys_table}{key_form}</div>'

        live_on = user.mode == "live" and user.live_allowed and live_open
        draining = live_stop_control(users_dir, user.id).killed and bool(
            execs.get("live", {}).get("positions"))
        if live_on:
            live_body = (f'<p class="switch on">Live trading <b>ON</b> · budget cap <b>{user.pilot_max_usdt or 0:,.2f} USDT</b></p>'
                         f'<form method="post" action="/me/live/off">{csrf}<button class="danger">'
                         'Turn live trading OFF now</button></form><p class="small muted">Instant: no new real-money '
                         'trades, the agent sells what it bought on your real account and goes back to paper.</p>')
        else:
            why = []
            if user.status != "active":
                why.append("your account isn't approved yet")
            if not user.live_allowed:
                why.append("the admin hasn't allowed live trading for your account")
            if not live_open:
                why.append("the platform's live lock is closed")
            gp = gate_problems(golive()) if live_open else []
            why += gp[:1]
            if not users.key_info(user.id, EXCHANGE, testnet=False):
                why.append("connect a real-account key first")
            sell = '<p class="bad small">Selling the positions from your last live session.</p>' if draining else ""
            if why:
                live_body = (f'<p class="switch">Live trading <b>OFF</b></p>{sell}<p class="small muted">Can\'t turn it on yet: '
                             f'{_e("; ".join(why))}.</p>')
            else:
                live_body = f"""<p class="switch">Live trading <b>OFF</b></p>{sell}
<form method="post" action="/me/live/on">{csrf}
<label>Budget cap: the most USDT the agent may use <input name="budget" type="number" min="10" step="1" required></label>
<label>Your password <input name="password" type="password" autocomplete="current-password" required></label>
<label>Type <b>{_e(LIVE_CONFIRM_PHRASE)}</b> <input name="confirm" autocomplete="off" required></label>
<button class="danger">Turn live trading ON</button></form>"""
        history = "".join(f'<li>{_when(a["ts"])}: {_e(a["action"].replace("_", " "))}'
                          f'{" by " + _e(a["actor"]) if a["actor"] != user.email else ""}'
                          f'{" (" + _e(a["detail"]) + ")" if a["detail"] else ""}</li>'
                          for a in users.audit(user.id, 8))
        live_card = (f'<div class="card"><h3>Real money</h3>{live_body}'
                     f'{"<h3>History</h3><ul class=small>" + history + "</ul>" if history else ""}</div>')

        pw_card = (f'<div class="card"><h3>Password</h3><form method="post" action="/me/password">{csrf}'
                   '<label>Current <input name="current" type="password" autocomplete="current-password" required></label>'
                   '<label>New (12+ characters) <input name="new" type="password" autocomplete="new-password" '
                   'minlength="12" required></label><button class="ghost">Change password</button></form></div>')
        body = [notes, '<div class="grid">', acct, keys_card, live_card, pw_card, "</div>",
                "<h2>Equity</h2>", _equity_svg({}, execs),
                "<h2>Open positions</h2>", _positions_table({}, execs),
                "<h2>Trade log</h2>", _trades_table({}, execs)]
        return page("My account", "".join(body), user.email, token, refresh=None, nav_links=USER_NAV)

    @app.post("/me/keys")
    async def my_keys(request: Request, csrf: str = Form(""), exchange: str = Form(EXCHANGE),
                      api_key: str = Form(""), secret: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        if user.status != "active":
            return back(error="Your account isn't approved yet.")
        try:
            v = vault()
            perms = await verifier(exchange, api_key, secret, settings)
        except (KeyRejected, AccountError) as exc:
            return back(error=f"Key not connected: {exc}")
        users.save_keys(v, user.id, exchange, False, api_key, secret, perms)
        users.log(user.id, user.email, "key_added", f"{exchange} real account")
        return back(msg=f"{exchange.title()} real-account key connected.")

    @app.post("/me/keys/delete")
    def my_keys_delete(request: Request, csrf: str = Form(""), net: str = Form("live")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        if net == "live":
            turn_off(users, users_dir, user.id, actor=user.email, reason="real-account key removed", events=events())
        users.delete_keys(user.id, EXCHANGE, net != "live")
        users.log(user.id, user.email, "key_removed", "old demo key" if net != "live" else "real account")
        return back(msg="Key removed. Remove it on the exchange too if you no longer use it.")

    @app.post("/me/live/on")
    def my_live_on(request: Request, csrf: str = Form(""), budget: float = Form(0), password: str = Form(""),
                   confirm: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        try:
            turn_on(users, settings, golive(), users_dir, user.id, password, confirm, budget,
                    actor=user.email, events=events())
        except AccountError as exc:
            return back(error=f"Live trading not turned on: {exc}")
        return back(msg="Live trading is ON. Turn it off here at any time.")

    @app.post("/me/live/off")
    def my_live_off(request: Request, csrf: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        turn_off(users, users_dir, user.id, actor=user.email, reason="switched off by the user", events=events())
        return back(msg="Live trading is OFF. Selling what the agent bought on your real account.")

    @app.post("/me/pause")
    def my_pause(request: Request, csrf: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        user_control(users_dir, user.id).pause("paused by the user", by=user.email)
        users.log(user.id, user.email, "pause")
        return back(msg="Paused: no new entries. Open positions are still managed.")

    @app.post("/me/resume")
    def my_resume(request: Request, csrf: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        user_control(users_dir, user.id).resume(by=user.email)
        users.log(user.id, user.email, "resume")
        return back(msg="Resumed.")

    @app.post("/me/kill")
    def my_kill(request: Request, csrf: str = Form(""), confirm: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        if confirm.strip() != "KILL":
            return back(error="Not pulled: type KILL to confirm.")
        user_control(users_dir, user.id).kill("kill switch from the user's page", by=user.email)
        turn_off(users, users_dir, user.id, actor=user.email, reason="kill switch", events=events())
        users.log(user.id, user.email, "kill_switch")
        return back(msg="Kill switch on: the agent is selling what it bought for you. Resume when ready.")

    @app.post("/me/password")
    def my_password(request: Request, csrf: str = Form(""), current: str = Form(""), new: str = Form("")):
        user, resp = checked(request, csrf)
        if resp:
            return resp
        if users.authenticate(user.email, current) is None:
            return back(error="The current password is wrong.")
        try:
            users.set_password(user.id, new)
        except AccountError as exc:
            return back(error=str(exc))
        return back(msg="Password changed.")

    # ---- admin: users ---------------------------------------------------------------------------

    @app.get("/admin/users")
    def admin_users(request: Request, msg: str = ""):
        admin, token, redirect = guard(request)
        if redirect:
            return redirect
        csrf = _csrf(auth, token)
        rows = []
        for u in users.list():
            keys = ", ".join(f'{k["exchange"]} real' for k in users.key_info(u.id, testnet=False)) or "-"
            st = _load_json(os.path.join(user_dir(users_dir, u.id), "live_state.json"))
            n_open = sum(1 for p in st.get("positions", {}).values() if p.get("status") == "open")
            pnl = sum(t.get("pnl_usdt", 0) for t in st.get("closed", []))
            actions = []
            for status, label in (("active", "Approve" if u.status == "pending" else "Enable"),
                                  ("disabled", "Disable")):
                if u.status != status:
                    actions.append(f'<form method="post" action="/admin/users/{u.id}/status" class="inline">{csrf}'
                                   f'<input type="hidden" name="status" value="{status}"><button>{label}</button></form>')
            live = "off" if u.live_allowed else "on"
            actions.append(f'<form method="post" action="/admin/users/{u.id}/live" class="inline">{csrf}'
                           f'<input type="hidden" name="allow" value="{live}"><button>'
                           f'{"Stop live" if u.live_allowed else "Allow live"}</button></form>')
            rows.append(f'<tr><td>{_e(u.email)}</td><td>{_e(u.status)}</td><td>{"live" if u.mode == "live" else "paper"}'
                        f'{" (live allowed)" if u.live_allowed else ""}</td><td>{_e(keys)}</td>'
                        f'<td>{n_open} open · {pnl:+,.2f} USDT</td><td>{_when(u.created)}</td>'
                        f'<td class="actions">{" ".join(actions)}</td></tr>')
        table = ("<table><tr><th>Email</th><th>Status</th><th>Mode</th><th>Keys</th><th>Results</th>"
                 f"<th>Joined</th><th></th></tr>{''.join(rows)}</table>" if rows else "<p class=muted>No users yet.</p>")
        note = f'<p class="good">{_e(msg)}</p>' if msg else ""
        info = ("<p class=small>Sign-up is <b>" + ("open" if signup else "closed") + "</b> "
                "(AITRADER_WEB_SIGNUP). New accounts wait for your approval. Disabling an account "
                "sells what the agent bought for it and stops it.</p>")
        log_rows = "".join(f'<tr><td>{_when(a["ts"])}</td><td>{_e(a["email"] or "-")}</td><td>{_e(a["actor"])}</td>'
                           f'<td class="{"bad" if a["action"].startswith("live_on") or a["action"] == "kill_switch" else ""}">'
                           f'{_e(a["action"].replace("_", " "))}</td><td class=small>{_e(a["detail"])}</td></tr>'
                           for a in users.audit(limit=40))
        audit = (f"<h2>Account log</h2><table><tr><th>When</th><th>Account</th><th>By</th><th>What</th><th></th></tr>"
                 f"{log_rows}</table>" if log_rows else "")
        return page("Users", note + info + table + audit, admin, token)

    def admin_post(request: Request, csrf: str, act) -> RedirectResponse | PlainTextResponse:
        admin, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        return RedirectResponse("/admin/users?msg=" + _q(act()), status_code=303)

    @app.post("/admin/users/{uid}/status")
    def admin_status(request: Request, uid: int, csrf: str = Form(""), status: str = Form("")):
        def act():
            u = users.get(uid)
            if u is None:
                return "No such user."
            if status not in ("active", "disabled", "pending"):
                return "Unknown status."
            users.set_status(uid, status)
            users.log(uid, "admin", status)
            if status == "disabled":
                turn_off(users, users_dir, uid, actor="admin", reason="account disabled", events=events())
            if status == "active":
                ctl = user_control(users_dir, uid)
                if ctl.state().get("by") == "platform":
                    ctl.resume(by="admin")  # undo the stop that disabling caused
            return f"{u.email} is now {status}."
        return admin_post(request, csrf, act)

    @app.post("/admin/users/{uid}/live")
    def admin_live(request: Request, uid: int, csrf: str = Form(""), allow: str = Form("off")):
        def act():
            u = users.get(uid)
            if u is None:
                return "No such user."
            if allow == "on":
                users.allow_live(uid, True)
                users.log(uid, "admin", "live_allowed")
            else:
                turn_off(users, users_dir, uid, actor="admin", reason="stopped by the admin", events=events())
                users.allow_live(uid, False)
                users.log(uid, "admin", "live_not_allowed")
            return f"Live trading {'allowed' if allow == 'on' else 'stopped'} for {u.email}."
        return admin_post(request, csrf, act)
