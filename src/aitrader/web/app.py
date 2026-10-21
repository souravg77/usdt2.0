"""The dashboard: a small web app behind a login.

First feature: submit an e-book. The book is saved to ``data/books``, added to the knowledge
base, and read by the agent, which writes its strategies as rules and backtests them (the same
pipeline as ``aitrader learn read --test``). Strategies that pass go to paper trading. The
overview shows the mode and live lock, every strategy with its results, the AI trade filter,
and recent jobs.

The Trading page shows the real Bybit account's balance (read only), open positions (paper and
live), the equity curve, the trade log
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
from urllib.parse import urlsplit

from ..clock import label as tz_label, local, offset_minutes

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from ..config import EXCHANGE, Settings
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
    pairs: str = "data/pairs/ranking.json"
    changes: str = "data/changes"
    feed: str = "data/ops/feed.json"
    teacher: str = "data/learn/teacher"
    improve: str = "data/learn/improve.json"


def create_app(settings: Settings, auth: Auth, jobs: JobRunner, paths: Paths | None = None,
               secure_cookie: bool = False, users=None, keys_secret: str = "", signup: bool = False,
               verifier=None, checkers: dict | None = None, live=None, account=None,
               monitor: dict | None = None, perp_live=None) -> FastAPI:
    """``users`` (an ``accounts.UserStore``) turns on user accounts: sign-up when ``signup`` is
    true, each user's own page, and the admin's Users page. Without it only the admin can log in."""
    paths = paths or Paths()
    if account is None:
        from .account import RealAccount

        account = RealAccount()
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
        # the script reloads the page but waits while someone types (static/reload.js); the
        # meta tag is for browsers without JavaScript
        meta = (f'<noscript><meta http-equiv="refresh" content="{refresh}"></noscript>'
                '<script src="/static/reload.js" defer></script>' if refresh else "")
        mode = settings.mode.value
        now = time.time()
        return HTMLResponse(f"""<!doctype html><html lang="en" data-tz-offset="{offset_minutes(now)}"
