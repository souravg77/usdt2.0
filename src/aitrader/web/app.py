"""The dashboard: a small web app behind a login.

First feature: submit an e-book. The book is saved to ``data/books``, added to the knowledge
base, and read by the agent, which writes its strategies as rules and backtests them (the same
pipeline as ``aitrader learn read --test``). Strategies that pass go to paper trading. The
overview shows the mode and live lock, every strategy with its results, the AI trade filter,
and recent jobs.

Nothing here can place an order, unlock live trading, or change a strategy's status by hand.
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


def create_app(settings: Settings, auth: Auth, jobs: JobRunner, paths: Paths | None = None,
               secure_cookie: bool = False) -> FastAPI:
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
        user, token = session(request)
        if not user:
            return None, None, RedirectResponse("/login", status_code=303)
        return user, token, None

    def page(title: str, body: str, user: str | None = None, token: str | None = None,
             refresh: int | None = None) -> HTMLResponse:
        nav = ""
        if user:
            nav = (f'<nav><a href="/">Overview</a><a href="/books">Submit a book</a>'
                   f'<form method="post" action="/logout" class="inline">{_csrf(auth, token)}'
                   f'<button>Log out {_e(user)}</button></form></nav>')
        meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{_e(title)} · aitrader</title><style>{CSS}</style></head>
<body><header><b>aitrader</b> <span class="pill">{_e(settings.mode.value)} mode</span>{nav}</header>
<main><h1>{_e(title)}</h1>{body}</main></body></html>""")

    # ---- login ----------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.get("/login")
    def login_form(error: str = ""):
        msg = {"1": "Wrong user name or password.",
               "locked": "Too many wrong attempts. Try again in 15 minutes."}.get(error, "")
        return page("Log in", f"""{f'<p class="bad">{_e(msg)}</p>' if msg else ''}
<form method="post" action="/login" class="card narrow">
<label>User <input name="user" autocomplete="username" required></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
<button>Log in</button></form>""")

    @app.post("/login")
    def login(request: Request, user: str = Form(...), password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if auth.locked_out(ip):
            return RedirectResponse("/login?error=locked", status_code=303)
        token = auth.login(ip, user, password)
        if not token:
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=int(auth.session_s), httponly=True,
                        samesite="strict", secure=secure_cookie)
        return resp

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
            '<p class="bad">ANTHROPIC_API_KEY is not set in .env, so the agent can\'t read books '
            "yet. Uploads are still added to the knowledge base.</p>")
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

    return app


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
        return "<p class=muted>No strategies yet. Submit a book, or run <code>aitrader learn add-library --test</code>.</p>"
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


CSS = """
:root{--bg:#f7f7f5;--fg:#1c1c1c;--muted:#6b6b6b;--card:#fff;--line:#e2e2de;--good:#1f7a3a;--bad:#b3261e;--accent:#2457c5}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececec;--muted:#9a9a9a;--card:#1e1e1e;--line:#333;--good:#5cc77a;--bad:#ff7b72;--accent:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card)}
nav{display:flex;gap:14px;align-items:center;margin-left:auto;flex-wrap:wrap}a{color:var(--accent)}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:1.4em}h2{font-size:1.15em;margin-top:28px}h3{margin:0 0 6px;font-size:1em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}.narrow{max-width:380px}
label{display:block;margin:10px 0}label.row{display:flex;gap:8px;align-items:center}input[type=text],input[type=password],input:not([type]),input[type=number]{display:block;width:100%;max-width:360px;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{padding:7px 14px;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;cursor:pointer}
form.inline{display:inline}form.inline button{background:none;color:var(--accent);border:none;padding:0}
table{width:100%;border-collapse:collapse;display:block;overflow-x:auto}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
pre{background:var(--card);border:1px solid var(--line);padding:12px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-word}
.good{color:var(--good)}.bad{color:var(--bad)}.muted{color:var(--muted)}.small{font-size:.85em}.pill{border:1px solid var(--line);border-radius:99px;padding:1px 8px;font-size:.85em}
"""
