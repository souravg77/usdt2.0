"""The Teacher page: what the backtest teacher learned (``learn.teacher``), which strategies have
won in each pair's market situation right now, and the buttons to re-teach and to turn the
lessons on or off for paper trading. Nothing here touches live trading."""

from __future__ import annotations

import html
import time
import urllib.parse

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..learn.teacher import Teacher, best_for, describe, now_situations


def _e(x) -> str:
    return html.escape(str(x))


def _pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"


def _r(x) -> str:
    return "–" if x is None else f"{x:+.3f}R"


def _check_text(chk: dict) -> str:
    w, f = chk["without"], chk["with"]
    return (f'without lessons {w["trades"]} trades, win rate {_pct(w["win_rate"])}, {_r(w["expectancy_r"])} each; '
            f'with them {f["trades"]} trades, win rate {_pct(f["win_rate"])}, {_r(f["expectancy_r"])} each')


def _lesson_rows(lessons: dict, verdict: str, limit: int) -> str:
    rows = sorted(((sid, key, x) for sid, by in lessons.items() for key, x in by.items()
                   if x["verdict"] == verdict), key=lambda t: t[2]["own"]["shrunk_r"], reverse=verdict == "trade")
    out = []
    for sid, key, x in rows[:limit]:
        chk = x.get("check") or {}
        out.append(f'<tr><td>{_e(sid)}</td><td>{_e(describe(key))}</td><td>{x["own"]["trades"]}</td>'
                   f'<td>{_pct(x["own"]["win_rate"])}</td><td>{_r(x["own"]["expectancy_r"])}</td>'
                   f'<td>{chk.get("trades", 0)} · {_r(chk.get("expectancy_r"))}</td>'
                   f'<td>{"yes" if x["exit_1r"] else ""}</td></tr>')
    if not out:
        return '<p class="muted">None yet.</p>'
    return ('<table><tr><th>Strategy</th><th>Market situation</th><th>Trades</th><th>Win rate</th>'
            '<th>Per trade</th><th>Unseen: trades · per trade</th><th>1R take</th></tr>' + "".join(out) + "</table>")


