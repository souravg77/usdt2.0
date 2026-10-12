"""The Research page: the trader-research log (``aitrader.news.research``).

It lists what traders and researchers publicly report about the strategy families the platform
runs, each with its source link, dates, evidence type and a credibility note, filtered by family
and evidence type. "Search now" queues ``aitrader research update`` as a job.

Findings are research inputs only: this page cannot change trading, risk limits or a strategy's
status. Every field comes from web pages, so all of it is escaped and only http(s) links are made.
"""

from __future__ import annotations

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..news import research
from ..news.service import DEFAULT_DIR
from .app import _csrf, _e, _q
from .news_pages import _link

EVIDENCE_CLS = {"anecdote": "warn", "backtest shown": "", "live record shown": "good",
                "peer-reviewed": "good", "other": ""}
SHOW = 200


def add_research_routes(app, *, auth, jobs, page, guard, news_dir: str = DEFAULT_DIR) -> None:
    def running() -> dict | None:
        for j in jobs.list():
            if j.get("meta", {}).get("kind") == "research" and j["status"] in ("queued", "running"):
                return j
        return None

    @app.get("/research")
    def research_page(request: Request, family: str = "", evidence: str = "", msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        family = family if family in research.FAMILIES else ""
        evidence = evidence if evidence in research.EVIDENCE else ""
        rows = research.load(news_dir)
        shown = [f for f in rows if (not family or f.get("family") == family)
                 and (not evidence or f.get("evidence") == evidence)]
        job = running()
        body = f'<p class="good">{_e(msg)}</p>' if msg else ""
        body += f'<p class="bad">{_e(error)}</p>' if error else ""
        if job:
            body += ('<p class="warn">The AI is searching the web now; this page refreshes by itself. '
                     f'<a href="/jobs/{_e(job["id"])}">Follow the job</a></p>')
        body += (f'<div class="card"><p class="small muted">Public reports from traders and researchers about the '
                 f'strategy families this platform runs. <b>Research input only</b>: findings never change trading, '
                 f'risk limits or a strategy\'s status, and votes, testimonials or win-rate screenshots are not '
                 f'evidence of profit. A promising idea still has to be written as a strategy and pass the '
                 f'backtest, out-of-sample and paper gates.</p>'
                 f'<form method="post" action="/research/refresh" class="row">{_csrf(auth, token)}'
                 f'<button{" disabled" if job else ""}>Search the web now</button></form></div>')
        body += _filters(family, evidence, len(shown), len(rows))
        body += _table(shown[:SHOW]) if shown else '<p class="muted">No findings yet.</p>'
        return page("Research", body, user, token, refresh=20 if job else None)

    @app.post("/research/refresh")
    def research_refresh(request: Request, csrf: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if running():
            return RedirectResponse("/research?error=" + _q("a search is already running"), status_code=303)
        jobs.submit("Research: search trader reports", [["research", "update"]],
                    meta={"kind": "research", "by": user})
        return RedirectResponse("/research?msg=" + _q("Searching the web. New findings appear here in a few "
                                                      "minutes."), status_code=303)


def _filters(family: str, evidence: str, shown: int, total: int) -> str:
    def options(values, current: str, everything: str) -> str:
        return f'<option value="">{everything}</option>' + "".join(
            f'<option value="{_e(v)}"{" selected" if v == current else ""}>{_e(v)}</option>' for v in values)

    return ('<form method="get" action="/research" class="row">'
            f'<label>Strategy family <select name="family">{options(research.FAMILIES, family, "all families")}'
            '</select></label>'
            f'<label>Evidence <select name="evidence">{options(research.EVIDENCE, evidence, "all evidence")}'
            '</select></label><button class="ghost">Filter</button></form>'
            f'<p class="muted small">{shown} of {total} findings.</p>')


def _table(rows: list[dict]) -> str:
    out = ("<table><tr><th>Found</th><th>Family</th><th>Claim</th><th>Evidence</th><th>Credibility</th>"
           "<th>Source</th></tr>")
    for f in rows:
        when = _e(f.get("date_found", ""))
        if f.get("source_date"):
            when += f'<div class="muted small">source {_e(f["source_date"])}</div>'
        ev = str(f.get("evidence", "other"))
        out += (f'<tr><td class="small">{when}</td><td class="small">{_e(f.get("family", ""))}</td>'
                f'<td>{_e(f.get("claim", ""))}</td>'
                f'<td><span class="badge {EVIDENCE_CLS.get(ev, "")}">{_e(ev)}</span></td>'
                f'<td class="small muted">{_e(f.get("credibility", ""))}</td>'
                f'<td class="small">{_link(f.get("url", ""), f.get("source") or "source")}</td></tr>')
    return out + "</table>"
