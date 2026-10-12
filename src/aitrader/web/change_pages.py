"""The Change requests page: describe a change to the platform; Claude writes it and opens a
GitHub pull request (``aitrader.ops.changes``). Nothing on the server changes until you merge the
pull request and run the installer. Admin only, like every page that can start a job.
"""

from __future__ import annotations

import os
import time

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..ops import changes
from .app import _csrf, _e, _q
from .news_pages import _link

STATUS_CLS = {"pr_open": "good", "failed": "bad", "declined": "warn", "running": "warn", "queued": "warn"}
STATUS_TEXT = {"pr_open": "pull request open", "failed": "failed", "declined": "declined by Claude",
               "running": "working", "queued": "waiting"}


def add_change_routes(app, *, auth, jobs, page, guard, changes_dir: str = changes.DEFAULT_DIR) -> None:
    def running() -> dict | None:
        for j in jobs.list():
            if j.get("meta", {}).get("kind") == "change" and j["status"] in ("queued", "running"):
                return j
        return None

    @app.get("/changes")
    def changes_page(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        job = running()
        missing = [n for n, k in (("Anthropic API key", "ANTHROPIC_API_KEY"), ("GitHub token", "GITHUB_TOKEN"))
                   if not os.environ.get(k)]
        body = f'<p class="good">{_e(msg)}</p>' if msg else ""
        body += f'<p class="bad">{_e(error)}</p>' if error else ""
        if missing:
            body += (f'<p class="warn">Save the {" and the ".join(missing)} on the <a href="/settings">Settings</a> '
                     'page first.</p>')
        if job:
            body += ('<p class="warn">Claude is working on a change; this page refreshes by itself. '
                     f'<a href="/jobs/{_e(job["id"])}">Follow the job</a></p>')
        body += ('<div class="card"><p class="small muted">Describe a change to the platform in plain words. '
                 'Claude reads the code on GitHub, writes the change and opens a <b>pull request</b>. '
                 '<b>Nothing changes on this server</b> until you merge it on GitHub and run the installer. '
                 'Claude can\'t run the tests from here, and it refuses changes that would weaken the live '
                 'lock, risk limits, keys or login. Changes to safety-critical files are flagged in the pull '
                 'request: read those line by line.</p>'
                 f'<form method="post" action="/changes">{_csrf(auth, token)}'
                 f'<textarea name="text" rows="6" maxlength="{changes.MAX_REQUEST_CHARS}" style="width:100%" '
                 'placeholder="For example: on the Trading page, show each open position\'s distance to its stop in %."'
                 '></textarea>'
                 f'<button{" disabled" if job or missing else ""}>Send to Claude</button></form></div>')
        body += _table(changes.list_requests(changes_dir))
        return page("Change requests", body, user, token, refresh=15 if job else None)

    @app.post("/changes")
    def changes_submit(request: Request, csrf: str = Form(""), text: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if running():
            return RedirectResponse("/changes?error=" + _q("a change is already being worked on"), status_code=303)
        try:
            req = changes.new_request(text, by=user, root=changes_dir)
        except changes.ChangeError as exc:
            return RedirectResponse("/changes?error=" + _q(str(exc)), status_code=303)
        jobs.submit(f"Change request {req['id']}", [["change", "run", req["id"]]],
                    meta={"kind": "change", "by": user, "change": req["id"]})
        return RedirectResponse("/changes?msg=" + _q("Sent. The pull request link appears here when Claude is "
                                                     "done."), status_code=303)


def _table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">No change requests yet.</p>'
    out = "<table><tr><th>Asked</th><th>Request</th><th>Status</th><th>Result</th></tr>"
    for r in rows:
        st = r.get("status", "")
        result = _link(r.get("pr_url", ""), "open the pull request") if r.get("pr_url") else ""
        detail = r.get("error") or r.get("summary") or ""
        if detail:
            result += f'<div class="small muted">{_e(detail[:600])}</div>'
        if r.get("files"):
            result += f'<div class="small muted">{_e(", ".join(r["files"]))}</div>'
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.get("created", 0)))
        out += (f'<tr><td class="small">{when} UTC<div class="muted">{_e(r.get("by", ""))}</div></td>'
                f'<td>{_e(r.get("text", ""))}</td>'
                f'<td><span class="badge {STATUS_CLS.get(st, "")}">{_e(STATUS_TEXT.get(st, st))}</span></td>'
                f'<td class="small">{result}</td></tr>')
    return out + "</table>"
