"""The dashboard: a small web app behind a login.

First feature: submit an e-book. The book is saved to ``data/books``, added to the knowledge
base, and read by the agent, which writes its strategies as rules and backtests them (the same
pipeline as ``aitrader learn read --test``). Strategies that pass go to paper trading. The
overview shows the mode and live lock, every strategy with its results, the AI trade filter,
and recent jobs.

The Trading page shows open positions (paper, testnet and live), the equity curve, the trade log
with the AI filter's probability, the event feed and the go/no-go gate, and has pause, resume
and kill-switch buttons. The kill switch only ever sells what the executors bought.

Nothing here can open a trade, approve going live, unlock live trading, or change a strategy's
status by hand.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from ..config import Settings
from ..safety import live_gate_problems
from .auth import COOKIE, Auth
from .jobs import JobRunner

MAX_UPLOAD = 50 * 1024 * 1024
TEXT_EXT = {".md", ".txt"}


@dataclass
class Paths:
    books: str = "data/books"
    registry: str = "data/learn/registry.json"
    models: str = "data/ai/models"
    paper_state: str = "data/learn/paper_state.json"
    kb_index: str = "data/kb/index.json"
    trade_dir: str = "data/trade"
    golive_dir: str = "data/golive"
    events: str = "data/ops/events.jsonl"
    users_dir: str = "data/users"
    keys_store: str = "data/settings/keys.enc"
    report: str = "data/reports/backtest.json"
    news: str = "data/news"


def create_app(settings: Settings, auth: Auth, jobs: JobRunner, paths: Paths | None = None,
               secure_cookie: bool = False, users=None, keys_secret: str = "", signup: bool = False,
               verifier=None, checkers: dict | None = None, live=None) -> FastAPI:
    """``users`` (an ``accounts.UserStore``) turns on user accounts: sign-up when ``signup`` is
    true, each user's own page, and the admin's Users page. Without it only the admin can log in."""
    paths = paths or Paths()
    app = FastAPI(title="aitrader dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                   "form-action 'self'; frame-ancestors 'none'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def session(request: Request) -> tuple[str | None, str | None]:
        token = request.cookies.get(COOKIE)
        return auth.verify(token), token

    def guard(request: Request):
        """Admin pages. A signed-in user account is sent to its own page instead."""
        user, token = session(request)
        if not user:
            return None, None, RedirectResponse("/login", status_code=303)
        if not auth.is_admin(user):
            return None, None, RedirectResponse("/me", status_code=303)
        return user, token, None

    def page(title: str, body: str, user: str | None = None, token: str | None = None,
             refresh: int | None = None, nav_links: str | None = None, head_extra: str = "") -> HTMLResponse:
        nav = ""
        if user:
            links = nav_links if nav_links is not None else "".join(
                f'<a href="{href}"{" class=active aria-current=page" if name == title else ""}>{name}</a>'
                for href, name in NAV if href != "/admin/users" or users is not None)
            nav = (f'<nav>{links}</nav><div class="who"><span class="muted small">{_e(user)}</span>'
                   f'<form method="post" action="/logout" class="inline">{_csrf(auth, token)}'
                   f'<button class="link">Log out</button></form></div>')
        meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        mode = settings.mode.value
        return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{_e(title)} · aitrader</title><style>{CSS}</style></head>
<body><header><a class="brand" href="/"><span class="logo" aria-hidden="true"></span>aitrader</a>
<span class="mode mode-{_e(mode)}">{_e(mode)} mode</span>{nav}</header>
<main><div class="page-head"><h1>{_e(title)}</h1>{head_extra}</div>{body}</main>
<footer class="muted small">aitrader · live trading stays locked until you open it on the server</footer></body></html>""")

    # ---- login ----------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.get("/login")
    def login_form(error: str = "", msg: str = ""):
        err = {"1": "Wrong email, user name or password.",
               "locked": "Too many wrong attempts. Try again in 15 minutes."}.get(error, "")
        note = {"signed_up": "Account created. You can log in now; the admin approves new accounts "
                             "before they can connect an exchange."}.get(msg, "")
        who = "Email (or admin user name)" if users is not None else "User"
        join = ('<p>New here? <a href="/signup">Create an account</a></p>'
                if users is not None and signup else "")
        return page("Log in", f"""{f'<p class="bad">{_e(err)}</p>' if err else ''}{f'<p class="good">{_e(note)}</p>' if note else ''}
<form method="post" action="/login" class="card narrow">
<label>{who} <input name="user" autocomplete="username" required></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
<button>Log in</button></form>{join}""")

    @app.post("/login")
    def login(request: Request, user: str = Form(...), password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if auth.locked_out(ip):
            return RedirectResponse("/login?error=locked", status_code=303)
        token, dest = None, "/"
        if "@" in user and users is not None:
            account = users.authenticate(user, password)
            if account is None:
                auth.record_failure(ip)
            else:
                token, dest = auth.issue(ip, account.subject), "/me"
        else:
            token = auth.login(ip, user, password)
        if not token:
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse(dest, status_code=303)
        resp.set_cookie(COOKIE, token, max_age=int(auth.session_s), httponly=True,
                        samesite="strict", secure=secure_cookie)
        return resp

    from .market_pages import add_market_routes
    from .news_pages import add_news_routes
    from .report_pages import add_report_routes
    from .settings_pages import add_settings_routes

    add_market_routes(app, settings=settings, page=page, guard=guard, live=live)
    add_report_routes(app, settings=settings, auth=auth, jobs=jobs, page=page, guard=guard,
                      report_path=paths.report)
    add_news_routes(app, settings=settings, auth=auth, jobs=jobs, page=page, guard=guard, news_dir=paths.news)

    add_settings_routes(app, settings=settings, auth=auth, paths=paths, jobs=jobs,
                        keys_secret=keys_secret, page=page, guard=guard, checkers=checkers)

    if users is not None:
        from .user_pages import add_user_routes

        add_user_routes(app, settings=settings, auth=auth, paths=paths, users=users,
                        keys_secret=keys_secret, signup=signup, verifier=verifier, page=page,
                        session=session, guard=guard)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        _user, token = session(request)
        resp = RedirectResponse("/login", status_code=303)
        if auth.check_csrf(token, csrf):
            auth.revoke(token)
            resp.delete_cookie(COOKIE)
        return resp

    # ---- overview -------------------------------------------------------------------------------

    @app.get("/")
    def overview(request: Request):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        problems = live_gate_problems(settings)
        live = ('<span class="good">locked</span> (paper only)' if problems
                else '<span class="bad">UNLOCKED</span>')
        body = [f'<div class="grid"><div class="card"><h3>Trading</h3><p>Mode <b>{_e(settings.mode.value)}</b>'
                f'<br>Live orders: {live}</p></div>',
                _filter_card(paths), _paper_card(paths), "</div>",
                "<h2>Strategies</h2>", _strategy_table(paths),
                "<h2>Recent jobs</h2>", _jobs_table(jobs.list(10))]
        return page("Overview", "".join(body), user, token)

    @app.get("/strategies/{sid}")
    def strategy(request: Request, sid: str):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        from ..learn import Registry

        e = Registry(paths.registry).entries.get(sid)
        if e is None:
            return page("Not found", "<p>No such strategy.</p>", user, token)
        info = {"status": e.status, "source": e.source, "spec": e.spec, "backtest": e.backtest,
                "paper": e.paper, "history": e.history}
        return page(e.id, f"<pre>{_e(json.dumps(info, indent=2, default=str))}</pre>", user, token)

    # ---- books ----------------------------------------------------------------------------------

    @app.get("/books")
    def books_form(request: Request, error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        warn = "" if has_key else (
            '<p class="bad">No Anthropic API key yet, so the agent can\'t read books. Add it on the '
            '<a href="/settings">Settings</a> page. Uploads are still added to the knowledge base.</p>')
        err = f'<p class="bad">{_e(error)}</p>' if error else ""
        body = f"""{err}{warn}
<form method="post" action="/books" enctype="multipart/form-data" class="card">
{_csrf(auth, token)}
<p>Upload a trading book (PDF, Markdown or text, up to {MAX_UPLOAD // 2**20} MB). The agent
reads it in full, writes each concrete strategy as checked rules (long spot only, stop required,
risk capped at 1%), then backtests them with fees and slippage. Strategies that pass move to
paper trading. Nothing trades real money.</p>
<label>Book <input type="file" name="book" accept=".pdf,.md,.txt,application/pdf,text/plain" required></label>
<label>Most strategies to keep <input type="number" name="max" value="12" min="1" max="30"></label>
<label class="row"><input type="checkbox" name="test" value="1" checked> Backtest the new strategies straight away</label>
<button>Submit book</button></form>
<h2>Book jobs</h2>{_jobs_table(jobs.list())}"""
        return page("Submit a book", body, user, token)

    @app.post("/books")
    async def books_submit(request: Request, book: UploadFile = File(...), csrf: str = Form(""),
                           max: int = Form(12), test: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        try:
            path = await _save_upload(book, paths.books)
        except ValueError as exc:
            return RedirectResponse(f"/books?error={_q(str(exc))}", status_code=303)
        steps = [["kb", "build", "--index", paths.kb_index]]
        if os.environ.get("ANTHROPIC_API_KEY"):
            read = ["learn", "--registry", paths.registry, "read", path,
                    "--max", str(min(max_val(max), 30))]
            if test:
                read.append("--test")
            steps.append(read)
        job_id = jobs.submit(f"Book: {book.filename or os.path.basename(path)}", steps,
                             {"book": path, "by": user})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}")
    def job(request: Request, job_id: str):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        try:
            j = jobs.get(job_id)
        except (KeyError, OSError):
            return page("Not found", "<p>No such job.</p>", user, token)
        running = j["status"] in ("queued", "running")
        body = (f'<p>Status <b class="{_status_class(j["status"])}">{_e(j["status"])}</b>'
                f'{" · " + _e(j["error"]) if j["error"] else ""}'
                f'{" · this page refreshes every 5 seconds" if running else ""}</p>'
                f'<p>Started {_when(j["started"])}, finished {_when(j["finished"])}</p>'
                f'<pre class="log">{_e(jobs.log_text(job_id)) or "(no output yet)"}</pre>'
                '<p><a href="/">Back to the overview</a> to see the strategies and their results.</p>')
        return page(j["title"], body, user, token, refresh=5 if running else None)

    # ---- trading (Phases 5 and 6) ---------------------------------------------------------------

    def _control():
        from ..trade import Control

        return Control(os.path.join(paths.trade_dir, "control.json"))

    @app.get("/trading")
    def trading(request: Request, msg: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        from ..ops import EventLog

        c = _control().state()
        paper = _load_json(paths.paper_state)
        execs = {m: _load_json(os.path.join(paths.trade_dir, f"{m}_state.json")) for m in ("testnet", "live")}
        if c["killed"]:
            state = f'<b class="bad">KILL SWITCH ON</b> since {_when(c.get("since"))}'
        elif c["paused"]:
            state = f'<b class="bad">PAUSED</b> since {_when(c.get("since"))}: {_e(c.get("reason", ""))}'
        else:
            state = '<b class="good">running</b>'
        live = ('<span class="good">locked</span>' if live_gate_problems(settings)
                else '<span class="bad">UNLOCKED</span>')
        csrf = _csrf(auth, token)
        controls = (f'<div class="card"><h3>Controls</h3><p>Trading {state}<br>Mode <b>{_e(settings.mode.value)}</b>'
                    f' · live orders {live}</p><div class="row">'
                    f'<form method="post" action="/trading/pause">{csrf}<button>Pause new entries</button></form>'
                    f'<form method="post" action="/trading/resume">{csrf}<button class="ghost">Resume</button></form>'
                    f'</div><form method="post" action="/trading/kill" class="kill">{csrf}'
                    '<label>Kill switch: stops entries and sells everything the testnet/live executors bought. '
                    'Type KILL to confirm <input name="confirm" autocomplete="off" placeholder="KILL"></label>'
                    '<button class="danger">Pull the kill switch</button></form></div>')
        note = f'<p class="good">{_e(msg)}</p>' if msg else ""
        events = EventLog(paths.events).read(limit=600)
        body = [note, _trading_stats(c, settings, paths, paper, events), _setup_hint(paths),
                '<div class="grid">', controls, _gate_card(paths, csrf), "</div>",
                "<h2>Equity</h2>", _equity_svg(paper, execs),
                "<h2>Open positions</h2>", _positions_table(paper, execs),
                "<h2>Trade log</h2>", _trades_table(paper, execs),
                "<h2>Events</h2>", _events_table(events)]
        return page("Trading", "".join(body), user, token, refresh=60)

    def _control_post(request: Request, csrf: str, action) -> Response:
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        return RedirectResponse(f"/trading?msg={_q(action(user))}", status_code=303)

    def _event(kind: str, text: str, level: str = "info") -> None:
        from ..ops import EventLog

        EventLog(paths.events, service="dashboard").emit(kind, text, level=level)

    @app.post("/trading/pause")
    def trading_pause(request: Request, csrf: str = Form("")):
        def act(user):
            _control().pause(f"paused from the dashboard by {user}", by=user)
            _event("risk_pause", f"paused from the dashboard by {user}", "warning")
            return "Paused: no new entries. Open positions are still managed."
        return _control_post(request, csrf, act)

    @app.post("/trading/resume")
    def trading_resume(request: Request, csrf: str = Form("")):
        def act(user):
            _control().resume(by=user)
            _event("resume", f"resumed from the dashboard by {user}")
            return "Resumed."
        return _control_post(request, csrf, act)

    @app.post("/trading/kill")
    def trading_kill(request: Request, csrf: str = Form(""), confirm: str = Form("")):
        def act(user):
            if confirm.strip() != "KILL":
                return "Not pulled: type KILL in the box to confirm."
            _control().kill(f"kill switch from the dashboard by {user}", by=user)
            _event("kill", f"kill switch pulled from the dashboard by {user}", "warning")
            return "Kill switch on: entries stopped, executors are selling. Resume when ready."
        return _control_post(request, csrf, act)

    @app.post("/trading/gate")
    def trading_gate(request: Request, csrf: str = Form("")):
        def act(_user):
            from ..trade import GateConfig, GoLive
            from ..ops import EventLog

            rep = GoLive(paths.golive_dir, paths.registry, paths.paper_state, paths.models,
                         events=EventLog(paths.events, service="dashboard"), cfg=GateConfig.from_env()).evaluate()
            return "Gate checked: " + ("PASSES" if rep["passed"] else "does not pass yet")
        return _control_post(request, csrf, act)

    return app


NAV = [("/", "Overview"), ("/markets", "Markets"), ("/trading", "Trading"), ("/news", "News"),
       ("/reports", "Reports"), ("/books", "Submit a book"), ("/admin/users", "Users"), ("/settings", "Settings")]


def max_val(v) -> int:
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 12


async def _save_upload(upload: UploadFile, folder: str) -> str:
    """Save an upload under a generated name. Accepts PDFs (by content) and UTF-8 text."""
    name = os.path.basename(upload.filename or "book")
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:80] or "book"
    head = await upload.read(5)
    is_pdf = head == b"%PDF-"
    if not is_pdf and ext.lower() not in TEXT_EXT:
        raise ValueError("Upload a PDF, .md or .txt file.")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(folder, f"{stamp}-{uuid.uuid4().hex[:8]}-{stem}{'.pdf' if is_pdf else ext.lower()}")
    size = len(head)
    with open(path, "wb") as fh:
        fh.write(head)
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                os.remove(path)
                raise ValueError(f"The file is larger than {MAX_UPLOAD // 2**20} MB.")
            fh.write(chunk)
    if not is_pdf:
        try:
            with open(path, encoding="utf-8") as fh:
                fh.read()
        except UnicodeDecodeError:
            os.remove(path)
            raise ValueError("That text file isn't UTF-8 text.") from None
    if size < 100:
        os.remove(path)
        raise ValueError("The file is empty or too small to be a book.")
    return path


# ---- page pieces --------------------------------------------------------------------------------


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s)


def _csrf(auth: Auth, token: str | None) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(auth.csrf(token or ""))}">'