data-tz-label="{_e(tz_label(now))}"{f' data-reload-ms="{int(refresh * 1000)}"' if refresh else ""}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{_e(title)} · aitrader</title><style>{CSS}</style></head>
<body{' class="signed-in"' if user else ""}><header><a class="brand" href="/"><span class="logo" aria-hidden="true">{LOGO}</span>aitrader</a>
<span class="mode mode-{_e(mode)}">{_e(mode)} mode</span>{_market_switch(paths, auth, token, title) if user and auth.is_admin(user) else ""}{nav}</header>
<main><div class="page-head"><h1>{_e(title)}</h1>{head_extra}</div>{body}</main>
<footer class="muted small">aitrader · live trading stays locked until you open it on the server</footer>
{_tabbar(title) if user else ""}</body></html>""")

    # ---- login ----------------------------------------------------------------------------------

    @app.get("/static/reload.js")
    def reload_js():
        with open(os.path.join(os.path.dirname(__file__), "static", "reload.js"), encoding="utf-8") as fh:
            return Response(fh.read(), media_type="text/javascript")

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
        if cross_site(request):  # no session yet, so no form token: refuse logins posted by other sites
            return PlainTextResponse("log in from this site's own login page", status_code=403)
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

    from .change_pages import add_change_routes
    from .market_pages import add_market_routes
    from .news_pages import add_news_routes
    from .pair_pages import add_pair_routes
    from .replay_pages import add_replay_routes
    from .research_pages import add_research_routes
    from .report_pages import add_report_routes
    from .settings_pages import add_settings_routes

    if live is None:
        from .live import LiveMarket

        live = LiveMarket(settings.db_path, EXCHANGE)
    perp_feed: list = [perp_live] if perp_live is not None else []  # Futures AI's positions: perpetual prices

    def feed_for(ws: str):
        if ws != "futures":
            return live
        if not perp_feed:  # made on first use
            from ..data.perp import futures_db_path
            from .live import LiveMarket

            perp_feed.append(LiveMarket(futures_db_path(settings.db_path), EXCHANGE, perp=True))
        return perp_feed[0]

    @app.get("/api/positions")
    async def api_positions(request: Request):
        """Each open position's profit or loss in USDT at the live price (cached ~1s, the same
        feed as the Markets page), for the Trading page to update every second."""
        _user, _token, redirect = guard(request)
        if redirect:
            return JSONResponse({"error": "sign in"}, status_code=401)
        ws = "futures" if _futures_state(paths)[0] else "spot"  # the rows the Trading page shows
        paper = _workspace(_load_json(paths.paper_state), ws)
        execs = {m: _workspace(_load_json(os.path.join(paths.trade_dir, f"{m}_state.json")), ws) for m in ("live",)}
        symbols = sorted(set(_open_symbols(paper, execs)))
        if not symbols:
            return JSONResponse({"source": "", "rows": {}, "total": None})
        prices = await feed_for(ws).tickers(symbols)
        return JSONResponse(live_position_usdt(paper, execs, {t["symbol"]: t.get("price") for t in prices.get("tickers", [])},
                                               source=prices.get("source", "")))

    @app.get("/static/positions.js")
    def positions_js():
        with open(os.path.join(os.path.dirname(__file__), "static", "positions.js"), encoding="utf-8") as fh:
            return Response(fh.read(), media_type="text/javascript")

    add_market_routes(app, settings=settings, page=page, guard=guard, live=live, paper_state=paths.paper_state,
                      registry=paths.registry)
    add_report_routes(app, settings=settings, auth=auth, jobs=jobs, page=page, guard=guard,
                      report_path=paths.report)
    add_news_routes(app, settings=settings, auth=auth, jobs=jobs, page=page, guard=guard, news_dir=paths.news,
                    paper_state=paths.paper_state)
    add_research_routes(app, auth=auth, jobs=jobs, page=page, guard=guard, news_dir=paths.news)
    add_change_routes(app, auth=auth, jobs=jobs, page=page, guard=guard, changes_dir=paths.changes)
    add_pair_routes(app, settings=settings, auth=auth, jobs=jobs, page=page, guard=guard, ranking_path=paths.pairs,
                    paper_state=paths.paper_state, report_path=paths.report)
    add_replay_routes(app, settings=settings, auth=auth, jobs=jobs, page=page, guard=guard,
                      registry_path=paths.registry, report_path=paths.report, paper_state=paths.paper_state)

    add_settings_routes(app, settings=settings, auth=auth, paths=paths, jobs=jobs,
                        keys_secret=keys_secret, page=page, guard=guard, checkers=checkers)

    if users is not None:
        from .user_pages import add_user_routes

        add_user_routes(app, settings=settings, auth=auth, paths=paths, users=users,
                        keys_secret=keys_secret, signup=signup, verifier=verifier, page=page,
                        session=session, guard=guard, secure_cookie=secure_cookie)

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
    async def books_submit(request: Request):
        user, token, redirect = guard(request)  # before reading the body: no uploads without a login
        if redirect:
            return redirect
        form = await request.form()
        if not auth.check_csrf(token, str(form.get("csrf", ""))):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        book, test = form.get("book"), str(form.get("test", ""))
        try:
            max = int(str(form.get("max", "12")))
        except ValueError:
            max = 12
        if book is None or isinstance(book, str):
            return RedirectResponse(f"/books?error={_q('Choose a file to upload.')}", status_code=303)
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

    from .monitor_pages import add_monitor_routes

    add_monitor_routes(app, settings=settings, page=page, guard=guard, auth=auth, paths=paths,
                       csrf=lambda token: _csrf(auth, token), control=_control, **(monitor or {}))

    from .teacher_pages import add_teacher_routes

    add_teacher_routes(app, settings=settings, page=page, guard=guard, auth=auth, paths=paths,
                       csrf=lambda token: _csrf(auth, token), jobs=jobs, exchange=EXCHANGE)

    @app.get("/scan-log")
    def scan_log(request: Request, show: str = "all"):
        """How trades are scanned and taken: every pass over the charts, every signal and what
        became of it, and what the real-money trader did with them (one workspace at a time)."""
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        from ..ops import EventLog

        ws = "futures" if _futures_state(paths)[0] else "spot"
        paper = _load_json(paths.paper_state)
        events = EventLog(paths.events).read(limit=2000)
        live = [e for e in events if str(e.get("service") or "").startswith("executor")
                and e.get("kind") != "heartbeat"]
        show = show if show in ("all", "opened", "skipped") else "all"
        body = [_workspace_note(ws), _scan_steps(ws),
                "<h2>Scans</h2>", _scans_table(paper, ws, int(time.time() * 1000)),
                "<h2>Signals</h2>", _signal_log(_workspace(paper, ws), show),
                "<h2>Real-money trader</h2>",
                '<p class="small muted">What the live trader did with the paper trades it copies (both engines). '
                "Empty while live trading is locked.</p>", _events_table(live, limit=60)]
        return page("Scan log", "".join(body), user, token, refresh=30)

    @app.get("/trading")
    async def trading(request: Request, msg: str = "", bad: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        from ..ops import EventLog

        c = _control().state()
        paper = _load_json(paths.paper_state)
        execs = {m: _load_json(os.path.join(paths.trade_dir, f"{m}_state.json")) for m in ("live",)}
        acct = await account.snapshot()
        if c["killed"]:
            state = f'<b class="bad">KILL SWITCH ON</b> since {_when(c.get("since"))}'
        elif c.get("halted"):
            state = (f'<b class="bad">SAFETY HALT</b> since {_when(c.get("since"))}: {_e(c.get("reason", ""))}. '
                     'Check the account on Bybit, then press Resume')
        elif c["paused"]:
            state = f'<b class="bad">PAUSED</b> since {_when(c.get("since"))}: {_e(c.get("reason", ""))}'
        else:
            state = '<b class="good">running</b>'
        live = ('<span class="good">locked</span>' if live_gate_problems(settings)
                else '<span class="bad">UNLOCKED</span>')
        csrf = _csrf(auth, token)
        fut_view = _futures_state(paths)[0]
        ws = "futures" if fut_view else "spot"
        name = "Futures AI" if fut_view else "Spot AI"
        close_ws = (f'<form method="post" action="/trading/close-all" class="row">{csrf}'
                    f'<input type="hidden" name="market" value="{ws}">'
                    f'<button class="warn-btn">Close all {name} positions</button>')
        controls = (f'<div class="card"><h3>Controls</h3><p>Trading {state}<br>Mode <b>{_e(settings.mode.value)}</b>'
                    f' · live orders {live}</p><div class="row">'
                    f'<form method="post" action="/trading/pause">{csrf}<button>Pause everything</button></form>'
                    f'<form method="post" action="/trading/resume">{csrf}<button class="ghost">Resume</button></form>'
                    '</div><p class="small muted">Pause everything stops new entries on Spot AI, Futures AI and the '
                    f'live trader together (an emergency stop). To stop only {name}, use Turn {name} off on its card; '
                    'the other engine keeps trading.</p>'
                    + close_ws +
                    f'<span class="small muted">Closes every {name} paper position at the last closed candle\'s '
                    f'price (fees included) within a minute; the other engine\'s positions are not touched. '
                    f'{name} keeps running: turn it off first if you don\'t want new ones.</span></form>'
                    f'<form method="post" action="/trading/kill" class="kill">{csrf}'
                    '<label>Kill switch: stops entries, closes every paper position and sells everything the live executor bought. '
                    'Type KILL to confirm <input name="confirm" autocomplete="off" autocapitalize="characters" autocorrect="off" spellcheck="false" required placeholder="KILL"></label>'
                    '<button class="danger">Pull the kill switch</button></form></div>')
        note = f'<p class="{"bad" if bad else "good"}">{_e(msg)}</p>' if msg else ""
        events = EventLog(paths.events).read(limit=600)
        full = paper
        paper, execs = _workspace(paper, ws), {m: _workspace(st, ws) for m, st in execs.items()}
        body = [note, _workspace_note(ws), _trading_stats(c, settings, paths, paper, events, acct, ws),
                '<div class="grid pack">', _market_card(paths, full, csrf, ws), _wallet_card(paths, paper, csrf, ws),
                "" if fut_view else _account_card(acct, csrf), _paper_switch_card(c, paths, events, jobs.list(), csrf, ws),
                _ai_card(paths, jobs.list(), csrf, ws), _live_card(settings, paths, events),
                controls, _gate_card(paths, csrf), "</div>",
                "<h2>Equity</h2>", _equity_svg(paper, execs),
                "<h2>Open positions</h2>",
                (close_ws + "</form>" if (paper.get("open") if isinstance(paper, dict) else None) else ""),
                _positions_table(paper, execs, csrf, _control().close_requests(), futures_view=fut_view,
                                 why=_indicator_names(paths.registry)),
                "<h2>Signals</h2>", _signals_section(paper, int(time.time() * 1000)),
                "<h2>Trade log</h2>", _trades_table(paper, execs),
                "<h2>Events</h2>", _events_table(_workspace_events(events, ws))]
        body.append('<script src="/static/positions.js" defer></script>')
        return page("Trading", "".join(body), user, token, refresh=60)

    def _control_post(request: Request, csrf: str, action) -> Response:
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        out = action(user)
        text, ok = out if isinstance(out, tuple) else (out, True)
        return RedirectResponse(f"/trading?msg={_q(text)}{'' if ok else '&bad=1'}", status_code=303)

    def _event(kind: str, text: str, level: str = "info") -> None:
        from ..ops import EventLog

        EventLog(paths.events, service="dashboard").emit(kind, text, level=level)

    @app.post("/trading/paper/wallet")
    def trading_paper_wallet(request: Request, csrf: str = Form(""), amount: str = Form(""),
                             market: str = Form("spot")):
        def act(user):
            from ..learn import wallet

            try:
                value = float(amount.replace(",", "").strip())
            except ValueError:
                return (f"Not changed: enter an amount from {wallet.MIN_START:,.0f} to {wallet.MAX_START:,.0f} USDT.",
                        False)
            if not wallet.MIN_START <= value <= wallet.MAX_START:
                return (f"Not changed: the starting amount must be {wallet.MIN_START:,.0f} to "
                        f"{wallet.MAX_START:,.0f} USDT.", False)
            mkt = "futures" if market == "futures" else "spot"
            name = "Futures AI" if mkt == "futures" else "Spot AI"
            w = wallet.save(wallet.wallet_path(paths.paper_state, mkt), value)
            _event("paper_wallet", f"{name} paper wallet restarted with {w['start']:,.2f} USDT, from the dashboard by {user}")
            return (f"The {name} paper wallet starts again with {w['start']:,.2f} USDT. New {name} paper trades are "
                    "sized from it; the other wallet is not changed and the trade log keeps the earlier trades.")
        return _control_post(request, csrf, act)

    @app.post("/trading/account/refresh")
    def trading_account_refresh(request: Request, csrf: str = Form("")):
        def act(user):
            if account.refresh_now():
                return "Asking Bybit for the balance again."
            return "The balance was read a few seconds ago; that is the latest."
        return _control_post(request, csrf, act)

    @app.post("/trading/pause")
    def trading_pause(request: Request, csrf: str = Form("")):
        def act(user):
            _control().pause(f"everything paused from the dashboard by {user}", by=user)
            _event("risk_pause", f"everything paused from the dashboard by {user}", "warning")
            return ("Everything is paused: Spot AI, Futures AI and the live trader open no new trades. Open positions "
                    "are still managed. Resume starts them all again.")
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
            if not kill_confirmed(confirm):
                return "Not pulled: type KILL in the box next to the button, then press it.", False
            _control().kill(f"kill switch from the dashboard by {user}", by=user)
            _event("kill", f"kill switch pulled from the dashboard by {user}", "warning")
            return ("Kill switch on: entries stopped, paper positions close within a minute and the live executor "
                    "sells. Resume when ready.")
        return _control_post(request, csrf, act)

    @app.post("/trading/close-all")
    def trading_close_all(request: Request, csrf: str = Form(""), market: str = Form("")):
        def act(user):
            if market in ("spot", "futures"):  # one workspace: each of its positions, as its own Close
                ctl = _control()
                name = "Futures AI" if market == "futures" else "Spot AI"
                mine = _workspace(_load_json(paths.paper_state), market).get("open", {})
                for key, pos in mine.items():
                    ctl.request_close(key, (pos.get("signal") or {}).get("ts"), by=user)
                _event("close_all", f"close all {name} paper positions ({len(mine)}) asked from the dashboard by {user}",
                       "warning")
                return (f"Closing all {len(mine)} {name} paper positions at the last closed candle's price; they'll be "
                        "gone within a minute. The other engine's positions are not touched.")
            _control().request_close_all(by=user)
            _event("close_all", f"close all open paper positions asked from the dashboard by {user}", "warning")
            return ("Closing every open paper position at the last closed candle's price; they'll be gone within "
                    "a minute. Paper trading keeps running.")
        return _control_post(request, csrf, act)

    @app.post("/trading/close-one")
    def trading_close_one(request: Request, csrf: str = Form(""), key: str = Form("")):
        def act(user):
            paper = _load_json(paths.paper_state)
            pos = (paper.get("open") or {}).get(key) if isinstance(paper, dict) else None
            if pos is None:
                return "That position isn't open any more.", False
            _control().request_close(key, (pos.get("signal") or {}).get("ts"), by=user)
            _entry_id, _ex, sym, tf = (key.split("|") + ["", "", "", ""])[:4]
            _event("close_one", f"close paper position {key} asked from the dashboard by {user}", "warning")
            return (f"Closing the {sym} {tf} position at the last closed candle's price; it'll be gone within a "
                    "minute. The others stay open.")
        return _control_post(request, csrf, act)

    def _ws_market(market: str) -> str:
        """The engine a form is for: its own field, else the workspace shown."""
        if market in ("spot", "futures"):
            return market
        return "futures" if _futures_state(paths)[0] else "spot"

    @app.post("/trading/paper/on")
    def trading_paper_on(request: Request, csrf: str = Form(""), market: str = Form("")):
        """Turn one engine's paper trading on (Spot AI or Futures AI); the other is not changed."""
        def act(user):
            from ..learn import wallet

            mkt = _ws_market(market)
            name, other = ("Futures AI", "Spot AI") if mkt == "futures" else ("Spot AI", "Futures AI")
            ctl = _control()
            if ctl.killed:
                return "The kill switch is on: press Resume under Controls first, then turn paper trading on."
            epath = wallet.engines_path(paths.paper_state)
            was_on = wallet.engines(epath)[mkt]
            extra = ""
            if ctl.stops_paper and not str(ctl.state().get("reason", "")).startswith("paper trading turned off"):
                return ("Everything is paused under Controls: press Resume there first, then turn the engine on.",
                        False)
            if ctl.stops_paper:
                # the older "Turn paper trading off" paused both engines: bring back only this one
                ctl.resume(by=user)
                omkt = "spot" if mkt == "futures" else "futures"
                if wallet.engines(epath)[omkt]:
                    wallet.set_engine(epath, omkt, False, by=user)
                    extra = f" {other} stays off as it was; turn it on in its own workspace."
                _event("resume", f"pause lifted for {name} from the dashboard by {user}")
            wallet.set_engine(epath, mkt, True, by=user)
            _event("paper_on", f"{name} paper trading turned on from the dashboard by {user}")
            busy = any(j["status"] in ("queued", "running") and j.get("meta", {}).get("kind") == "paper-start"
                       for j in jobs.list())
            if was_on or not _trading_count(paths, mkt):
                if busy:
                    return f"{name} is on. The strategies are already being loaded; see Recent jobs on the Overview page.{extra}"
                since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 365 * 86400))
                jobs.submit("Load every strategy into paper trading, then a year of price history",
                            [["learn", "add-library"], ["learn", "add-builtin"], ["learn", "add-timeframe", "30m"],
                             ["learn", "add-shorts"],
                             ["learn", "paper-all"],
                             ["download", "--since", since, "--exchange", EXCHANGE],
                             ["learn", "test", "--paper"], ["learn", "keep-top", "--market", mkt]],
                            meta={"kind": "paper-start"})
                return (f"{name} paper trading is on, and every strategy is being loaded: a year of price history is "
                        "downloaded, each one is backtested and only the best ones keep trading. Trades open when a "
                        f"strategy's signal appears on a closed candle.{extra}")
            return (f"{name} paper trading is on: it opens new paper trades from its own wallet. {other} is not "
                    f"changed.{extra}")
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/keep")
    def trading_paper_keep(request: Request, csrf: str = Form(""), n: str = Form(""), market: str = Form("")):
        def act(user):
            from ..learn.keep import DEFAULT_N, MAX_N, MIN_N, clamp

            mkt = _ws_market(market)
            name = "Futures AI" if mkt == "futures" else "Spot AI"

            try:
                count = clamp(int(n or DEFAULT_N))
            except ValueError:
                return f"Not changed: enter a number from {MIN_N} to {MAX_N}."
            busy = {j.get("meta", {}).get("kind") for j in jobs.list() if j["status"] in ("queued", "running")}
            if busy & {"paper-start", "keep-top"}:
                return "Strategies are already being loaded or ranked; see Recent jobs on the Overview page."
            jobs.submit(f"{name}: keep only the best {count} strategies paper trading",
                        [["learn", "keep-top", "--n", str(count), "--market", mkt]], meta={"kind": "keep-top"})
            _event("keep_top", f"{name}: keep only the best {count} strategies, from the dashboard by {user}")
            return (f"Ranking {name}'s strategies on its own results: its best {count} keep trading on {name}, "
                    "the rest stop opening trades there (their open trades still close normally). The other "
                    "engine's strategies are not changed.")
        return _control_post(request, csrf, act)

    @app.post("/market-mode")
    def market_mode(request: Request, csrf: str = Form(""), market: str = Form(""), back: str = Form("/trading")):
        """The header's Spot / Futures switch: the same as the Trading page's, back to the same page."""
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        from ..learn import wallet

        to_futures = market == "futures"
        wallet.set_market(wallet.market_path(paths.paper_state), to_futures, by=user)
        _event("paper_market", f"dashboard switched to {'futures' if to_futures else 'spot'} by {user}")
        target = back if back in {href for href, _n in NAV} else "/trading"
        msg = ("Showing the Futures AI workspace: futures prices, the futures wallet and its positions. "
               "Nothing was converted." if to_futures else
               "Showing the Spot AI workspace: spot prices, the spot wallet and its positions. Nothing was converted.")
        return RedirectResponse(f"{target}?msg={_q(msg)}", status_code=303)

    @app.post("/trading/paper/market")
    def trading_paper_market(request: Request, csrf: str = Form(""), market: str = Form(""), on: str = Form("")):
        """Turn the Spot AI or the Futures AI paper engine on or off (its open trades keep running)."""
        def act(user):
            from ..learn import wallet

            if market not in wallet.MARKETS:
                return "Not changed: choose Spot AI or Futures AI.", False
            turn_on = on == "1"
            wallet.set_engine(wallet.engines_path(paths.paper_state), market, turn_on, by=user)
            name = "Futures AI" if market == "futures" else "Spot AI"
            _event("paper_engine", f"{name} paper engine turned {'on' if turn_on else 'off'} from the dashboard by {user}")
            if turn_on:
                return f"{name} is on: it opens new paper trades from its own wallet."
            return (f"{name} is off: it opens no new paper trades. Its open trades still run to their stop or target "
                    "(use Close on a position to end it now). The other engine is not changed.")
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/limits")
    def trading_paper_limits(request: Request, csrf: str = Form(""), market: str = Form(""), loss: str = Form(""),
                             max_open: str = Form("")):
        """Spot AI's or Futures AI's own daily loss limit and maximum of open positions."""
        def act(user):
            from ..learn import wallet

            if market not in wallet.MARKETS:
                return "Not changed: choose Spot AI or Futures AI.", False
            try:
                lim = wallet.set_limits(wallet.limits_path(paths.paper_state), market, float(loss), int(max_open), by=user)
            except ValueError:
                return "Not changed: enter a daily loss limit in % and a whole number of open positions.", False
            name = "Futures AI" if market == "futures" else "Spot AI"
            _event("paper_limits", f"{name} limits set to {lim['daily_loss_pct']:g}% a day, {lim['max_open']} open, by {user}")
            return (f"{name} now stops new trades for the day after losing {lim['daily_loss_pct']:g}% of its wallet, "
                    f"and holds at most {lim['max_open']} open positions.")
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/guards")
    def trading_paper_guards(request: Request, csrf: str = Form(""), market: str = Form(""),
                             breaker: str = Form(""), day_pause_pct: str = Form(""), pause_hours: str = Form(""),
                             week_close_pct: str = Form(""), min_rr: str = Form(""), trend: str = Form(""),
                             split_tp: str = Form(""), breakeven: str = Form(""), maker_entry: str = Form(""), max_long: str = Form(""),
                             max_short: str = Form(""), max_correlated: str = Form(""), corr_min: str = Form(""),
                             trade_usdt: str = Form(""), agree: str = Form("")):
        """One engine's loss breakers, minimum reward, higher-timeframe trend check and split take-profit."""
        def act(user):
            from ..learn import guards

            if market not in ("spot", "futures"):
                return "Not changed: choose Spot AI or Futures AI.", False
            try:
                g = guards.save(guards.path_for(paths.paper_state), market, {
                    "breaker": breaker == "1", "day_pause_pct": day_pause_pct, "pause_hours": pause_hours,
                    "week_close_pct": week_close_pct, "min_rr": min_rr or 0, "trend": trend == "1",
                    "split_tp": split_tp == "1", "breakeven": breakeven == "1", "maker_entry": maker_entry == "1",
                    "max_long": max_long,
                    "max_short": max_short, "max_correlated": max_correlated, "corr_min": corr_min,
                    "trade_usdt": trade_usdt, "agree": agree}, by=user)
            except ValueError:
                return "Not changed: enter numbers for the amount per trade, loss limits, hours and minimum reward.", False
            name = "Futures AI" if market == "futures" else "Spot AI"
            _event("paper_guards", f"{name} trading rules set by {user}: {_guards_text(g)}")
            return f"{name} now uses: {_guards_text(g)}. Open trades keep the take-profit they opened with."
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/amount")
    def trading_paper_amount(request: Request, csrf: str = Form(""), market: str = Form(""),
                             trade_usdt: str = Form("")):
        """The amount each new paper trade of one engine uses (0 = sized from the strategy's risk)."""
        def act(user):
            from ..learn import guards

            if market not in ("spot", "futures"):
                return "Not changed: choose Spot AI or Futures AI.", False
            try:
                g = guards.save(guards.path_for(paths.paper_state), market,
                                {"trade_usdt": trade_usdt.strip() or 0}, by=user)
            except ValueError:
                return "Not changed: enter the amount per trade as a number (0 for automatic).", False
            name = "Futures AI" if market == "futures" else "Spot AI"
            amt = g["trade_usdt"]
            what = (f"{amt:,.2f} USDT per trade" + (" as margin, times the leverage" if market == "futures" else "")
                    if amt else "automatic trade size (from the strategy's risk)")
            _event("paper_guards", f"{name} amount per trade set by {user}: {what}")
            return f"Saved: {name} now uses {what}. Open trades keep their size."
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/timeframes")
    async def trading_paper_timeframes(request: Request):
        """The timeframes one engine opens new paper trades on (the ticked boxes; none ticked = all)."""
        form = await request.form()
        market = str(form.get("market", ""))
        chosen = [str(t) for t in form.getlist("tf")]

        def act(user):
            from ..learn import guards

            if market not in ("spot", "futures"):
                return "Not changed: choose Spot AI or Futures AI.", False
            g = guards.save(guards.path_for(paths.paper_state), market, {"timeframes": chosen or list(guards.TIMEFRAMES)}, by=user)
            name = "Futures AI" if market == "futures" else "Spot AI"
            what = ", ".join(g["timeframes"])
            _event("paper_guards", f"{name} timeframes set by {user}: {what}")
            return (f"Saved: {name} now opens trades only on {what} candles. Open trades on other timeframes "
                    "still run to their exits.")
        return _control_post(request, str(form.get("csrf", "")), act)

    @app.post("/trading/paper/leverage")
    def trading_paper_leverage(request: Request, csrf: str = Form(""), lev: str = Form("")):
        def act(user):
            from ..learn import wallet
            from ..trade import futures

            try:
                value = futures.clamp_paper_leverage(float(lev))
            except ValueError:
                return f"Not changed: choose 1x to {futures.PAPER_MAX_LEVERAGE:g}x.", False
            wallet.set_leverage(wallet.market_path(paths.paper_state), value, by=user)
            _event("paper_leverage", f"paper leverage set to {value:g}x from the dashboard by {user}")
            return (f"Paper futures leverage is now {value:g}x for new trades (less on coins whose Bybit maximum is "
                    "lower); open ones keep theirs. The live trader keeps its own limit.")
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/winners-only")
    def trading_paper_winners_only(request: Request, csrf: str = Form(""), on: str = Form(""),
                                   market: str = Form("")):
        def act(user):
            from ..learn import picks

            mkt = _ws_market(market)
            name = "Futures AI" if mkt == "futures" else "Spot AI"
            turn_on = on == "1"
            picks.set_winners_only(paths.registry, turn_on, by=user, market=mkt)
            _event("winners_only", f"{name}: WIN strategies only turned {'on' if turn_on else 'off'} from the "
                                   f"dashboard by {user}")
            if turn_on:
                return (f"{name} trades only the WIN strategies from the next minute. Trades the others already "
                        "opened still close normally. The other engine is not changed.")
            return f"{name} trades the best strategies again, with the WIN ones. The other engine is not changed."
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/filter")
    def trading_paper_filter(request: Request, csrf: str = Form(""), market: str = Form(""), name: str = Form(""),
                             on: str = Form("")):
        """Whether one engine uses the AI filter or the backtest teacher; the other is not changed."""
        def act(user):
            from ..learn import wallet

            mkt = _ws_market(market)
            eng = "Futures AI" if mkt == "futures" else "Spot AI"
            if name not in wallet.FILTERS:
                return "Not changed: choose the AI filter or the teacher.", False
            what = "the AI filter" if name == "ai" else "the backtest teacher"
            turn_on = on == "1"
            wallet.set_filter(wallet.filters_path(paths.paper_state), mkt, name, turn_on, by=user)
            _event("paper_filter", f"{eng} {'uses' if turn_on else 'stopped using'} {what}, from the dashboard by {user}")
            if turn_on:
                return f"{eng} uses {what} again whenever it is turned on. The other engine is not changed."
            return f"{eng} no longer uses {what}: it takes its signals without it. The other engine is not changed."
        return _control_post(request, csrf, act)

    @app.post("/trading/ai/train")
    def trading_ai_train(request: Request, csrf: str = Form("")):
        def act(user):
            busy = {j.get("meta", {}).get("kind") for j in jobs.list() if j["status"] in ("queued", "running")}
            if "ai-train" in busy:
                return "The AI filter is already being trained; see Recent jobs on the Overview page."
            if "paper-start" in busy:
                return ("Wait until paper trading has finished switching on: the AI filter learns from the "
                        "year of price history it is still downloading.")
            jobs.submit("Train the AI trade filter on every strategy's backtested and paper signals",
                        [["ai", "retrain", "--all"]], meta={"kind": "ai-train"})
            _event("ai_train", f"AI filter training started from the dashboard by {user}")
            return ("Training started. When it finishes, the AI filter card shows how the new model did on "
                    "data it never saw, and a Turn on button if it beat the unfiltered strategies.")
        return _control_post(request, csrf, act)

    @app.post("/trading/ai/on")
    def trading_ai_on(request: Request, csrf: str = Form(""), version: str = Form("")):
        def act(user):
            from ..ai import ModelRegistry

            reg = ModelRegistry(paths.models)
            try:
                reg.promote(version, approved_by=user)
            except (KeyError, ValueError) as exc:
                return f"Not turned on: {exc}"
            _event("ai_promote", f"AI filter {version} turned on from the dashboard by {user}", "warning")
            return (f"AI filter {version} is on. Paper trading now skips the signals it rates weakest and "
                    "shrinks the doubtful ones; Turn off takes every signal again at once.")
        return _control_post(request, csrf, act)

    @app.post("/trading/ai/off")
    def trading_ai_off(request: Request, csrf: str = Form("")):
        def act(user):
            from ..ai import ModelRegistry

            ModelRegistry(paths.models).deactivate(by=user)
            _event("ai_off", f"AI filter turned off from the dashboard by {user}")
            return "AI filter off: every signal is taken at full size."
        return _control_post(request, csrf, act)

    @app.post("/trading/paper/off")
    def trading_paper_off(request: Request, csrf: str = Form(""), market: str = Form("")):
        """Turn one engine's paper trading off; the other engine keeps trading."""
        def act(user):
            from ..learn import wallet

            mkt = _ws_market(market)
            name, other = ("Futures AI", "Spot AI") if mkt == "futures" else ("Spot AI", "Futures AI")
            wallet.set_engine(wallet.engines_path(paths.paper_state), mkt, False, by=user)
            _event("paper_engine", f"{name} paper trading turned off from the dashboard by {user}", "warning")
            return (f"{name} paper trading is off: no new {name} trades. Its open positions are still managed until "
                    f"they close. {other} keeps trading.")
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



