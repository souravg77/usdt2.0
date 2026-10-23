"""The Change requests page: the change requests Claude turned into GitHub pull requests
(``aitrader.ops.changes``). New requests are turned off (``changes.OFF``); the list stays.
"""

from __future__ import annotations

from fastapi import Request

from ..ops import changes
from .app import _e
from .news_pages import _link
from ..clock import local

STATUS_CLS = {"pr_open": "good", "failed": "bad", "declined": "warn", "running": "warn", "queued": "warn"}
STATUS_TEXT = {"pr_open": "pull request open", "failed": "failed", "declined": "declined by Claude",
               "running": "working", "queued": "waiting"}


def add_change_routes(app, *, page, guard, changes_dir: str = changes.DEFAULT_DIR) -> None:
    @app.get("/changes")
    def changes_page(request: Request):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        body = f'<p class="warn">{_e(changes.OFF[0].upper() + changes.OFF[1:])}.</p>'
        body += _table(changes.list_requests(changes_dir))
        return page("Change requests", body, user, token)


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
        when = local(r.get("created", 0))
        out += (f'<tr><td class="small">{when}<div class="muted">{_e(r.get("by", ""))}</div></td>'
                f'<td>{_e(r.get("text", ""))}</td>'
                f'<td><span class="badge {STATUS_CLS.get(st, "")}">{_e(STATUS_TEXT.get(st, st))}</span></td>'
                f'<td class="small">{result}</td></tr>')
    return out + "</table>"