def _when(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _status_class(status: str) -> str:
    return {"done": "good", "paper": "good", "active": "good", "failed": "bad",
            "rejected": "bad", "retired": "muted", "archived": "muted"}.get(status, "")


def _jobs_table(jobs: list[dict]) -> str:
    if not jobs:
        return "<p class=muted>No jobs yet.</p>"
    rows = "".join(f'<tr><td><a href="/jobs/{_e(j["id"])}">{_e(j["title"])}</a></td>'
                   f'<td class="{_status_class(j["status"])}">{_e(j["status"])}</td>'
                   f'<td>{_when(j["created"])}</td><td>{_e(j["meta"].get("by", ""))}</td></tr>'
                   for j in jobs)
    return f"<table><tr><th>Job</th><th>Status</th><th>Submitted</th><th>By</th></tr>{rows}</table>"


def _strategy_table(paths: Paths) -> str:
    from ..learn import Registry

    reg = Registry(paths.registry)
    if not reg.entries:
        return "<p class=muted>No strategies yet. Start the first-time setup on the <a href=\"/settings\">Settings</a> page, or submit a book.</p>"
    order = {"paper": 0, "candidate": 1, "retired": 2, "rejected": 3}
    rows = []
    for e in sorted(reg.entries.values(), key=lambda e: (order.get(e.status, 9), e.id)):
        bt = e.backtest.get("out_of_sample", {})
        pp = e.paper or {}
        why = e.history[-1]["why"] if e.history else ""
        rows.append(
            f'<tr><td><a href="/strategies/{_e(e.id)}">{_e(e.id)}</a><div class="muted small">{_e(e.source[:70])}</div></td>'
            f'<td class="{_status_class(e.status)}">{_e(e.status)}</td>'
            f'<td>{_result(bt)}</td><td>{_result(pp)}</td>'
            f'<td class="small">{_e(why[:140])}</td></tr>')
    return ("<table><tr><th>Strategy</th><th>Status</th><th>Backtest (out of sample)</th>"
            f"<th>Paper</th><th>Last change</th></tr>{''.join(rows)}</table>")


def _result(m: dict) -> str:
    if not m:
        return '<span class="muted">not yet</span>'
    return f'{m.get("trades", 0)} trades · {m.get("expectancy_r", 0):+.3f}R'


def _filter_card(paths: Paths) -> str:
    from ..ai import ModelRegistry

    reg = ModelRegistry(paths.models)
    if reg.active:
        m = reg.info(reg.active)
        f = m["test"]["filtered"]
        text = (f"Active model <b>{_e(reg.active)}</b><br>test: {f['trades']} trades, "
                f"{f['expectancy_r']:+.3f}R each")
    else:
        text = "Off: every signal is taken."
    waiting = [v for v in reg.versions() if reg.info(v)["status"] == "candidate"]
    if waiting:
        text += f"<br>{len(waiting)} candidate(s) waiting for approval (<code>aitrader ai promote</code>)"
    return f'<div class="card"><h3>AI trade filter</h3><p>{text}</p></div>'


def _paper_card(paths: Paths) -> str:
    if not os.path.exists(paths.paper_state):
        return '<div class="card"><h3>Paper trading</h3><p class=muted>Not started.</p></div>'
    try:
        with open(paths.paper_state, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return '<div class="card"><h3>Paper trading</h3><p class=muted>State unreadable.</p></div>'
    trades = st.get("trades", [])
    total = sum(t.get("pnl_usdt", 0) for t in trades)
    week = time.time() * 1000 - 7 * 86_400_000
    recent = [t for t in trades if t.get("exit_ts", 0) >= week]
    return (f'<div class="card"><h3>Paper trading</h3><p>{len(st.get("open", {}))} open · '
            f'{len(trades)} closed<br>P&amp;L {total:+,.2f} USDT · last 7 days {len(recent)} trades, '
            f'{sum(t.get("r", 0) for t in recent):+.2f}R<br>'
            f'{len(st.get("filtered", []))} signals skipped by the AI filter</p></div>')


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _ms(ts) -> str:
    return _when(ts / 1000) if ts else "-"


def _positions_table(paper: dict, execs: dict) -> str:
    rows = []
    for key, p in paper.get("open", {}).items():
        sig = p.get("signal", {})
        entry_id, ex, sym, tf = (key.split("|") + ["", "", "", ""])[:4]
        mark = p.get("mark_r")
        rows.append(f"<tr><td>paper</td><td>{_e(sym)} <span class=muted>{_e(ex)} {_e(tf)}</span></td>"
                    f"<td>{_e(entry_id)}</td><td>{p.get('entry', sig.get('entry', 0)):.6g}"
                    f"{'' if p.get('filled') else ' <span class=muted>(waiting)</span>'}</td>"
                    f"<td>{sig.get('stop', 0):.6g}</td><td>{'' if mark is None else f'{mark:+.2f}R'}</td>"
                    f"<td>{_prob(p.get('ai_prob'))}</td></tr>")
    for mode, st in execs.items():
        for p in st.get("positions", {}).values():
            cls = "" if p.get("status") == "open" else "bad"
            rows.append(f"<tr><td>{_e(mode)}</td><td>{_e(p['symbol'])} <span class=muted>{_e(p.get('amount', 0))}</span></td>"
                        f"<td>{_e(p['entry_id'])}</td><td>{p.get('entry') or 0:.6g}</td><td>{p['stop']:.6g}</td>"
                        f'<td class="{cls}">{_e(p.get("status"))}</td><td>{_prob(p.get("ai_prob"))}</td></tr>')
    if not rows:
        return "<p class=muted>No open positions.</p>"
    return ("<table><tr><th>Where</th><th>Market</th><th>Strategy</th><th>Entry</th><th>Stop</th>"
            f"<th>Now</th><th>AI p</th></tr>{''.join(rows)}</table>")


def _prob(p) -> str:
    return "-" if p is None else f"{p:.2f}"


def _trades_table(paper: dict, execs: dict, limit: int = 40) -> str:
    rows = [("paper", t) for t in paper.get("trades", [])]
    for mode, st in execs.items():
        rows += [(mode, t) for t in st.get("closed", [])]
    rows.sort(key=lambda x: x[1].get("exit_ts", 0), reverse=True)
    if not rows:
        return "<p class=muted>No closed trades yet.</p>"
    out = []
    for mode, t in rows[:limit]:
        r = t.get("r", 0)
        twin = f" (paper {t['paper_r']:+.2f}R)" if t.get("paper_r") is not None else ""
        out.append(f"<tr><td>{_ms(t.get('exit_ts'))}</td><td>{_e(mode)}</td><td>{_e(t.get('symbol', ''))}</td>"
                   f"<td>{_e(t.get('entry_id', ''))}</td><td>{t.get('entry', 0):.6g} → {t.get('exit', 0):.6g}</td>"
                   f'<td class="{"good" if r > 0 else "bad"}">{r:+.2f}R{_e(twin)}</td>'
                   f"<td>{t.get('pnl_usdt', 0):+,.2f}</td><td>{_prob(t.get('ai_prob'))}</td>"
                   f"<td class=small>{_e(t.get('exit_reason', ''))}</td></tr>")
    return ("<table><tr><th>Closed</th><th>Where</th><th>Market</th><th>Strategy</th><th>Price</th>"
            f"<th>Result</th><th>USDT</th><th>AI p</th><th>Exit</th></tr>{''.join(out)}</table>")


def _events_table(events: list[dict], limit: int = 30) -> str:
    """Newest first; repeats of the same message in a row are shown once with a count."""
    groups: list[dict] = []
    for e in events:
        if e.get("kind") == "heartbeat":
            continue
        key = (e.get("service"), e.get("kind"), e.get("text"))
        if groups and groups[-1]["key"] == key:
            groups[-1]["n"] += 1
            groups[-1]["ts"] = e.get("ts")
        else:
            groups.append({"key": key, "n": 1, "ts": e.get("ts"), "first": e.get("ts"), "level": e.get("level")})
    groups = groups[-limit:]
    if not groups:
        return "<p class=muted>No events yet.</p>"
    rows = []
    for g in reversed(groups):
        service, kind, text = g["key"]
        lvl = g["level"] if g["level"] in ("error", "warning") else ""
        badge = {"error": "bad", "warning": "warn"}.get(lvl, "")
        times = f' <span class="badge">×{g["n"]}</span><div class="muted small">since {_ms(g["first"])}</div>' \
            if g["n"] > 1 else ""
        rows.append(f'<tr><td class=small>{_ms(g["ts"])}{times}</td><td>{_e(service or "")}</td>'
                    f'<td><span class="badge {badge}">{_e(kind or "")}</span></td>'
                    f'<td class=small>{_e(str(text or "")[:240])}</td></tr>')
    return f"<table><tr><th>When</th><th>Service</th><th>Kind</th><th>What</th></tr>{''.join(rows)}</table>"


def _service_state(events: list[dict], service: str, now_ms: int) -> tuple[str, str, str]:
    """(label, css class, detail) for a service, from its latest events."""
    mine = [e for e in events if e.get("service") == service]
    if not mine:
        return "not running", "", "no events yet"
    last = mine[-1]
    beats = [e for e in mine if e.get("kind") == "heartbeat"]
    fresh = beats and now_ms - beats[-1].get("ts", 0) < 15 * 60_000
    if last.get("kind") == "waiting":
        return "waiting for keys", "warn", "add the testnet keys on the Settings page"
    if last.get("level") == "error":
        return "error", "bad", str(last.get("text", ""))[:120]
    if fresh:
        return "running", "good", f"last check-in {_ms(beats[-1].get('ts'))}"
    return "quiet", "warn", f"last seen {_ms(last.get('ts'))}"


def _trading_stats(c: dict, settings, paths: Paths, paper: dict, events: list[dict]) -> str:
    from ..learn import Registry

    now = int(time.time() * 1000)
    if c["killed"]:
        trading = ("Kill switch on", "bad")
    elif c["paused"]:
        trading = ("Paused", "warn")
    else:
        trading = ("Running", "good")
    tn, tn_cls, tn_detail = _service_state(events, "executor-testnet", now)
    trades = paper.get("trades", [])
    pnl = sum(t.get("pnl_usdt", 0) for t in trades)
    try:
        in_paper = sum(1 for e in Registry(paths.registry).entries.values() if e.status == "paper")
    except Exception:
        in_paper = 0
    tiles = [
        ("Trading", trading[0], trading[1], "new entries" if not c["paused"] and not c["killed"] else ""),
        ("Mode", settings.mode.value, "", "live orders locked" if live_gate_problems(settings) else "LIVE UNLOCKED"),
        ("Testnet trader", tn, tn_cls, tn_detail),
        ("Strategies trading", str(in_paper), "good" if in_paper else "warn", "in paper trading"),
        ("Paper P&L", f"{pnl:+,.2f}", "good" if pnl > 0 else "bad" if pnl < 0 else "",
         f"{len(paper.get('open', {}))} open · {len(trades)} closed"),
    ]
    return '<div class="stats">' + "".join(
        f'<div class="stat"><span class="k">{_e(k)}</span><span class="v {cls}">{_e(v)}</span>'
        f'<span class="s">{_e(sub)}</span></div>' for k, v, cls, sub in tiles) + "</div>"


def _setup_hint(paths: Paths) -> str:
    from ..learn import Registry

    try:
        if any(e.status == "paper" for e in Registry(paths.registry).entries.values()):
            return ""
    except Exception:
        pass
    return ('<p class="warn">No strategy is paper trading yet, so there is nothing to trade. Run the '
            '<b>first-time setup</b> on the <a href="/settings">Settings</a> page: it downloads price '
            'history and tests the strategy library; the ones that pass start paper trading.</p>')


def _equity_svg(paper: dict, execs: dict) -> str:
    """Inline SVG line chart: paper P&L (cumulative, from closed trades) and executor equity."""
    series = []
    pts, total = [], 0.0
    for t in sorted(paper.get("trades", []), key=lambda t: t.get("exit_ts", 0)):
        total += t.get("pnl_usdt", 0)
        pts.append((t.get("exit_ts", 0), total))
    if pts:
        series.append(("paper P&L (USDT)", pts, "var(--accent)"))
    colors = {"testnet": "var(--good)", "live": "var(--bad)"}
    for mode, st in execs.items():
        curve = [(a, b) for a, b in st.get("equity_curve", [])]
        if len(curve) > 1:
            series.append((f"{mode} equity (USDT)", curve, colors[mode]))
    if not series:
        return "<p class=muted>No equity history yet.</p>"
    w, h, pad = 900, 220, 36
    parts = []
    for name, pts, color in series:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = min(xs), max(xs) or 1
        y0, y1 = min(ys + [0.0]) if "P&L" in name else min(ys), max(ys + [0.0]) if "P&L" in name else max(ys)
        if x1 == x0:
            x1 = x0 + 1
        if y1 == y0:
            y1 = y0 + 1
        line = " ".join(f"{pad + (x - x0) / (x1 - x0) * (w - 2 * pad):.1f},"
                        f"{h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad):.1f}" for x, y in pts)
        parts.append(f'<div class="card chart"><h3>{_e(name)}: {ys[-1]:{"+" if "P&L" in name else ""},.2f}</h3>'
                     f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_e(name)}" preserveAspectRatio="none">'
                     f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="var(--line)"/>'
                     f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{line}"/>'
                     f'<text x="{pad}" y="{h - 10}" fill="var(--muted)" font-size="12">{_e(_ms(x0))}</text>'
                     f'<text x="{w - pad}" y="{h - 10}" fill="var(--muted)" font-size="12" text-anchor="end">{_e(_ms(x1))}</text>'
                     f'<text x="4" y="{pad - 8}" fill="var(--muted)" font-size="12">{y1:,.0f}</text>'
                     f'<text x="4" y="{h - pad}" fill="var(--muted)" font-size="12">{y0:,.0f}</text></svg></div>')
    return "".join(parts)


def _gate_card(paths: Paths, csrf: str) -> str:
    rep = _load_json(os.path.join(paths.golive_dir, "report.json"))
    approval = _load_json(os.path.join(paths.golive_dir, "approval.json"))
    if rep:
        rows = "".join(f'<li class="{"good" if c["passed"] else "bad"}">{"✓" if c["passed"] else "✗"} '
                       f'{_e(c["name"])}: {_e(c["value"])}</li>' for c in rep.get("criteria", []))
        head = (f'<b class="{"good" if rep.get("passed") else "bad"}">'
                f'{"passes" if rep.get("passed") else "does not pass yet"}</b> (checked {_when(rep.get("at"))})')
    else:
        rows, head = "", "not checked yet"
    appr = (f'<br>Approved by {_e(approval["by"])} for {_e(", ".join(approval.get("strategies", {})))}'
            if approval.get("by") else "<br>No live approval (approve on the server: <code>aitrader golive approve</code>)")
    return (f'<div class="card"><h3>Go/no-go gate (Phase 6)</h3><p>{head}{appr}</p><ul class="small gate">{rows}</ul>'
            f'<form method="post" action="/trading/gate">{csrf}<button class="ghost">Check the gate now</button></form></div>')


CSS = """
:root{--bg:#0b0e13;--bg2:#0f131a;--fg:#e6e9ef;--muted:#8a93a3;--card:#141922;--card2:#1a2030;--line:#252c3a;
--good:#16c784;--bad:#ea3943;--warnc:#f0b90b;--accent:#4c8dff;--accent2:#2f6fe4;--radius:12px;--shadow:0 1px 2px rgba(0,0,0,.3)}
@media (prefers-color-scheme:light){:root{--bg:#f4f6f9;--bg2:#eef1f5;--fg:#141821;--muted:#5f6b7c;--card:#fff;--card2:#f7f9fc;
--line:#e1e6ee;--good:#0e9f6e;--bad:#d92d3a;--warnc:#b7791f;--accent:#2563eb;--accent2:#1d4ed8;--shadow:0 1px 3px rgba(16,24,40,.08)}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
font-feature-settings:"tnum" 1;min-height:100vh;display:flex;flex-direction:column}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;display:flex;gap:10px 18px;align-items:center;flex-wrap:wrap;padding:10px 20px;
background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:1.08em;color:var(--fg);letter-spacing:.2px}
.brand:hover{text-decoration:none}
.logo{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--accent),var(--good));
clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%)}
.mode{font-size:.72em;font-weight:600;text-transform:uppercase;letter-spacing:.6px;padding:3px 9px;border-radius:99px;
border:1px solid var(--line);color:var(--muted)}
.mode-paper{color:var(--warnc);border-color:color-mix(in srgb,var(--warnc) 45%,transparent)}
.mode-testnet{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,transparent)}
.mode-live{color:#fff;background:var(--bad);border-color:var(--bad)}
nav{display:flex;gap:2px;flex-wrap:wrap;margin-left:auto}
nav a{color:var(--muted);padding:6px 11px;border-radius:8px;font-weight:500;font-size:.94em}
nav a:hover{color:var(--fg);background:var(--card2);text-decoration:none}
nav a.active{color:var(--fg);background:var(--card2);box-shadow:inset 0 -2px 0 var(--accent)}
.who{display:flex;gap:10px;align-items:center}
main{width:100%;max-width:1200px;margin:0 auto;padding:20px;flex:1}
footer{text-align:center;padding:18px;border-top:1px solid var(--line)}
.page-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 16px}
h1{font-size:1.5em;margin:0;letter-spacing:-.2px}h2{font-size:1.12em;margin:28px 0 10px}h3{margin:0 0 8px;font-size:.98em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px;box-shadow:var(--shadow)}
.narrow{max-width:400px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px}
.stat .k{display:block;color:var(--muted);font-size:.78em;text-transform:uppercase;letter-spacing:.5px}
.stat .v{display:block;font-size:1.25em;font-weight:650;margin-top:2px}
.stat .s{display:block;font-size:.82em;color:var(--muted)}
label{display:block;margin:10px 0;font-size:.94em}label.row{display:flex;gap:8px;align-items:center}
input[type=text],input[type=password],input:not([type]),input[type=number],input[type=email],input[type=file],select{
display:block;width:100%;max-width:420px;margin-top:4px;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
background:var(--bg2);color:var(--fg);font:inherit}
input:focus,select:focus{outline:2px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:1px;border-color:var(--accent)}
button{padding:8px 16px;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:8px;cursor:pointer;
font:inherit;font-weight:600;font-size:.92em}
button:hover{background:var(--accent2)}button.ghost{background:transparent;color:var(--accent)}
button.ghost:hover{background:color-mix(in srgb,var(--accent) 12%,transparent)}
button.danger{background:var(--bad);border-color:var(--bad)}button.danger:hover{filter:brightness(1.1)}
button.link,form.inline button{background:none;border:none;color:var(--accent);padding:0;font-weight:500}
form.inline{display:inline}
table{width:100%;border-collapse:collapse;display:block;overflow-x:auto;background:var(--card);border:1px solid var(--line);
border-radius:var(--radius);font-size:.92em}table>tbody{display:table;width:100%}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.8em;text-transform:uppercase;letter-spacing:.4px;background:var(--card2)}
tr:last-child td{border-bottom:none}
pre{background:var(--card);border:1px solid var(--line);padding:14px;border-radius:var(--radius);overflow-x:auto;
white-space:pre-wrap;word-break:break-word}
code{background:var(--card2);border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:.9em}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}form.row{align-items:center}
.row select{width:auto;margin-top:0}form.inline{display:inline;margin:0}
.chips{display:flex;flex-wrap:wrap;gap:8px}.tick-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 6px 4px 12px;
border:1px solid var(--line);border-radius:99px;font-size:.9em}button.small{padding:2px 8px;font-size:.8em}
.posts{list-style:none;padding:0}.posts li{padding:10px 0;border-bottom:1px solid var(--line)}.row>label{margin:0}
form.kill{margin-top:14px;border-top:1px solid var(--line);padding-top:10px}ul.gate{padding-left:0;list-style:none;margin:8px 0}
.chart svg{width:100%;height:180px;display:block}.chart svg.candles{height:auto}
.switch{font-size:1.1em}.switch.on b{color:var(--bad)}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warnc)}.stack{margin:12px 0}.muted{color:var(--muted)}
.small{font-size:.85em}.pill{border:1px solid var(--line);border-radius:99px;padding:1px 8px;font-size:.85em}
.badge{display:inline-block;white-space:nowrap;font-size:.75em;font-weight:600;padding:2px 8px;border-radius:99px;
background:var(--card2);border:1px solid var(--line);color:var(--muted)}
.badge.good{color:var(--good);border-color:color-mix(in srgb,var(--good) 40%,transparent)}
.badge.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
.badge.warn{color:var(--warnc);border-color:color-mix(in srgb,var(--warnc) 40%,transparent)}
p.good,p.bad,p.warn{padding:10px 14px;border-radius:10px;border:1px solid;background:var(--card)}
p.good{border-color:color-mix(in srgb,var(--good) 40%,transparent)}p.bad{border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
p.warn{border-color:color-mix(in srgb,var(--warnc) 40%,transparent)}
/* markets */
.live-status{display:inline-flex;align-items:center;gap:6px;font-size:.85em;color:var(--muted)}
.live-status .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.live-status.on .dot{background:var(--good);box-shadow:0 0 0 0 var(--good);animation:pulse 1.6s infinite}
.live-status.off .dot{background:var(--warnc)}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--good) 60%,transparent)}100%{box-shadow:0 0 0 8px transparent}}
.tickers{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tick{display:grid;gap:1px;text-align:left;background:var(--card);border:1px solid var(--line);color:var(--fg);
padding:10px 12px;border-radius:var(--radius);font-weight:400;transition:border-color .2s,background .4s}
.tick:hover{background:var(--card2);border-color:var(--accent)}.tick.active{border-color:var(--accent);background:var(--card2)}
.tick-sym{font-size:.8em;color:var(--muted);font-weight:600}.tick-px{font-size:1.12em;font-weight:650}
.tick-ch{font-size:.85em;font-weight:600}.tick-vol{font-size:.75em}
.tick.up{background:color-mix(in srgb,var(--good) 14%,var(--card))}.tick.down{background:color-mix(in srgb,var(--bad) 14%,var(--card))}
.chart-card{padding:14px 14px 10px}.chart-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.chart-head h2{margin:0;font-size:1.1em}
.segs{display:inline-flex;margin-left:auto;background:var(--bg2);border:1px solid var(--line);border-radius:9px;padding:2px}
.seg{background:none;border:none;color:var(--muted);padding:5px 10px;border-radius:7px;font-weight:600;font-size:.85em}
.seg:hover{background:var(--card2);color:var(--fg)}.seg.active{background:var(--accent);color:#fff}
.ohlc{min-height:1.6em;margin:8px 0 6px;display:flex;flex-wrap:wrap;gap:4px 8px}
#chart{display:block;width:100%;height:440px;touch-action:pan-y}
#market:not(.js) .js-only{display:none}
details.health summary{cursor:pointer;font-weight:600}details.health table{margin-top:12px}
@media (max-width:640px){header{padding:8px 12px}main{padding:14px 12px}nav{margin-left:0;width:100%}
nav a{white-space:nowrap}.who{margin-left:auto}#chart{height:340px}.segs{margin-left:0}h1{font-size:1.3em}}
"""