def add_teacher_routes(app, *, settings, page, guard, auth, paths, csrf, jobs, exchange: str,
                       clock=time.time):
    root = paths.teacher

    def _event(kind: str, text: str, level: str = "info") -> None:
        from ..ops import EventLog

        EventLog(paths.events, service="dashboard").emit(kind, text, level=level)

    @app.get("/teacher")
    def teacher_page(request: Request, msg: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        t = Teacher(root)
        cand, act = t.candidate, t.active
        form = csrf(token)
        busy = next((j for j in jobs.list() if j["status"] in ("queued", "running")
                     and j.get("meta", {}).get("kind") == "teach"), None)
        state = (f'<b class="good">ON</b>: paper trading follows lessons {_e(act["version"])} '
                 f'(turned on by {_e(act.get("turned_on_by", "?"))})' if act
                 else '<b class="bad">OFF</b>: paper trading takes every signal as before')
        parts = [f'<p class="good">{_e(msg)}</p>' if msg else "",
                 '<div class="card"><h3>Backtest teacher</h3>'
                 f'<p>{state}</p><p class="small muted">The teacher replays every strategy\'s backtest and '
                 'notes the market situation at each signal: trend (up, range, down), volatility (low, normal, '
                 'high) and volume (quiet, normal, heavy). It learns on the earlier 70% of the signals where each '
                 'strategy made or lost money, and where taking profit at 1R caught more winners. Then it checks '
                 'those lessons on the later 30% it never saw. The newest 20% of history is never used: it is '
                 'kept for the go-live gate. The lessons can be turned on only if following them did better '
                 'on that unseen part. When on, paper trading skips a strategy in situations it lost in and adds '
                 'the 1R profit-take where it helped. Live trading never uses them. Past situations can stop '
                 'paying, so nothing here promises a profit.</p>']
        if cand:
            verdict = ('<b class="good">passed the check</b>' if cand["passed"]
                       else f'<b class="bad">not usable</b>: {_e(cand["why"])}')
            c = cand["counts"]
            parts.append(f'<p class="small">Latest lessons {_e(cand["version"])} {verdict}<br>'
                         f'{cand["signals"]} backtest signals; {c["trade"]} situations to trade, {c["avoid"]} to '
                         f'avoid, {c["unproven"]} unproven; 1R take helps in {cand["exit_1r"]}<br>'
                         f'On the unseen part: {_check_text(cand["check"])}</p>')
        buttons = []
        if busy:
            parts.append(f'<p class="small">Learning now (<a href="/jobs/{_e(busy["id"])}">progress</a>)</p>')
        else:
            buttons.append(f'<form method="post" action="/teacher/teach">{form}<button class="ghost">'
                           f'{"Teach again" if cand else "Teach from the backtests"}</button></form>')
        if cand and cand["passed"] and cand["version"] != act.get("version"):
            buttons.append(f'<form method="post" action="/teacher/on">{form}<button>Turn on {_e(cand["version"])}'
                           '</button></form>')
        if act:
            buttons.append(f'<form method="post" action="/teacher/off">{form}<button class="ghost">Turn off'
                           '</button></form>')
        parts.append(f'<div class="row">{"".join(buttons)}</div></div>')

        lessons = (act or cand).get("lessons", {})
        if lessons:
            from ..data.store import CandleStore

            store = CandleStore(settings.db_path)
            try:
                now = now_situations(store, exchange, list(settings.symbols), now_ms=int(clock() * 1000))
            finally:
                store.close()
            rows = []
            for sym, key in now.items():
                if key is None:
                    rows.append(f'<tr><td>{_e(sym)}</td><td class="muted">not enough 1h history</td><td></td></tr>')
                    continue
                best = best_for(lessons, key)
                names = ", ".join(f'{_e(b["strategy"])} ({_r(b["own"]["expectancy_r"])}, win {_pct(b["own"]["win_rate"])})'
                                  for b in best) or '<span class="muted">no strategy proven here yet</span>'
                rows.append(f"<tr><td>{_e(sym)}</td><td>{_e(describe(key))}</td><td>{names}</td></tr>")
            parts += ["<h2>Right now</h2><p class='small muted'>Each pair's situation on its newest closed 1h "
                      "candle, and the strategies that made money in that situation in the backtest.</p>",
                      "<table><tr><th>Pair</th><th>Situation</th><th>Proven strategies here</th></tr>",
                      "".join(rows), "</table>",
                      "<h2>Best lessons</h2>", _lesson_rows(lessons, "trade", 30),
                      "<h2>Situations to avoid</h2>", _lesson_rows(lessons, "avoid", 20)]
        return page("Teacher", "".join(parts), user, token, refresh=120)

    def _post(request: Request, csrf_value: str, action):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf_value):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        return RedirectResponse(f"/teacher?msg={urllib.parse.quote(action(user))}", status_code=303)

    @app.post("/teacher/teach")
    def teacher_teach(request: Request, csrf: str = Form("")):
        def act(user):
            busy = {j.get("meta", {}).get("kind") for j in jobs.list() if j["status"] in ("queued", "running")}
            if "teach" in busy:
                return "The teacher is already learning; see its progress below."
            jobs.submit("Backtest teacher: learn which market situations each strategy wins in",
                        [["learn", "teach", "--dir", root]], meta={"kind": "teach"})
            _event("teacher", f"backtest teacher started from the dashboard by {user}")
            return "Learning started. When it finishes this page shows the lessons and how they did on unseen data."
        return _post(request, csrf, act)

    @app.post("/teacher/on")
    def teacher_on(request: Request, csrf: str = Form("")):
        def act(user):
            try:
                t = Teacher(root, events=_events()).turn_on(user)
            except ValueError as exc:
                return f"Not turned on: {exc}"
            return (f"Lessons {t['version']} are on. Paper trading now skips strategies in situations they lost "
                    "in and adds the 1R profit-take where it helped. Turn off goes back to every signal at once.")
        return _post(request, csrf, act)

    @app.post("/teacher/off")
    def teacher_off(request: Request, csrf: str = Form("")):
        def act(user):
            Teacher(root, events=_events()).turn_off(user)
            return "Lessons off: paper trading takes every signal as before."
        return _post(request, csrf, act)

    def _events():
        from ..ops import EventLog

        return EventLog(paths.events, service="dashboard")
