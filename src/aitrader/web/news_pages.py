"""The News page: what the agent knows about the world and each pair right now.

It shows the AI's web-search brief (market risk, world events, each pair's demand, risk and
coming events, with source links), the Fear & Greed index, trending coins and the latest
headlines. "Search now" queues ``aitrader news update`` as a job.
"""

from __future__ import annotations

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..news.filter import NewsFilter
from ..news.service import DEFAULT_DIR, load_latest
from .app import _csrf, _e, _ms, _q

RISK_CLS = {"low": "good", "normal": "", "elevated": "warn", "high": "bad"}
DEMAND_CLS = {"falling": "bad", "steady": "", "rising": "good", "hype": "warn"}
EFFECT = {"high": "new buys paused", "elevated": "new buys at half size"}


def add_news_routes(app, *, settings, auth, jobs, page, guard, news_dir: str = DEFAULT_DIR) -> None:
    def running() -> dict | None:
        for j in jobs.list():
            if j.get("meta", {}).get("kind") == "news" and j["status"] in ("queued", "running"):
                return j
        return None

    @app.get("/news")
    def news(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        latest = load_latest(news_dir)
        job = running()
        body = f'<p class="good">{_e(msg)}</p>' if msg else ""
        body += f'<p class="bad">{_e(error)}</p>' if error else ""
        if job:
            body += ('<p class="warn">The AI is searching the web now; this page refreshes by itself. '
                     f'<a href="/jobs/{_e(job["id"])}">Follow the job</a></p>')
        body += _top(auth, token, latest, job, NewsFilter(news_dir, cache_s=0), list(settings.symbols))
        if latest:
            body += _brief(latest.get("brief")) + _feeds(latest.get("feeds") or {})
        return page("News", body, user, token, refresh=20 if job else 300)

    @app.post("/news/refresh")
    def refresh(request: Request, csrf: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if running():
            return RedirectResponse("/news?error=" + _q("a search is already running"), status_code=303)
        jobs.submit("News: search the web", [["news", "update"]], meta={"kind": "news", "by": user})
        return RedirectResponse("/news?msg=" + _q("Searching the web. The brief appears here in a minute or two."),
                                status_code=303)


def _top(auth, token, latest: dict, job, flt: NewsFilter, symbols: list[str]) -> str:
    b = (latest or {}).get("brief")
    if not latest:
        state = "No news yet. The news service writes a brief every few hours; or search now."
    elif not b:
        state = (f"Feeds read {_ms(latest.get('generated'))}, but the AI brief failed: "
                 f"{latest.get('brief_error') or 'unknown error'}. It needs the Anthropic key on Settings.")
    else:
        state = f"Last brief {_ms(latest.get('generated'))} · {b.get('searches', 0)} web searches."
    disabled = " disabled" if job else ""
    effects = []
    for s in symbols:
        d = flt.decide(s)
        if not d.allow_entries:
            effects.append(f'<span class="badge bad">{_e(s)}: new buys paused</span>')
        elif d.size_multiplier < 1:
            effects.append(f'<span class="badge warn">{_e(s)}: size x{d.size_multiplier:g}</span>')
    effect = (" ".join(effects) if effects else '<span class="badge good">no limits from the news</span>')
    return (f'<div class="card"><h3>What the news means for trading</h3><p>{effect}</p>'
            f'<p class="small muted">{_e(state)} The news can only make the agent more careful: high risk '
            'pauses new buys, elevated risk halves their size. It never buys because of a headline.</p>'
            f'<form method="post" action="/news/refresh" class="row">{_csrf(auth, token)}'
            f'<button{disabled}>Search the web now</button></form></div>')


def _link(url: str, text: str = "source") -> str:
    return f'<a href="{_e(url)}" rel="noopener noreferrer" target="_blank">{_e(text)}</a>' if url else ""


def _brief(b: dict | None) -> str:
    if not b:
        return ""
    risk = b.get("market_risk", "normal")
    out = ('<h2>Market</h2><div class="stats"><div class="stat"><span class="k">Market risk</span>'
           f'<span class="v {RISK_CLS.get(risk, "")}">{_e(risk.title())}</span>'
           f'<span class="s">{_e(EFFECT.get(risk, "no limit"))}</span></div></div>'
           f'<p>{_e(b.get("market_risk_why", ""))}</p><p class="muted">{_e(b.get("summary", ""))}</p>')
    world = b.get("world_events") or []
    if world:
        out += "<h2>World events</h2><table><tr><th>Event</th><th>Impact</th><th>Effect on crypto</th><th></th></tr>"
        for w in world:
            out += (f'<tr><td>{_e(w["title"])}<div class="muted small">{_e(w.get("date", ""))}</div></td>'
                    f'<td><span class="badge {RISK_CLS.get({"medium": "elevated"}.get(w["impact"], w["impact"]), "")}">'
                    f'{_e(w["impact"])}</span></td><td>{_e(w["direction"])}</td><td>{_link(w.get("url", ""))}</td></tr>')
        out += "</table>"
    out += ("<h2>Pairs</h2><table><tr><th>Pair</th><th>Risk</th><th>Demand</th><th>Sentiment</th>"
            "<th>Why</th><th>Coming events</th></tr>")
    for p in b.get("pairs", []):
        sent = p.get("sentiment", 0)
        events = "".join(f'<div class="small">{_e(e.get("date", ""))} {_e(e["title"])} {_link(e.get("url", ""), "↗")}</div>'
                         for e in p.get("events", [])) or '<span class="muted">-</span>'
        srcs = " ".join(_link(u, f"[{i}]") for i, u in enumerate(p.get("sources", []), 1))
        out += (f'<tr><td><b>{_e(p["symbol"])}</b></td>'
                f'<td><span class="badge {RISK_CLS.get(p["risk"], "")}">{_e(p["risk"])}</span></td>'
                f'<td class="{DEMAND_CLS.get(p["demand"], "")}">{_e(p["demand"])}</td>'
                f'<td class="{"good" if sent > 0.2 else "bad" if sent < -0.2 else ""}">{sent:+.2f}</td>'
                f'<td class="small">{_e(p.get("why", ""))} <span class="small">{srcs}</span></td><td>{events}</td></tr>')
    return out + "</table>"


def _feeds(f: dict) -> str:
    out = ""
    fg, trend = f.get("fear_greed"), f.get("trending") or []
    tiles = []
    if fg:
        cls = "bad" if fg["value"] <= 25 else "warn" if fg["value"] >= 75 else ""
        tiles.append(f'<div class="stat"><span class="k">Fear &amp; Greed</span><span class="v {cls}">'
                     f'{fg["value"]} · {_e(fg["label"])}</span><span class="s">yesterday {fg["yesterday"]}</span></div>')
    if trend:
        tiles.append('<div class="stat"><span class="k">Trending searches (CoinGecko)</span><span class="v small">'
                     + _e(", ".join(t["symbol"] for t in trend)) + "</span></div>")
    if tiles:
        out += '<h2>Sentiment</h2><div class="stats">' + "".join(tiles) + "</div>"
    for key, title in (("crypto_headlines", "Crypto headlines"), ("world_headlines", "World headlines")):
        items = f.get(key) or []
        if items:
            out += (f'<details class="card health"><summary>{title} ({len(items)})</summary><ul class="small">'
                    + "".join(f'<li>{_link(i["url"], i["title"])} <span class="muted">· {_e(i["source"])}'
                              f'{" · " + _ms(i["published"]) if i.get("published") else ""}</span></li>'
                              for i in items[:40]) + "</ul></details>")
    if f.get("errors"):
        out += f'<p class="muted small">Not reachable this time: {_e(", ".join(f["errors"]))}</p>'
    return out