def kill_confirmed(text: str) -> bool:
    """The kill switch's confirm box: KILL in any case (phones capitalise only the first letter)."""
    return (text or "").strip().upper() == "KILL"


LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>')
# the phone's bottom tab bar: the pages used most (the full list stays in the header)
TABS = [("/trading", "Trading", '<path d="M3 17l6-6 4 4 8-8"/>'),
        ("/markets", "Markets", '<path d="M5 20V10M12 20V4M19 20v-7"/>'),
        ("/pairs", "Gainers", '<path d="M12 3l3 6 6 1-4.5 4 1 6-5.5-3-5.5 3 1-6L3 10l6-1z"/>'),
        ("/replay", "Replay", '<path d="M6 4l13 8-13 8z"/>'),
        ("/monitor", "Monitor", '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>')]


def _tabbar(title: str) -> str:
    names = {"Pairs": "Gainers"}
    return ('<nav class="tabbar" aria-label="main pages">' + "".join(
        f'<a href="{href}"{" class=active aria-current=page" if names.get(title, title) == name else ""}>'
        f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{icon}</svg>{name}</a>' for href, name, icon in TABS) + "</nav>")


NAV = [("/", "Overview"), ("/markets", "Markets"), ("/pairs", "Pairs"), ("/trading", "Trading"), ("/scan-log", "Scan log"),
       ("/monitor", "Monitor"),
       ("/replay", "Replay"), ("/teacher", "Teacher"),
       ("/news", "News"), ("/research", "Research"), ("/reports", "Reports"), ("/books", "Submit a book"), ("/changes", "Change requests"), ("/admin/users", "Users"),
       ("/settings", "Settings")]


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


def cross_site(request) -> bool:
    """A form posted from another website (the browser says so in Sec-Fetch-Site or Origin).
    Our pages send ``Referrer-Policy: no-referrer``, so browsers post our own forms with
    ``Origin: null``: that says nothing about the site and isn't refused."""
    if request.headers.get("sec-fetch-site", "") == "cross-site":
        return True
    origin = (request.headers.get("origin") or "").strip()
    if not origin or origin == "null":
        return False
    return urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()


def _csrf(auth: Auth, token: str | None) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(auth.csrf(token or ""))}">'


def _when(ts) -> str:
    if not ts:
        return "-"
    return local(ts)


def _status_class(status: str) -> str:
    return {"done": "good", "paper": "good", "active": "good", "failed": "bad",
            "rejected": "bad", "retired": "muted", "archived": "muted", "shadow": "good",
            "live_eligible": "good", "live_enabled": "good", "under_review": "warn"}.get(status, "")


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
    from ..learn.ladder import Evidence, ladder_state, label

    reg = Registry(paths.registry)
    if not reg.entries:
        return "<p class=muted>No strategies yet. Start the first-time setup on the <a href=\"/settings\">Settings</a> page, or submit a book.</p>"
    order = {"paper": 0, "oos_validated": 1, "backtested": 1, "research": 1, "under_review": 2,
             "retired": 3, "rejected": 4}
    ev = Evidence(paths.golive_dir, paths.trade_dir, paths.registry, paths.models, paths.paper_state)
    rows = []
    for e in sorted(reg.entries.values(), key=lambda e: (order.get(e.status, 9), e.id)):
        state, note = ladder_state(e, ev)
        bt = e.backtest.get("out_of_sample", {})
        pp = e.paper or {}
        why = e.history[-1]["why"] if e.history else ""
        rows.append(
            f'<tr><td><a href="/strategies/{_e(e.id)}">{_e(e.id)}</a><div class="muted small">{_e(e.source[:70])}</div></td>'
            f'<td class="{_status_class(state)}">{_e(label(state))}<div class="muted small">{_e(note)}</div></td>'
            f'<td>{_result(bt)}</td><td>{_result(pp)}</td>'
            f'<td class="small">{_e(why[:140])}</td></tr>')
    return ("<table><tr><th>Strategy</th><th>Ladder</th><th>Backtest (out of sample)</th>"
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
        text += f'<br>{len(waiting)} candidate(s) waiting for approval on the <a href="/trading">Trading</a> page'
    elif not reg.active:
        text += '<br><a href="/trading">Train it on the Trading page</a>'

    return f'<div class="card"><h3>AI trade filter</h3><p>{text}</p></div>'


def _ai_card(paths: Paths, job_list: list[dict], csrf: str, ws: str = "spot") -> str:
    """The AI trade filter: train a new model, turn the best candidate on, or turn it off; and
    whether the workspace's engine uses it and the backtest teacher."""
    from ..ai import ModelRegistry

    reg = ModelRegistry(paths.models)
    training = next((j for j in job_list if j["status"] in ("queued", "running")
                     and j.get("meta", {}).get("kind") == "ai-train"), None)
    if reg.active:
        f = reg.info(reg.active)["test"]["filtered"]
        state = (f'<b class="good">ON</b>: model {_e(reg.active)} (on unseen data {f["trades"]} trades, '
                 f'{f["expectancy_r"]:+.3f}R each)')
    else:
        state = '<b class="bad">OFF</b>: every signal is taken at full size'
    parts = [f'<div class="card"><h3>AI trade filter</h3><p>{state}</p>',
             '<p class="small muted">The filter learns from every strategy\'s backtested and paper signals '
             'which ones reached 1R before the stop. It is tested on a later period it never saw, and it can be '
             'turned on only when it beat the strategies without a filter there (higher result per trade, '
             'no worse drawdown). When on, paper trading skips the weakest signals and shrinks doubtful '
             'ones. A monthly retrain never turns a model on by itself.</p>']
    newest = reg.versions()[-1] if reg.versions() else None
    if newest and newest != reg.active:
        m = reg.info(newest)
        u, f = m["test"]["unfiltered"], m["test"]["filtered"]
        verdict = ('<b class="good">beat the unfiltered strategies</b>' if m["status"] == "candidate"
                   else f'<b class="bad">{_e(m["status"])}</b>: {_e(m.get("why", ""))}')
        parts.append(f'<p class="small">Latest model {_e(newest)} {verdict}<br>on unseen data: without filter '
                     f'{u["trades"]} trades {u["expectancy_r"]:+.3f}R each; with it {f["trades"]} trades '
                     f'{f["expectancy_r"]:+.3f}R each</p>')
    buttons = []
    if training:
        parts.append(f'<p class="small">Training now (<a href="/jobs/{_e(training["id"])}">progress</a>)</p>')
    else:
        buttons.append(f'<form method="post" action="/trading/ai/train">{csrf}<button class="ghost">'
                       f'{"Retrain" if reg.versions() else "Train the AI filter"}</button></form>')
    if newest and newest != reg.active and reg.info(newest)["status"] == "candidate":
        buttons.append(f'<form method="post" action="/trading/ai/on">{csrf}'
                       f'<input type="hidden" name="version" value="{_e(newest)}">'
                       f'<button>Turn on {_e(newest)}</button></form>')
    if reg.active:
        buttons.append(f'<form method="post" action="/trading/ai/off">{csrf}<button class="ghost">'
                       'Turn the AI filter off</button></form>')
    parts.append(f'<div class="row">{"".join(buttons)}</div>')
    parts.append(_engine_filters(paths, csrf, ws))
    parts.append('</div>')
    return "".join(parts)


def _engine_filters(paths: Paths, csrf: str, ws: str) -> str:
    """Whether the workspace's engine uses the AI filter and the backtest teacher (each engine
    chooses for itself; turning a model or lessons on or off is shared research)."""
    from ..learn import wallet

    name = "Futures AI" if ws == "futures" else "Spot AI"
    uses = wallet.filters(wallet.filters_path(paths.paper_state))[ws]
    rows = []
    for key, what in (("ai", "AI filter"), ("teacher", "backtest teacher")):
        yes = uses[key]
        rows.append(f'<form method="post" action="/trading/paper/filter" class="row">{csrf}'
                    f'<input type="hidden" name="market" value="{ws}"><input type="hidden" name="name" value="{key}">'
                    f'<input type="hidden" name="on" value="{"0" if yes else "1"}">'
                    f'<span class="small">{name} uses the {what}: <b class="{"good" if yes else "muted"}">'
                    f'{"YES" if yes else "NO"}</b></span>'
                    f'<button class="ghost">{"Stop using it on " + name if yes else "Use it on " + name}</button></form>')
    return ('<p class="small muted">Each engine chooses for itself whether it uses the AI filter and the backtest '
            'teacher (when they are turned on); changing it here does not change the other engine.</p>'
            + "".join(rows))


def _wallet_card(paths: Paths, paper: dict, csrf: str, ws: str = "spot") -> str:
    """The workspace's paper wallet: its balance, what's in open positions, and its starting amount
    (100-10,000). ``paper`` is already cut to that workspace."""
    from ..learn import wallet

    name = "Futures AI" if ws == "futures" else "Spot AI"
    w = wallet.load(wallet.wallet_path(paths.paper_state, ws))
    sm = wallet.summary(paper if isinstance(paper, dict) else {}, w, ws)
    open_pnl = [u for u in (_paper_usdt(p) for p in (paper or {}).get("open", {}).values()) if u is not None]
    worth = sm["balance"] + sum(open_pnl)
    since = f"since {_ms(w['since'])}" if w["since"] else "since paper trading began"
    pnl = sm["closed_pnl"]
    rules = ("Each trade risks its strategy's share of the balance (about 0.5%), one position is at most half of "
             "it, and open positions together never use more than it.")
    what = ("Futures AI trades USDT perpetual futures, long and short. A position ties up only its margin "
            "(its value divided by the leverage)." if ws == "futures" else
            "Spot AI buys coins with no leverage and never sells short. A position ties up what it cost.")
    return (f'<div class="card"><h3>{name} paper wallet (fake money)</h3>'
            f'<p>Balance <b>{sm["balance"]:,.2f} USDT</b><br>'
            f'<span class="small">Started with {w["start"]:,.2f} USDT {_e(since)} · '
            f'<span class="{"good" if pnl >= 0 else "bad"}">{pnl:+,.2f}</span> from {sm["trades"]} closed trades</span></p>'
            '<table>'
            f'<tr><td>{"Margin in open positions" if ws == "futures" else "In open positions"}</td><td>{sm["in_use"]:,.2f}</td></tr>'
            f'<tr><td>Free to trade</td><td>{sm["free"]:,.2f}</td></tr>'
            f'<tr><td>Open trades now</td><td class="{"good" if sum(open_pnl) >= 0 else "bad"}">{sum(open_pnl):+,.2f}</td></tr>'
            f'<tr><td><b>Worth now</b></td><td><b>{worth:,.2f}</b></td></tr></table>'
            f'<p class="small muted">{what} This wallet is separate from the '
            f'{"Spot AI" if ws == "futures" else "Futures AI"} one. {rules} A trade under Bybit\'s '
            f"{wallet.MIN_ORDER_USDT:g} USDT minimum order is skipped.</p>"
            f'<form method="post" action="/trading/paper/wallet">{csrf}<input type="hidden" name="market" value="{ws}">'
            f'<label>Starting amount (USDT) <input name="amount" type="number" inputmode="decimal" '
            f'min="{wallet.MIN_START:g}" max="{wallet.MAX_START:g}" step="any" value="{w["start"]:g}" required></label>'
            f'<button class="ghost">Start a new {name} wallet</button>'
            '<p class="small muted">Sets the balance to this amount from now on. Open positions stay open '
            'and count in the new wallet when they close.</p></form></div>')


def _workspace(state: dict, ws: str) -> dict:
    """A paper or live state cut to one workspace (Spot AI or Futures AI): its open positions,
    closed trades and signals only."""
    from ..learn import wallet

    if not isinstance(state, dict):
        return {}

    def mine(row: dict) -> bool:
        if str(row.get("key", "")).endswith("|futures"):
            return ws == "futures"
        return wallet.market_of(row) == ws

    out = dict(state)
    if isinstance(state.get("open"), dict):
        out["open"] = {k: p for k, p in state["open"].items() if mine({**p, "key": k})}
    for name in ("trades", "signals", "closed"):
        if isinstance(state.get(name), list):
            out[name] = [r for r in state[name] if mine(r)]
    if isinstance(state.get("positions"), dict):
        out["positions"] = {k: p for k, p in state["positions"].items() if mine(p)}
    return out


def _workspace_events(events: list[dict], ws: str) -> list[dict]:
    """The events table of one workspace: paper trade events of the other engine are left out."""
    def other(e: dict) -> bool:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        key = str(d.get("key") or "")
        if e.get("kind") not in ("trade_open", "trade_close") or not key or d.get("mode") != "paper":
            return False
        text = str(e.get("text") or "")
        fut = (d["market"] == "futures" if d.get("market") in ("spot", "futures")
               else key.endswith("|futures") or "perp" in text or "SELL" in text)
        return fut != (ws == "futures")
    return [e for e in events if not other(e)]


def _workspace_note(ws: str) -> str:
    if ws == "futures":
        return ('<p class="small"><b>Futures AI workspace</b>: the futures wallet, positions, signals and trades. '
                'Spot AI keeps running in the background; switch to Spot in the header to see it.</p>')
    return ('<p class="small"><b>Spot AI workspace</b>: the spot wallet, positions, signals and trades. '
            'Futures AI keeps running in the background; switch to Futures in the header to see it.</p>')


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
    from ..learn import wallet

    ws = "futures" if _futures_state(paths)[0] else "spot"  # only the workspace shown
    st = _workspace(st, ws)
    trades = st.get("trades", [])
    total = sum(t.get("pnl_usdt", 0) for t in trades)
    recent = [t for t in trades if t.get("exit_ts", 0) >= week]
    bal = wallet.balance(st, wallet.load(wallet.wallet_path(paths.paper_state, ws)), ws)
    name = "Futures AI" if ws == "futures" else "Spot AI"
    return (f'<div class="card"><h3>{name} paper trading</h3><p>Wallet <b>{bal:,.2f} USDT</b><br>'
            f'{len(st.get("open", {}))} open · '
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


def _close_button(key: str, csrf: str, asked: dict) -> str:
    """A paper position's own Close button (only on the admin's Trading page, where csrf is set)."""
    if not csrf:
        return ""
    if key in asked:  # the paper trader clears it once the position is closed
        return '<br><span class="small muted">closing...</span>'
    return (f'<form method="post" action="/trading/close-one">{csrf}<input type="hidden" name="key" value="{_e(key)}">'
            '<button class="warn-btn mini">Close</button></form>')


def _lev_cell(p: dict, side) -> str:
    """A perpetual position's leverage, margin and size, and why it uses less than the setting."""
    if not (p.get("perp") or side == "sell"):
        return "<td>-</td>"
    lev = p.get("leverage")
    if not p.get("filled") or not lev or not p.get("units") or not p.get("entry"):
        return '<td><span class="small muted">set when it fills</span></td>'
    size = float(p["units"]) * float(p["entry"])
    why = p.get("lev_why")
    less = (f'<br><span class="small muted">setting {p.get("lev_setting", lev):g}x; {_e(why)}</span>'
            if why else "")
    return (f'<td><b>{lev:g}x</b><br><span class="small">margin {size / lev:,.2f} × {lev:g} = '
            f'{size:,.2f} USDT position</span>{less}</td>')


def _liq_cell(p: dict, side) -> str:
    """A perpetual position's rough liquidation price and how far the price is from it."""
    from ..trade import futures

    lev, entry = p.get("leverage"), p.get("entry")
    if not lev or not entry or not (p.get("perp") or side == "sell"):
        return "<td>-</td>"
    mmr = p.get("mmr", futures.MAINTENANCE_MARGIN)
    liq = (futures.short_liquidation_price(entry, lev, mmr) if side == "sell"
           else futures.long_liquidation_price(entry, lev, mmr))
    if lev <= 1 and side != "sell":
        return '<td><span class="small muted">none at 1x</span></td>'
    mark = p.get("mark")
    away = "" if not mark else f' <span class="small muted">{abs(liq - mark) / mark * 100:.1f}% away</span>'
    return f"<td>{liq:.6g}{away}</td>"


def _indicator_names(registry_path: str):
    """entry id -> the chart indicators its rules use ("EMA 20/50/200, RSI"), from the registry."""
    from .market_pages import STUDIES

    labels = {k: label for k, label, *_ in STUDIES}
    from ..learn.keep import indicators

    try:
        from ..learn import Registry

        entries = Registry(registry_path).entries
    except Exception:
        entries = {}
    memo: dict[str, str] = {}

    def names(entry_id: str) -> str:
        if entry_id not in memo:
            try:
                memo[entry_id] = ", ".join(labels.get(k, k) for k in sorted(indicators(entries[entry_id])))
            except Exception:
                memo[entry_id] = ""
        return memo[entry_id]
    return names


def _why_cell(sig: dict, used: str) -> str:
    """Why a position was opened: the strategy's own words for its signal and the indicators its rules
    read (the same ones the Markets and Replay charts draw)."""
    reason = (sig.get("reason") or "").strip()
    return (f'<td class="small">{_e(reason[:160])}{"…" if len(reason) > 160 else ""}'
            f'{"<br><span class=muted>Indicators: " + _e(used) + "</span>" if used else ""}</td>')


def _positions_table(paper: dict, execs: dict, csrf: str = "", asked: dict | None = None,
                     futures_view: bool = False, why=None) -> str:
    rows, total, counted = [], 0.0, 0
    for key, p in paper.get("open", {}).items():
        sig = p.get("signal", {})
        entry_id, ex, sym, tf = (key.split("|") + ["", "", "", ""])[:4]
        mark = p.get("mark_r")
        usdt = _paper_usdt(p)
        if usdt is not None:
            total, counted = total + usdt, counted + 1
        # the money and price columns come first so a phone shows them without scrolling sideways
        pid = "paper|" + key
        rows.append(f"<tr><td>paper{_close_button(key, csrf, asked or {})}</td><td>{_e(sym)}{_short(sig.get('side'), p.get('perp'))} <span class=muted>{_e(ex)} {_e(tf)}</span></td>"
                    f"{_usdt_cell(usdt, pos_id=pid)}"
                    f"{_price_cells(pid, p.get('mark') if p.get('filled') else None, sig.get('stop', 0), sig.get('side'), _num(sig.get('target')), sig.get('trailing'))}"
                    f"{_lev_cell(p, sig.get('side')) + _liq_cell(p, sig.get('side')) if futures_view else ''}"
                    f"<td>{'' if mark is None else f'{mark:+.2f}R'}</td>"
                    f"<td>{_e(entry_id)}</td><td>{p.get('entry', sig.get('entry', 0)):.6g}"
                    f"{'' if p.get('filled') else ' <span class=muted>(waiting)</span>'}</td>"
                    f"<td>{_prob(p.get('ai_prob'))}</td>{_why_cell(sig, why(p.get('entry_id') or entry_id) if why else '')}</tr>")
    for mode, st in execs.items():
        for p in st.get("positions", {}).values():
            cls = "" if p.get("status") == "open" else "bad"
            usdt = _live_usdt(p) if p.get("status") == "open" else None
            if usdt is not None:
                total, counted = total + usdt, counted + 1
            pid = mode + "|" + str(p.get("key", ""))
            rows.append(f"<tr><td>{_e(mode)}</td><td>{_e(p['symbol'])} <span class=muted>{_e(p.get('amount', 0))}</span></td>"
                        f'{_usdt_cell(usdt, pos_id=pid)}{_price_cells(pid, p.get("mark"), p["stop"], p.get("side"), _num(p.get("target")))}'
                        f'{"<td>-</td><td>-</td>" if futures_view else ""}<td class="{cls}">{_e(p.get("status"))}</td>'
                        f"<td>{_e(p['entry_id'])}</td><td>{p.get('entry') or 0:.6g}</td>"
                        f'<td>{_prob(p.get("ai_prob"))}</td>{_why_cell({}, why(p["entry_id"]) if why else "")}</tr>')
    if not rows:
        return "<p class=muted>No open positions.</p>"
    foot = (f'<tr><td colspan="2"><b>Total now</b> <span class="muted" id="pos-live">at the last closed candle</span></td>'
            f'{_usdt_cell(total if counted else None, bold=True, pos_id="total")}<td colspan="{10 if futures_view else 8}"></td></tr>')
    return ("<table><tr><th>Where</th><th>Market</th><th>Now USDT</th><th>Price now</th><th>Stop loss</th><th>Take profit</th>"
            + ("<th>Leverage</th><th>Liquidation</th>" if futures_view else "") +
            "<th>R (last candle)</th><th>Strategy</th><th>Entry</th>"
            f"<th>AI p</th><th>Why (indicators)</th></tr>{''.join(rows)}{foot}</table>")


def _num(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def stop_distance(price: float | None, stop: float, side) -> float | None:
    """How far the price is from the stop, in % of the price: above 0 while the stop hasn't been
    reached (below the price for a long, above it for a short)."""
    if not price or not stop:
        return None
    gap = (stop - price) if side in ("sell", "short") else (price - stop)
    return 100 * gap / price


def target_distance(price: float | None, target: float | None, side) -> float | None:
    """How far the price still has to go to the take-profit, in % of the price (above 0 until
    it is reached: the target is above the price for a long, below it for a short)."""
    if not price or not target:
        return None
    gap = (price - target) if side in ("sell", "short") else (target - price)
    return 100 * gap / price


def _price_cells(pos_id: str, price: float | None, stop: float, side, target: float | None = None,
                 trailing: str | None = None) -> str:
    """The "Price now", "Stop loss" and "Take profit" cells; static/positions.js keeps them live.
    A strategy without a fixed target exits on its own rule (``trailing``) or its time stop."""
    d = stop_distance(price, stop, side)
    away = "" if d is None else f' <span class="small {"muted" if d > 0 else "bad"}">{d:.2f}% away</span>'
    short = "1" if side in ("sell", "short") else ""
    if target:
        t = target_distance(price, target, side)
        t_away = "" if t is None else f' <span class="small {"muted" if t > 0 else "good"}">{t:.2f}% away</span>'
        tp = (f'<td data-tp="{_e(pos_id)}" data-level="{target:.10g}" data-short="{short}">'
              f'{target:.6g}{t_away}</td>')
    else:
        rule = f"exit rule: {trailing}" if trailing else "no fixed target"
        tp = f'<td><span class="small muted">{_e(rule)}</span></td>'
    return (f'<td data-px="{_e(pos_id)}">{"-" if not price else f"{price:.6g}"}</td>'
            f'<td data-stop="{_e(pos_id)}" data-level="{stop:.10g}" data-short="{short}">{stop:.6g}{away}</td>{tp}')


def _paper_usdt(p: dict) -> float | None:
    """A paper position's profit or loss in USDT at the last closed candle, worked out the way
    the paper trader books it when the trade closes (R x risk per unit x units)."""
    sig = p.get("signal", {})
    if not p.get("filled") or p.get("mark_r") is None or not p.get("units"):
        return None
    try:
        return float(p["mark_r"]) * abs(float(sig["entry"]) - float(sig["stop"])) * float(p["units"])
    except (KeyError, TypeError, ValueError):
        return None


def _live_usdt(p: dict) -> float | None:
    """A live position's profit or loss in USDT at the executor's last price (the bid for a
    long, the ask for a short), after the entry fee; None until it has read a price."""
    if p.get("mark") is None or not p.get("amount") or not p.get("entry"):
        return None
    if p.get("side") == "short":  # the executor's word for a perp short
        return (p["entry"] - p["mark"]) * p["amount"] - p.get("entry_fee", 0.0)
    if ":" in (p.get("market") or ""):  # a Futures AI long on the perpetual: margin only, no coins bought
        return (p["mark"] - p["entry"]) * p["amount"] - p.get("entry_fee", 0.0)
    return p["mark"] * p["amount"] - p.get("cost", p["entry"] * p["amount"])


def _usdt_cell(x: float | None, bold: bool = False, pos_id: str = "") -> str:
    tag = f' data-pos="{_e(pos_id)}"' if pos_id else ""
    if x is None:
        return f"<td{tag}>-</td>"
    text = f"{x:+,.2f}"
    return f'<td{tag} class="{"good" if x >= 0 else "bad"}">{f"<b>{text}</b>" if bold else text}</td>'


def _open_symbols(paper: dict, execs: dict) -> list[str]:
    out = [(k.split("|") + ["", "", ""])[2] for k, p in paper.get("open", {}).items() if p.get("filled")]
    for st in execs.values():
        out += [p.get("symbol", "") for p in st.get("positions", {}).values() if p.get("status") == "open"]
    return [s for s in out if s]


def live_position_usdt(paper: dict, execs: dict, prices: dict, source: str = "") -> dict:
    """Each open position's profit or loss in USDT at ``prices`` (symbol -> last price), keyed
    like the table's cells, plus the total. Before exit fees; a position without a price is left
    out (its cell keeps what it showed)."""
    rows: dict[str, float] = {}
    px_by_pos: dict[str, float] = {}  # each position's live price, for the Price now column
    for key, p in paper.get("open", {}).items():
        sym = (key.split("|") + ["", "", ""])[2]
        px, units, entry = prices.get(sym), p.get("units"), p.get("entry")
        if px:
            px_by_pos["paper|" + key] = px
        if not p.get("filled") or not px or not units or not entry:
            continue
        short = (p.get("signal") or {}).get("side") == "sell"
        rows["paper|" + key] = ((entry - px) if short else (px - entry)) * units
    for mode, st in execs.items():
        for p in st.get("positions", {}).values():
            px = prices.get(p.get("symbol"))
            if p.get("status") != "open" or not px:
                continue
            u = _live_usdt({**p, "mark": px})
            if u is not None:
                rows[f"{mode}|{p.get('key', '')}"] = u
            px_by_pos[f"{mode}|{p.get('key', '')}"] = px
    return {"source": source, "rows": {k: round(v, 4) for k, v in rows.items()},
            "total": round(sum(rows.values()), 4) if rows else None, "prices": px_by_pos}


def _short(side, perp: bool = False) -> str:
    """The futures badge: every short is a perpetual; a long is one when it opened in Futures mode."""
    if side == "sell":
        return ' <span class="badge warn">short (perp)</span>'
    return ' <span class="badge">long (perp)</span>' if perp else ""


def _prob(p) -> str:
    return "-" if p is None else f"{p:.2f}"


def _scan_steps(ws: str) -> str:
    name = "Futures AI" if ws == "futures" else "Spot AI"
    return (f'<details class="card"><summary><b>How {name} scans and takes a trade</b></summary><ol class="small">'
            "<li>About once a minute it looks for charts whose candle has just closed, for every strategy marked "
            "paper on every pair in its list and every timeframe ticked on the Trading page.</li>"
            "<li>Each strategy's own rules run on that candle. Most candles give no signal.</li>"
            "<li>A signal then has to pass, in order: fresh prices, the engine's limits and loss breakers, "
            "max longs, shorts and pairs moving together, a stop safe from liquidation, fees, Min reward, the "
            "4h/1d trend check (if on), market context and news, the AI filter and the backtest teacher "
            "(if turned on), and how many strategies must agree (when that rule is set).</li>"
            "<li>A signal that passes opens a paper trade. The real-money trader copies it only for an approved "
            "strategy with live trading unlocked, after its own risk checks.</li></ol>"
            '<p class="small muted">Scans below are the passes that read new candles, newest first. Signals show '
            "every one of the last 500 and the check that stopped it. The page refreshes every 30 seconds.</p></details>")


def _scans_table(paper: dict, ws: str, now_ms: int, limit: int = 60) -> str:
    scans = [s for s in paper.get("scans", []) if isinstance(s, dict)]
    lp = paper.get("last_pass") or {}
    head = (f'<p class="small muted">Last check {_ms(lp.get("at"))}: {lp.get("strategies", 0)} strategies on '
            f'{lp.get("pairs", 0)} pairs.</p>' if lp else "")
    if not scans:
        return head + ('<p class="muted">No scan logged yet. Scans are logged from this update on, each time '
                       "a candle closes.</p>")
    rows = []
    for s in reversed(scans[-limit:]):
        e = (s.get("engines") or {}).get(ws) or {}
        why = e.get("why") or {}
        reasons = ", ".join(f"{k}: {n}" for k, n in sorted(why.items(), key=lambda kv: -kv[1]))
        if s.get("blocked") and not e.get("charts"):
            what = f'<span class="bad">no new entries: {_e(s["blocked"])}</span>'
        elif not e.get("charts"):
            what = '<span class="muted">no new candle for this engine</span>'
        else:
            what = (f'{e["charts"]} charts checked · '
                    + (f'<b class="good">{e.get("opened", 0)} opened</b>' if e.get("opened") else "0 opened")
                    + (f' · <span class="warn">{e["skipped"]} skipped</span> ({_e(reasons)})' if e.get("skipped")
                       else "" if e.get("opened") else " · no signal"))
        rows.append(f'<tr><td>{_ms(s.get("at"))}</td><td class="small">{s.get("strategies", 0)} strategies, '
                    f'{s.get("pairs", 0)} pairs</td><td class="small">{what}</td></tr>')
    return head + f"<table><tr><th>Scan</th><th>Looked at</th><th>Result</th></tr>{''.join(rows)}</table>"


def _signal_log(paper: dict, show: str, limit: int = 300) -> str:
    sigs = [s for s in paper.get("signals", []) if isinstance(s, dict)]
    tabs = " ".join(f'<a class="seg{" active" if show == v else ""}" href="/scan-log?show={v}">{label}</a>'
                    for v, label in (("all", "All"), ("opened", "Opened"), ("skipped", "Skipped")))
    if show == "opened":
        sigs = [s for s in sigs if not s.get("skipped")]
    elif show == "skipped":
        sigs = [s for s in sigs if s.get("skipped")]
    if not sigs:
        return f'<div class="segs" aria-label="filter">{tabs}</div><p class="muted">No signals here yet.</p>'
    rows = []
    for s in reversed(sigs[-limit:]):
        sid, _ex, sym, tf = (s.get("key", "").split("|") + ["", "", "", ""])[:4]
        rows.append(f"<tr><td>{_ms(s.get('ts'))}</td><td>{_e(sym)} {_e(tf)}"
                    f"{_short(s.get('side'), s.get('key', '').endswith('|futures'))}</td>"
                    f"<td class=small>{_e(sid)}</td><td>{s.get('entry', 0):.6g} / {s.get('stop', 0):.6g}</td>"
                    f'<td class="small {"good" if not s.get("skipped") else "warn"}">'
                    f'{_e(s.get("skipped") or "opened")}'
                    f'{"<br>" + _e(s["detail"]) if s.get("detail") else ""}</td></tr>')
    return (f'<div class="segs" aria-label="filter">{tabs}</div><table><tr><th>Signal</th><th>Market</th><th>Strategy</th>'
            f"<th>Entry / stop</th><th>What happened</th></tr>{''.join(rows)}</table>")


def _signals_section(paper: dict, now_ms: int, limit: int = 15) -> str:
    """Why trades open or don't: every paper signal of the last 24 hours and what became of it."""
    day = [s for s in paper.get("signals", []) if s.get("ts", 0) >= now_ms - 86_400_000]
    blocked = (paper.get("blocked") or {}).get("why")
    head = (f'<p class="bad">New paper entries are blocked right now by {_e(blocked)}.</p>' if blocked else "")
    lp = paper.get("last_pass") or {}
    if lp and blocked and not lp.get("markets"):  # a blocked pass doesn't read the charts
        head += (f'<p class="small muted">Last check {_ms(lp.get("at"))}: {lp.get("strategies", 0)} strategies '
                 f'on {lp.get("pairs", 0)} pairs; the charts are read again once new entries are allowed.</p>')
    elif lp:
        age = (now_ms - lp["newest_candle"]) / 60_000 if lp.get("newest_candle") else None
        stale = age is None or age > 90
        head += (f'<p class="small {"bad" if stale or not lp.get("strategies") else "muted"}">Last check '
                 f'{_ms(lp.get("at"))}: {lp.get("strategies", 0)} strategies on {lp.get("pairs", 0)} pairs '
                 f'({lp.get("markets", 0)} pair/timeframe charts)'
                 + (f', {lp["broken"]} could not be built' if lp.get("broken") else "")
                 + (", <b>no candles found</b>: is the collector running?" if age is None else
                    f", newest candle closed {age:.0f} min ago" + (" (<b>prices are not updating</b>: check the "
                                                                    "collector)" if stale else ""))
                 + ".</p>")
    if not day:
        return head + ('<p class="muted">No strategy gave a signal in the last 24 hours. A strategy signals only '
                       "when its rules match on a candle that has just closed; a quiet market can mean hours "
                       "without one.</p>")
    opened = sum(1 for s in day if not s.get("skipped"))
    why: dict[str, int] = {}
    for s in day:
        if s.get("skipped"):
            why[s["skipped"]] = why.get(s["skipped"], 0) + 1
    parts = ", ".join(f"{k}: {n}" for k, n in sorted(why.items(), key=lambda kv: -kv[1]))
    summary = (f"<p>Last 24 hours: <b>{len(day)}</b> signals, <b class=\"good\">{opened}</b> opened"
               + (f", <b class=\"warn\">{len(day) - opened}</b> skipped ({_e(parts)})" if why else "") + ".</p>")
    rows = []
    for s in reversed(day[-limit:]):
        _sid, _ex, sym, tf = (s.get("key", "").split("|") + ["", "", "", ""])[:4]
        rows.append(f"<tr><td>{_ms(s.get('ts'))}</td><td>{_e(sym)} {_e(tf)}{_short(s.get('side'), s.get('key', '').endswith('|futures'))}</td>"
                    f"<td class=small>{_e(_sid)}</td><td>{s.get('entry', 0):.6g} / {s.get('stop', 0):.6g}</td>"
                    f'<td class="small {"good" if not s.get("skipped") else "warn"}">'
                    f'{_e(s.get("skipped") or "opened")}'
                    f'{"<br>" + _e(s["detail"]) if s.get("detail") else ""}</td></tr>')
    return (head + summary + "<table><tr><th>Signal</th><th>Market</th><th>Strategy</th><th>Entry / stop</th>"
            f"<th>What happened</th></tr>{''.join(rows)}</table>"
            '<p class="small"><a href="/scan-log">Full scan log: every scan and signal</a></p>')


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
        out.append(f"<tr><td>{_ms(t.get('exit_ts'))}</td><td>{_e(mode)}</td><td>{_e(t.get('symbol', ''))}{_short(t.get('side'), t.get('perp'))}</td>"
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
        return "waiting", "warn", str(last.get("text", ""))[:120]
    if last.get("level") == "error":
        return "error", "bad", str(last.get("text", ""))[:120]
    if fresh:
        return "running", "good", f"last check-in {_ms(beats[-1].get('ts'))}"
    return "quiet", "warn", f"last seen {_ms(last.get('ts'))}"


def _trading_stats(c: dict, settings, paths: Paths, paper: dict, events: list[dict], acct: dict,
                   ws: str = "spot") -> str:
    if c["killed"]:
        trading = ("Kill switch on", "bad")
    elif c.get("halted"):
        trading = ("Safety halt", "bad")
    elif c["paused"]:
        trading = ("Paused", "warn")
    else:
        trading = ("Running", "good")
    trades = paper.get("trades", [])
    pnl = sum(t.get("pnl_usdt", 0) for t in trades)
    in_paper = _trading_count(paths, ws)
    from ..learn import wallet

    if not c["killed"] and not c["paused"] and not wallet.engines(wallet.engines_path(paths.paper_state))[ws]:
        trading = (f"{'Futures' if ws == 'futures' else 'Spot'} AI off", "warn")
    tiles = [
        ("Trading", trading[0], trading[1], "new entries" if not c["paused"] and not c["killed"] else ""),
        ("Mode", settings.mode.value, "", "live orders locked" if live_gate_problems(settings) else "LIVE UNLOCKED"),
        *([] if ws == "futures" else [_account_tile(acct)]),  # the real spot balance: Spot workspace only
        ("Strategies trading", str(in_paper), "good" if in_paper else "warn", "in paper trading"),
        (f"{'Futures' if ws == 'futures' else 'Spot'} AI paper P&L", f"{pnl:+,.2f}", "good" if pnl > 0 else "bad" if pnl < 0 else "",
         f"{len(paper.get('open', {}))} open · {len(trades)} closed"),
    ]
    return '<div class="stats">' + "".join(
        f'<div class="stat"><span class="k">{_e(k)}</span><span class="v {cls}">{_e(v)}</span>'
        f'<span class="s">{_e(sub)}</span></div>' for k, v, cls, sub in tiles) + "</div>"


def _usdt(v) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _account_tile(acct: dict) -> tuple[str, str, str, str]:
    st = acct.get("state")
    if st == "ok":
        return ("Bybit account", f"{acct['total_usdt']:,.2f}", "warn" if acct.get("stale") else "",
                "USDT, last refresh failed" if acct.get("stale") else "USDT, real balance")
    if st == "no_key":
        return ("Bybit account", "no key", "warn", "save it on Settings")
    if st == "loading":
        return ("Bybit account", "loading", "warn", "reload in a moment")
    return ("Bybit account", "error", "bad", acct.get("note", ""))


def _ago(at: float, now: float) -> str:
    s = max(0, int(now - at))
    if s < 90:
        return f"{s} seconds ago"
    if s < 5400:
        return f"{round(s / 60)} minutes ago"
    return f"{s / 3600:.1f} hours ago"


def _account_card(acct: dict, csrf: str = "", now: float | None = None) -> str:
    """The real Bybit account, read only: what it holds and what that is worth in USDT."""
    st = acct.get("state")
    now = time.time() if now is None else now
    head = '<div class="card"><h3>Bybit real account (read only)</h3>'
    again = (f'<form method="post" action="/trading/account/refresh">{csrf}<button class="ghost">Refresh now'
             '</button></form>') if csrf else ""
    if st == "no_key":
        return (head + '<p class="small">Save your real-account key on the <a href="/settings">Settings</a> page '
                'to see your Bybit balance here. Paper trading doesn\'t need it.</p></div>')
    if st == "loading":
        return head + '<p class="small muted">Asking Bybit for the balance; reload in a moment.</p></div>'
    if st != "ok":
        return head + f'<p class="bad small">{_e(acct.get("note", ""))}</p>{again}</div>'
    rows = "".join(f'<tr><td>{_e(c["asset"])}</td><td class="num">{c["amount"]:,.8g}</td>'
                   f'<td class="num">{_usdt(c["usdt"])}</td></tr>' for c in acct["coins"])
    table = (f'<table><tr><th>Coin</th><th>Amount</th><th>USDT</th></tr>{rows}</table>' if rows
             else '<p class="muted small">The spot account is empty.</p>')
    notes = "".join(f'<p class="bad small">{_e(n)}</p>' for n in (
        f'Last refresh failed ({acct["stale"]}); these are the numbers from {_ago(acct["at"], now)}.'
        if acct.get("stale") else "",
        f'{acct["price_note"]}; coins without a price count as 0 in the total.' if acct.get("price_note") else "")
        if n)
    return (head + f'<p>Total <b>{acct["total_usdt"]:,.2f} USDT</b> '
            f'<span class="muted small">updated {_ago(acct["at"], now)}</span></p>{notes}'
            f'{table}<p class="small muted">Read from Bybit with your saved key once a minute (this page reloads '
            'itself every minute). This card can\'t trade; real orders need the live lock on the server.</p>'
            f'{again}</div>')


def _trading_count(paths: Paths, ws: str = "spot") -> int:
    """How many strategies ``ws``'s engine opens paper trades for (only the WIN ones when its switch is on)."""
    from ..learn import Registry, picks

    try:
        from ..learn.keep import kept_ids

        only, kept = picks.winners_only(paths.registry, ws), kept_ids(paths.registry, ws)
        return sum(1 for e in Registry(paths.registry).entries.values()
                   if e.status == "paper" and not (ws == "spot" and e.kind == "short")
                   and picks.trades_now(e, only, ws, kept))
    except Exception:
        return 0


def _paper_switch_card(c: dict, paths: Paths, events: list[dict], jobs: list[dict], csrf: str,
                       ws: str = "spot") -> str:
    """The paper-trading switch of the workspace's engine: on, off and its strategy choices. The
    other engine's switch is only on its own workspace."""
    from ..learn import Registry, picks, wallet

    name = "Futures AI" if ws == "futures" else "Spot AI"
    other = "Spot AI" if ws == "futures" else "Futures AI"
    in_paper = _trading_count(paths, ws)
    try:
        wins = sum(1 for e in Registry(paths.registry).entries.values()
                   if e.status == "paper" and picks.pick_market(e) == ws)
    except Exception:
        wins = 0
    only = picks.winners_only(paths.registry, ws)
    engine_on = wallet.engines(wallet.engines_path(paths.paper_state))[ws]
    svc, svc_cls, svc_detail = _service_state(events, "paper", int(time.time() * 1000))
    starting = next((j for j in jobs if j.get("meta", {}).get("kind") == "paper-start"
                     and j["status"] in ("queued", "running")), None)
    from ..trade.control import Control

    if c["killed"]:
        state, cls, why = "OFF", "bad", "the kill switch is on"
    elif c["paused"] and Control._manual(c):
        state, cls, why = "OFF", "bad", "everything is paused under Controls"
    elif not engine_on:
        state, cls, why = "OFF", "bad", f"turned off here; {other} is not affected"
    elif starting:
        state, cls, why = "STARTING", "warn", f'loading strategies and price history (<a href="/jobs/{_e(starting["id"])}">progress</a>)'
    elif not in_paper and only:
        state, cls, why = "OFF", "bad", ("WIN strategies only is on, but there are none yet: press Keep only "
                                         "the winners on the Replay page")
    elif not in_paper:
        state, cls, why = "OFF", "bad", "no strategy is paper trading yet"
    elif svc_cls != "good":
        state, cls, why = "ON", "warn", f"but the paper service is {_e(svc)} ({_e(svc_detail)})"
    else:
        state, cls, why = "ON", "good", (f"{in_paper} WIN strategies trading with fake money" if only
                                         else f"{in_paper} strategies trading with fake money")
    if state == "ON" and c["paused"]:  # an automatic live pause: paper keeps going
        why += "; live trading is paused (see Controls), paper keeps collecting results"
    on = f"Turn {name} paper trading on" if state != "ON" else "Reload every strategy"
    hidden = f'<input type="hidden" name="market" value="{ws}">'
    from ..learn.keep import MAX_N, MIN_N, saved_n

    keep_n = saved_n(paths.registry, ws)
    return (f'<div class="card"><h3>{name} paper trading (fake money)</h3><p>{name} paper trading '
            f'<b class="{cls}">{state}</b>: {why}</p>'
            f'<p class="small muted">Off stops new {name} paper trades only; its open positions still run to their '
            f'exits and {other} keeps trading. On starts {name} again, and loads every strategy first when none is '
            f'paper trading yet. Reload every strategy loads the strategy library, downloads a year of history, '
            f'backtests every strategy and ranks them for {name} only.</p>'
            f'<div class="row"><form method="post" action="/trading/paper/on">{csrf}{hidden}<button>{on}</button></form>'
            f'<form method="post" action="/trading/paper/off">{csrf}{hidden}<button class="ghost">'
            f'Turn {name} paper trading off</button></form>'
            f'</div><p class="small muted">Keep only the best: {name} ranks the strategies by their backtest and '
            f'its own paper results only (ones that passed the backtest first, then result per trade, with few '
            f'trades counting for less{"; shorts included" if ws == "futures" else "; longs only"}). Its best '
            f'trade on {name} and the rest stop opening trades there; every reload ranks them again, so one can '
            f'come back. {other} has its own ranking: a strategy kept by one engine never trades on the other '
            'because of it.</p>'
            f'<form method="post" action="/trading/paper/keep" class="row">{csrf}{hidden}'
            f'<label class="small">Keep the best <input type="number" name="n" min="{MIN_N}" max="{MAX_N}" '
            f'value="{keep_n}" style="width:4.5em"> strategies</label><button class="ghost">Keep only the best</button></form>'
            + _winners_only_form(only, wins, csrf, ws) + '</div>')


def _futures_state(paths: Paths) -> tuple[bool, float]:
    """(Futures mode on, the leverage paper perpetuals use)."""
    from ..learn import wallet
    from ..trade import futures

    path = wallet.market_path(paths.paper_state)
    chosen = wallet.paper_leverage(path)
    return wallet.futures_on(path), futures.clamp_paper_leverage(chosen) if chosen else futures.leverage()


def _market_switch(paths: Paths, auth, token, title: str) -> str:
    """The Spot / Futures switch in the header of every page."""
    try:
        on, lev = _futures_state(paths)
    except Exception:
        return ""
    back = next((href for href, name in NAV if name == title), "/trading")
    csrf = _csrf(auth, token)
    spot = '<button class="seg{}" name="market" value="spot">Spot AI</button>'.format("" if on else " active")
    fut = '<button class="seg{}" name="market" value="futures">Futures AI{}</button>'.format(
        " active" if on else "", f" {lev:g}x" if on else "")
    return (f'<form method="post" action="/market-mode" class="market-switch" aria-label="spot or futures">{csrf}'
            f'<input type="hidden" name="back" value="{_e(back)}">{spot}{fut}</form>')


def _guards_text(g: dict) -> str:
    parts = [f"{g['trade_usdt']:,.2f} USDT per trade" if g["trade_usdt"] else "trade size from the strategy's risk",
             f"pause {g['pause_hours']:g}h after a {g['day_pause_pct']:g}% day loss, close all at a "
             f"{g['week_close_pct']:g}% week loss" if g["breaker"] else "loss breakers off",
             f"minimum reward {g['min_rr']:g}R" if g["min_rr"] else "no minimum reward",
             "higher-timeframe trend check on" if g["trend"] else "no trend check",
             ("split take-profit on, then breakeven stop" if g["breakeven"] else "split take-profit on") if g["split_tp"]
             else "one take-profit",
             "limit (maker) entries" if g["maker_entry"] else "market entries",
             f"a trade only when {g['agree']:g} strategies agree" if g["agree"] > 1 else "every strategy trades alone",
             f"at most {g['max_long']:g} longs and {g['max_short']:g} shorts, {g['max_correlated']:g} that move "
             f"together (correlation {g['corr_min']:g}+)"]
    return ", ".join(parts)


def _amount_form(paths: Paths, csrf: str, ws: str) -> str:
    """The amount each new trade uses, with its own Save button."""
    from ..learn import guards

    amt = guards.load(guards.path_for(paths.paper_state))[ws]["trade_usdt"]
    lo, hi = guards.RANGES["trade_usdt"]
    now = f"now {amt:,.2f} USDT per trade" if amt else "now automatic"
    if ws == "futures" and amt:  # the amount is the margin: the position is that times the leverage
        on, lev = _futures_state(paths)
        now = (f"now {amt:,.2f} USDT margin × {lev:g}x = {amt * lev:,.2f} USDT position per trade "
               "(less when a wide stop needs lower leverage)")
    return (f'<form method="post" action="/trading/paper/amount" class="row">{csrf}'
            f'<input type="hidden" name="market" value="{ws}">'
            f'<label class="small">Amount per trade USDT (0 auto) <input name="trade_usdt" type="number" '
            f'inputmode="decimal" step="any" min="0" max="{hi:g}" value="{amt:g}" style="width:6em"></label>'
            f'<button>Save amount</button> <span class="small muted">{now}</span></form>')


def _timeframes_form(paths: Paths, csrf: str, ws: str) -> str:
    """Which candle timeframes this engine opens new trades on."""
    from ..learn import guards

    on = guards.load(guards.path_for(paths.paper_state))[ws]["timeframes"]
    boxes = " ".join(f'<label class="small"><input type="checkbox" name="tf" value="{t}"'
                     f'{" checked" if t in on else ""}> {t}</label>' for t in guards.TIMEFRAMES)
    now = "every timeframe" if len(on) == len(guards.TIMEFRAMES) else "only " + ", ".join(on)
    return (f'<form method="post" action="/trading/paper/timeframes" class="row">{csrf}'
            f'<input type="hidden" name="market" value="{ws}">'
            f'<span class="small">Trade on</span> {boxes} <button class="ghost">Set timeframes</button> '
            f'<span class="small muted">now {now}</span></form>')


def _guards_form(paths: Paths, csrf: str, ws: str) -> str:
    """The engine's extra rules: loss breakers, minimum reward, trend check, split take-profit."""
    from ..learn import guards

    g = guards.load(guards.path_for(paths.paper_state))[ws]

    def box(name, label):
        return (f'<label class="small"><input type="checkbox" name="{name}" value="1"'
                f'{" checked" if g[name] else ""}> {label}</label>')

    def num(name, label, step="any"):
        lo, hi = guards.RANGES[name]
        lo = 0 if name in guards.OFF_AT_ZERO else lo
        return (f'<label class="small">{label} <input name="{name}" type="number" inputmode="decimal" step="{step}" '
                f'min="{lo:g}" max="{hi:g}" value="{g[name]:g}" style="width:4.5em"></label>')

    return (f'<form method="post" action="/trading/paper/guards" class="row">{csrf}'
            f'<input type="hidden" name="market" value="{ws}">'
            f'{box("breaker", "Loss breakers")} {num("day_pause_pct", "Pause at day loss %")} '
            f'{num("pause_hours", "for hours")} {num("week_close_pct", "Close all at week loss %")} '
            f'{num("min_rr", "Min reward R (0 off)")} {box("trend", "Trade with the 4h/1d trend")} '
            f'{box("split_tp", "Half at halfway, half at target")} '
            f'{box("breakeven", "Then stop to breakeven")} '
            f'{box("maker_entry", "Enter with limit orders (maker fee)")} '
            f'{num("max_long", "Max longs", "1")} {num("max_short", "Max shorts", "1")} '
            f'{num("max_correlated", "Max moving together", "1")} {num("corr_min", "at correlation", "0.05")} '
            f'{num("agree", "Strategies that must agree (1 off)", "1")}'
            '<button class="ghost">Set rules</button></form>'
            '<p class="small muted">Amount per trade: 0 sizes each trade from the strategy\'s risk (at most half '
            'the wallet). Any other amount (5 USDT or more) is what each new trade uses: the purchase amount on Spot, '
            'the margin on Futures, so on Futures the position is that amount times the leverage and so is the loss '
            'at the stop. A trade never uses more than the wallet has free. Open trades keep their size. '
            'Loss breakers count closed and open trades: past the day loss the engine opens '
            'nothing new for the hours set; past the week loss (from Monday midnight) it closes all its positions and '
            'opens nothing new until Monday. Min reward skips signals whose target is closer than that many times '
            'the risk (signals that exit on their own rule are not judged). Max longs and shorts count open positions '
            'of each side; max moving together counts open positions on the same side whose hourly prices over the '
            'last week moved with the new pair (at the correlation set), so one Bitcoin move can\'t stop them all '
            'at once. The trend check skips a long while the '
            '4h or daily trend points down, and a short while it points up. With "Then stop to breakeven" (it needs '
            'the split take-profit), once the first half is taken the rest\'s stop moves to the entry price plus fees, '
            'so the trade can no longer end in a loss, but it is stopped out more often on a pullback. Replay uses the '
            'minimum reward, the split take-profit and the breakeven stop too, so you can test them there first. Limit entries pay the maker fee (0.02% on '
            'perps instead of 0.055% plus slippage) but miss the trade when the entry candle never comes back to '
            'the signal price; Replay shows both effects. Strategies that must agree: with 3, a coin is only traded '
            'when 3 different strategies signal the same side on it at the same candle close, and then as one trade '
            '(the one with the furthest target); 1 lets every strategy trade on its own. Real-money trades copy '
            'these paper trades, so they follow it too.</p>')


def _market_card(paths: Paths, paper: dict, csrf: str, ws: str = "spot") -> str:
    """The engine of the workspace shown: Spot AI (on/off, limits) or Futures AI (on/off, limits,
    leverage). The other engine's controls are only on its own workspace."""
    from ..learn import wallet
    from ..trade import futures

    _on, lev = _futures_state(paths)
    on = wallet.engines(wallet.engines_path(paths.paper_state))[ws]
    lim = wallet.limits(wallet.limits_path(paths.paper_state))[ws]
    now_ms = int(time.time() * 1000)
    st = paper if isinstance(paper, dict) else {}
    mine = [p for k, p in (st.get("open") or {}).items()
            if p.get("filled") and (wallet.market_of(p) == "futures" or k.endswith("|futures")) == (ws == "futures")]
    name = "Futures AI" if ws == "futures" else "Spot AI"
    from ..learn import guards

    block = (wallet.entry_block(st, wallet.load(wallet.wallet_path(paths.paper_state, ws)), ws, lim, now_ms)
             or guards.block(st, ws, now_ms))
    state = ('<b class="bad">OFF</b>' if not on else f'<b class="warn">PAUSED</b> ({_e(block)})' if block
             else '<b class="good">ON</b>')
    today = wallet.today_pnl(st, ws, now_ms, wallet.load(wallet.wallet_path(paths.paper_state, ws))["since"])
    if ws == "futures":
        margin = sum(p.get("units", 0) * p.get("entry", 0) / p.get("leverage", 1.0) for p in mine)
        longs = sum(1 for p in mine if (p.get("signal") or {}).get("side") != "sell")
        what = (f"USDT perpetual futures, long and short, at <b>{lev:g}x</b> isolated margin. {len(mine)} open "
                f"({longs} long, {len(mine) - longs} short), margin in use {margin:,.2f} USDT.")
        from ..market import gainers

        from ..market import squeeze

        hot = list(dict.fromkeys([*gainers.symbols(gainers.path_for("futures")),
                                  *squeeze.symbols(squeeze.path_from_env())]))
        what += (f" It trades only the Futures top gainers and squeeze pairs: {_e(', '.join(hot))}." if hot else
                 ' <b class="warn">It opens nothing new now: there is no fresh Futures top gainers or squeeze list. '
                 'Turn one on on the Pairs page.</b>')
    else:
        what = f"buys coins, long only, no leverage, never short. {len(mine)} open."
    btn = (f'<input type="hidden" name="market" value="{ws}"><input type="hidden" name="on" value="{"0" if on else "1"}">'
           f'<button class="{"ghost" if on else ""}">Turn {name} {"off" if on else "on"}</button>')
    out = (f'<div class="card"><h3>{name}</h3>'
           f'<p>{state}: {what} Today <span class="{"good" if today >= 0 else "bad"}">{today:+,.2f} USDT</span>.</p>'
           f'<form method="post" action="/trading/paper/market" class="row">{csrf}{btn}</form>'
           f'<form method="post" action="/trading/paper/limits" class="row">{csrf}'
           f'<input type="hidden" name="market" value="{ws}">'
           f'<label class="small">Daily loss limit % <input name="loss" type="number" inputmode="decimal" step="any" '
           f'min="{wallet.DAILY_LOSS_RANGE[0]:g}" max="{wallet.DAILY_LOSS_RANGE[1]:g}" value="{lim["daily_loss_pct"]:g}" '
           'style="width:5em"></label>'
           f'<label class="small">Max open <input name="max_open" type="number" min="{wallet.MAX_OPEN_RANGE[0]}" '
           f'max="{wallet.MAX_OPEN_RANGE[1]}" value="{lim["max_open"]}" style="width:5em"></label>'
           '<button class="ghost">Set limits</button></form>')
    out += _amount_form(paths, csrf, ws)
    out += _timeframes_form(paths, csrf, ws)
    out += _guards_form(paths, csrf, ws)
    if ws == "futures":
        choices = sorted(set(futures.PAPER_LEVERAGE_CHOICES) | {lev})
        opts = "".join(f'<option value="{x:g}"{" selected" if abs(x - lev) < 1e-9 else ""}>{x:g}x</option>'
                       for x in choices)
        out += (f'<form method="post" action="/trading/paper/leverage" class="row">{csrf}'
                f'<label class="small">Leverage <select name="lev" style="width:6em">{opts}</select></label>'
                '<button class="ghost">Set leverage</button></form>'
                '<p class="small muted">Futures AI opens a perpetual long on a long signal and a perpetual short on '
                'a short signal. Leverage choices are Bybit\'s (1x to 100x), cut to each coin\'s own Bybit maximum. '
                'Higher leverage does not change the risk per trade (the stop sets that) but brings liquidation much '
                'closer. A trade whose stop is too wide for the leverage chosen uses less (the most at which the '
                'stop comes at most halfway to liquidation), so it still opens with the same risk. Paper only: the '
                'live trader keeps its '
                f'{futures.MAX_LEVERAGE:g}x safety limit and stays locked.</p>')
    else:
        out += ('<p class="small muted">Spot AI buys the coin on a long signal and sells it at the exit. It ignores '
                'short signals, which only Futures AI trades.</p>')
    out += ('<p class="small muted">It stops opening trades for the rest of the day (until midnight) once it has '
            'lost its daily limit, and while it holds its maximum of open positions. Turning it off stops new '
            f'trades; open ones still run to their exits. Its controls are only here, on the {name} workspace.</p></div>')
    return out


def _winners_only_form(only: bool, wins: int, csrf: str, ws: str = "spot") -> str:
    """The workspace engine's "WIN strategies only" switch on the paper-trading card."""
    name = "Futures AI" if ws == "futures" else "Spot AI"
    hidden = f'<input type="hidden" name="market" value="{ws}">'
    if only:
        head = (f'<p><b>{name} WIN strategies only: <span class="good">ON</span></b>. Only the {wins} WIN strategies '
                f'open {name} trades.')
        btn = hidden + '<input type="hidden" name="on" value="0"><button class="ghost">Trade the best strategies too</button>'
        if not wins:
            head += (f'</p><p class="warn">{name} has no WIN strategies yet, so it opens no new trades. On the Replay page '
                     f'in the {name} view press Keep the winners on every timeframe, or turn this off.')
    else:
        head = (f'<p><b>{name} WIN strategies only: <span class="muted">OFF</span></b>. The best strategies and the '
                f'{wins} WIN strategies all open {name} trades.')
        btn = hidden + '<input type="hidden" name="on" value="1"><button>Trade only the WIN strategies</button>'
    return (head + f'</p><p class="small muted">{name}\'s WIN strategies are the ones kept by Keep only the winners on '
            f'the Replay page while it shows the {name} workspace (the other engine\'s picks never trade here). '
            f'When this is on, the best strategies above stop opening {name} trades; their open trades still '
            'close normally. The other engine has its own switch on its workspace.</p>'
            f'<form method="post" action="/trading/paper/winners-only" class="row">{csrf}{btn}</form>')


def _live_card(settings, paths: Paths, events: list[dict]) -> str:
    """Live (real money) readiness. It is switched on only on the server, never from the website."""
    rep = _load_json(os.path.join(paths.golive_dir, "report.json"))
    from ..trade.golive import approval_intact

    approval = _load_json(os.path.join(paths.golive_dir, "approval.json"))
    svc, svc_cls, _detail = _service_state(events, "executor-live", int(time.time() * 1000))
    checks = [
        ("Real Bybit keys saved (spot trade only, no withdrawals, IP-bound)",
         bool(os.environ.get("BYBIT_API_KEY") and os.environ.get("BYBIT_API_SECRET"))),
        ("Go/no-go gate passed", bool(rep.get("passed"))),
        ("Strategies approved (aitrader golive approve)", approval_intact(approval)),
        ("Pilot budget set (AITRADER_PILOT_MAX_USDT in .env)", bool(os.environ.get("AITRADER_PILOT_MAX_USDT"))),
        ("Live lock opened in .env on the server", not live_gate_problems(settings)),
        ("Live trader running", svc_cls == "good"),
    ]
    on = all(ok for _n, ok in checks)
    rows = "".join(f'<li class="{"good" if ok else "bad"}">{"✓" if ok else "✗"} {_e(n)}</li>' for n, ok in checks)
    return (f'<div class="card"><h3>Live trading (real money)</h3><p>Live trading <b class="{"good" if on else "bad"}">'
            f'{"ON" if on else "OFF"}</b></p><ul class="small gate">{rows}</ul>'
            '<p class="small">There is no switch for this on the website, on purpose: anyone who got your '
            'dashboard password could otherwise trade your real money. When the first four lines are '
            'ticked, turn it on from the server: in <code>~/AI_crypto_trader/.env</code> set '
            '<code>AITRADER_MODE=live</code>, <code>AITRADER_LIVE_UNLOCKED=true</code> and '
            '<code>AITRADER_LIVE_CONFIRM=I accept real-money risk</code>, then run '
            '<code>docker compose up -d</code> and <code>docker compose --profile live up -d executor-live</code>. '
            'To stop it at once, pull the kill switch below; on the server, '
            '<code>docker compose stop executor-live</code>. See docs/live-trading.md.</p></div>')


def _equity_svg(paper: dict, execs: dict) -> str:
    """Inline SVG line chart: paper P&L (cumulative, from closed trades) and executor equity."""
    series = []
    pts, total = [], 0.0
    for t in sorted(paper.get("trades", []), key=lambda t: t.get("exit_ts", 0)):
        total += t.get("pnl_usdt", 0)
        pts.append((t.get("exit_ts", 0), total))
    if pts:
        series.append(("paper P&L (USDT)", pts, "var(--accent)"))
    colors = {"live": "var(--bad)"}
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
    from ..trade.golive import approval_intact

    approval = _load_json(os.path.join(paths.golive_dir, "approval.json"))
    if rep:
        rows = "".join(f'<li class="{"good" if c["passed"] else "bad"}">{"✓" if c["passed"] else "✗"} '
                       f'{_e(c["name"])}: {_e(c["value"])}</li>' for c in rep.get("criteria", []))
        head = (f'<b class="{"good" if rep.get("passed") else "bad"}">'
                f'{"passes" if rep.get("passed") else "does not pass yet"}</b> (checked {_when(rep.get("at"))})')
    else:
        rows, head = "", "not checked yet"
    appr = (f'<br>Approved by {_e(approval["by"])} for {_e(", ".join(approval.get("strategies", {})))}'
            if approval_intact(approval) else
            ('<br><b class="bad">The approval file was edited or not made by <code>golive approve</code>: '
             'it counts for nothing</b>' if approval else '') + "<br>No live approval (approve on the server: <code>aitrader golive approve</code>)")
    return (f'<div class="card"><h3>Go/no-go gate (Phase 6)</h3><p>{head}{appr}</p><ul class="small gate">{rows}</ul>'
            f'<form method="post" action="/trading/gate">{csrf}<button class="ghost">Check the gate now</button></form></div>')


CSS = """
:root{color-scheme:dark;--bg:#0b0e11;--bg2:#0b0e11;--fg:#eaecef;--muted:#848e9c;--card:#161a1e;--card2:#1e2329;--line:#2b3139;
--good:#0ecb81;--bad:#f6465d;--warnc:#f0b90b;--accent:#f7a600;--accent2:#ffb92e;--on-accent:#0b0e11;--link:#fcd535;
--radius:10px;--shadow:none;--mono:ui-monospace,"SF Mono","Roboto Mono","Cascadia Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums;min-height:100vh;display:flex;flex-direction:column}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;display:flex;gap:10px 18px;align-items:center;flex-wrap:wrap;padding:10px 20px;
background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:1.08em;color:var(--fg);letter-spacing:.2px}
.brand:hover{text-decoration:none}
.logo{width:22px;height:22px;display:inline-flex;color:var(--accent)}.logo svg{width:22px;height:22px}
.mode{font-size:.72em;font-weight:600;text-transform:uppercase;letter-spacing:.6px;padding:3px 9px;border-radius:99px;
border:1px solid var(--line);color:var(--muted)}
.mode-paper{color:var(--warnc);border-color:color-mix(in srgb,var(--warnc) 45%,transparent)}
.mode-live{color:#fff;background:var(--bad);border-color:var(--bad)}
.market-switch{display:inline-flex;margin-left:8px;background:var(--card2);border-radius:8px;padding:3px;gap:2px}
.market-switch button.seg{background:transparent;color:var(--muted);border:none;border-radius:6px;padding:6px 12px;font-size:.8em;
font-weight:600;min-height:34px}
.market-switch button.seg.active{background:var(--accent);color:var(--on-accent)}
nav{display:flex;gap:2px;flex-wrap:wrap;margin-left:auto}
nav a{color:var(--muted);padding:6px 11px;border-radius:8px;font-weight:500;font-size:.94em}
nav a:hover{color:var(--fg);background:var(--card2);text-decoration:none}
nav a.active{color:var(--fg);background:var(--card2);box-shadow:inset 0 -2px 0 var(--accent)}
.tabbar{display:none}
.who{display:flex;gap:10px;align-items:center}
main{width:100%;max-width:1200px;margin:0 auto;padding:20px;flex:1}
footer{text-align:center;padding:18px;border-top:1px solid var(--line)}
.page-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 16px}
h1{font-size:1.5em;margin:0;letter-spacing:-.2px}h2{font-size:1.12em;margin:28px 0 10px}h3{margin:0 0 8px;font-size:.98em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
/* cards of different heights stack in columns, so no empty space is left under the short ones */
.grid.pack{display:block;columns:300px;column-gap:14px}
.grid.pack>*{break-inside:avoid;margin:0 0 14px;display:inline-block;width:100%;box-sizing:border-box}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px;box-shadow:var(--shadow)}
.narrow{max-width:400px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px}
.stat .k{display:block;color:var(--muted);font-size:.78em;text-transform:uppercase;letter-spacing:.5px}
.stat .v{display:block;font-size:1.25em;font-weight:600;margin-top:2px;font-family:var(--mono)}
.stat .s{display:block;font-size:.82em;color:var(--muted)}
label{display:block;margin:10px 0;font-size:.94em}label.row{display:flex;gap:8px;align-items:center}
input[type=text],input[type=password],input:not([type]),input[type=number],input[type=email],input[type=file],input[type=month],select{
display:block;width:100%;max-width:420px;margin-top:4px;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
background:var(--bg2);color:var(--fg);font:inherit}
input:focus,select:focus{outline:2px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:1px;border-color:var(--accent)}
button{padding:8px 16px;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);border-radius:8px;cursor:pointer;
font:inherit;font-weight:600;font-size:.92em;min-height:38px}
button:hover{background:var(--accent2)}button.ghost{background:transparent;color:var(--accent)}
button.ghost:hover{background:color-mix(in srgb,var(--accent) 12%,transparent)}
button.danger{background:var(--bad);border-color:var(--bad);color:#fff}button.warn-btn{background:#d97706;border-color:#d97706;color:#fff}button.danger:hover{filter:brightness(1.1)}
button.mini{padding:3px 10px;font-size:.8em;margin-top:6px;min-height:30px}
button.link,form.inline button{background:none;border:none;color:var(--link);padding:0;font-weight:500;min-height:0}
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
.row select{width:auto;margin-top:0}form.inline{display:inline;margin:0}.hidden{display:none}
.replay-form{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end}.replay-form label{margin:0}
.replay-form label.wide{flex:1 1 260px}.replay-form select,.replay-form input{margin-top:4px}.replay-form label:not(.wide) select{width:auto}
.replay-controls{align-items:center}.replay-controls input[type=range]{flex:1 1 160px;max-width:none;padding:0;margin:0}
.trade-log{list-style:none;padding:0;margin:0;max-height:320px;overflow:auto}.trade-log li{padding:6px 0;border-bottom:1px solid var(--line)}
.chips{display:flex;flex-wrap:wrap;gap:8px}.tick-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 6px 4px 12px;
border:1px solid var(--line);border-radius:99px;font-size:.9em}button.small{padding:2px 8px;font-size:.8em}
.posts{list-style:none;padding:0}.posts li{padding:10px 0;border-bottom:1px solid var(--line)}.row>label{margin:0}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:8px}.bar span{display:block;height:100%;background:#d9534f}
.bar.progress{height:12px;border-radius:6px}.bar.progress span{background:var(--accent,#4c8dff);transition:width .6s}
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
.live-status.down .dot{background:var(--bad)}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--good) 60%,transparent)}100%{box-shadow:0 0 0 8px transparent}}
.tickers{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tick{display:grid;gap:1px;text-align:left;background:var(--card);border:1px solid var(--line);color:var(--fg);
padding:10px 12px;border-radius:var(--radius);font-weight:400;transition:border-color .2s,background .4s}
.tick:hover{background:var(--card2);border-color:var(--accent)}.tick.active{border-color:var(--accent);background:var(--card2)}
.tick-sym{font-size:.8em;color:var(--muted);font-weight:600}.tick-px{font-size:1.12em;font-weight:600;font-family:var(--mono)}
.tick-ch{font-size:.85em;font-weight:600}.hot{font-size:.85em;color:#d97706;font-weight:700}.tick-vol{font-size:.75em}
.tick.up{background:color-mix(in srgb,var(--good) 14%,var(--card))}.tick.down{background:color-mix(in srgb,var(--bad) 14%,var(--card))}
.chart-card{padding:14px 14px 10px}.chart-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.chart-head h2{margin:0;font-size:1.1em}
.segs{display:inline-flex;margin-left:auto;background:var(--bg2);border:1px solid var(--line);border-radius:9px;padding:2px}
.seg{background:none;border:none;color:var(--muted);padding:5px 10px;border-radius:7px;font-weight:600;font-size:.85em}
.seg:hover{background:var(--card2);color:var(--fg)}.seg.active{background:var(--accent);color:var(--on-accent)}
.ohlc{min-height:1.6em;margin:8px 0 6px;display:flex;flex-wrap:wrap;gap:4px 8px}
#chart{display:block;width:100%;height:440px;touch-action:pan-y;cursor:grab}#chart.dragging{cursor:grabbing}
.chart-nav{display:flex;gap:6px;justify-content:flex-end;margin-top:6px}
.chart-btn{min-width:44px;min-height:40px;display:inline-flex;align-items:center;justify-content:center;padding:0 10px}.chart-btn[hidden]{display:none}
#market:not(.js) .js-only{display:none}
.studies{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--bg2);border:1px solid var(--line);color:var(--muted);
padding:4px 10px;border-radius:999px;font-weight:600;font-size:.8em}
.chip:hover{border-color:var(--accent)}.chip[aria-pressed=true]{color:var(--fg);border-color:var(--accent);background:var(--card2)}
.chip i{width:10px;height:3px;border-radius:2px;display:inline-block}
.chip small{color:var(--muted);font-size:11px}.linkish{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;font:inherit;text-decoration:underline}
.legend{display:flex;flex-wrap:wrap;gap:2px 12px;font-size:.8em;min-height:1.3em}.legend b{font-weight:600}
.market-read{margin:.5em 0 0;line-height:1.5;min-height:1.5em}
details.health summary{cursor:pointer;font-weight:600}details.health table{margin-top:12px}
td.num,.num{font-family:var(--mono)}
/* three screen sizes: laptop (side menu), tablet (one scrolling menu row), phone (that row plus a bottom tab bar) */
@media (min-width:1100px){
header{padding:0 24px;height:58px;flex-wrap:nowrap;backdrop-filter:none;background:var(--bg)}
header nav{position:fixed;top:58px;left:0;bottom:0;width:212px;margin:0;padding:14px 10px;flex-direction:column;
flex-wrap:nowrap;gap:2px;overflow-y:auto;background:var(--bg);border-right:1px solid var(--line)}
header nav a{padding:9px 12px;border-radius:8px}
header nav a.active{box-shadow:inset 3px 0 0 var(--accent)}
.who{margin-left:auto}
main,footer{margin-left:212px;width:auto}main{max-width:1320px;padding:24px 28px}
.grid{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}}
@media (max-width:1099px){
header nav{order:5;margin-left:0;width:100%;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;
border-top:1px solid var(--line);padding-top:6px}
header nav::-webkit-scrollbar{display:none}nav a{white-space:nowrap}
.who{order:2;margin-left:auto}.market-switch{order:3}
main{padding:18px 20px}.grid{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}}
@media (max-width:640px){header{padding:8px 12px;gap:8px 10px}main{padding:14px 12px}
.brand{order:0}.mode{order:1}.who .muted{display:none}.market-switch{order:4;margin-left:0}
#chart{height:340px}.segs{margin-left:0}h1{font-size:1.3em}.grid{grid-template-columns:1fr}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}
body.signed-in{padding-bottom:64px}footer{padding-bottom:76px}
.tabbar{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));position:fixed;left:0;right:0;bottom:0;z-index:6;
background:var(--bg);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.tabbar a{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 0;min-height:56px;font-size:11px;
color:var(--muted);text-decoration:none;border-radius:0}
.tabbar a:hover{background:none}.tabbar a.active{color:var(--accent);background:none;box-shadow:none}
.tabbar svg{width:22px;height:22px}}
"""
