"""The Reports page: run the multi-year backtest of every strategy and read the win report.

"Run" queues one job: download the price history the strategies need (Binance public data, no
key), then ``aitrader learn report``. The report is saved as JSON and shown here, best first.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from .app import _csrf, _e, _ms, _q

REPORT_PATH = "data/reports/backtest.json"
REPORT_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]  # every timeframe the library uses


def add_report_routes(app, *, settings, auth, jobs, page, guard, report_path: str = REPORT_PATH) -> None:
    def running() -> dict | None:
        for j in jobs.list():
            if j.get("meta", {}).get("kind") == "report" and j["status"] in ("queued", "running"):
                return j
        return None

    @app.get("/reports")
    def reports(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        rep = _load(report_path)
        job = running()
        body = f'<p class="good">{_e(msg)}</p>' if msg else ""
        body += f'<p class="bad">{_e(error)}</p>' if error else ""
        if job:
            body += (f'<p class="warn">The backtest is running ({_e(job["status"])}). Downloading 5 years of '
                     f'prices takes a while the first time; this page refreshes every minute. '
                     f'<a href="/jobs/{_e(job["id"])}">Follow the job</a></p>')
        body += _run_card(auth, token, rep, job)
        body += _report(rep) if rep else ('<p class=muted>No report yet. Run the backtest above: it '
                                          'tests every strategy the agent has learned on the last 5 years '
                                          'of Binance prices.</p>')
        return page("Reports", body, user, token, refresh=60 if job else None)

    @app.post("/reports/run")
    def run(request: Request, csrf: str = Form(""), years: int = Form(5)):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if running():
            return RedirectResponse("/reports?error=" + _q("a backtest is already running"), status_code=303)
        years = min(max(int(years or 5), 1), 8)
        since = (datetime.now(timezone.utc) - timedelta(days=365 * years + 30)).strftime("%Y-%m-%d")
        dl = ["download", "--since", since, "--exchange", "binance"]
        for tf in REPORT_TIMEFRAMES:
            dl += ["--timeframe", tf]
        jobs.submit(f"{years}-year backtest of every strategy",
                    [dl, ["learn", "report", "--years", str(years), "--out", report_path]],
                    meta={"kind": "report", "by": user})
        return RedirectResponse("/reports?msg=" + _q("Backtest started. It downloads the price history "
                                                     "first, then tests every strategy."), status_code=303)

    @app.get("/reports/backtest.json")
    def download(request: Request):
        _user, _token, redirect = guard(request)
        if redirect:
            return redirect
        if not os.path.exists(report_path):
            return PlainTextResponse("no report yet", status_code=404)
        with open(report_path, encoding="utf-8") as fh:
            return Response(fh.read(), media_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="backtest-report.json"'})


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _run_card(auth, token, rep: dict, job) -> str:
    last = f"Last report: {_ms(rep.get('generated'))}." if rep else "No report yet."
    disabled = " disabled" if job else ""
    return (f'<div class="card"><h3>Backtest every strategy</h3><p class="small">Tests each strategy the '
            f'agent learned (from the books and the built-ins) on Binance history for every pair, with '
            f'fees and slippage. {_e(last)}</p><form method="post" action="/reports/run" class="row">'
            f'{_csrf(auth, token)}<label class="row">Years <select name="years">'
            + "".join(f'<option{" selected" if y == 5 else ""}>{y}</option>' for y in (1, 2, 3, 4, 5))
            + f'</select></label><button{disabled}>Run backtest</button></form></div>')


def _short(verdict: str) -> str:
    return {"strong": "Strong", "profitable": "Profitable, uneven", "slightly": "Slightly profitable",
            "lost": "Lost money", "too": "Too few trades"}.get(verdict.split()[0].rstrip(":"), verdict)


def _r(x) -> str:
    return "-" if x is None else f"{x:+.1f}"


def _pair(a: dict, k1: str, k2: str, fmt: str) -> str:
    """Two numbers as "x / y"; a report saved before they existed shows "-"."""
    if a.get(k1) is None or a.get(k2) is None:
        return "-"
    return f"{a[k1]:{fmt}} / {a[k2]:{fmt}}"


def _report(rep: dict) -> str:
    t = rep.get("totals", {})
    span = f"{_ms(rep.get('data_from'))[:10]} to {_ms(rep.get('data_to'))[:10]}"
    tiles = [("Strategies tested", t.get("tested", 0), ""),
             ("Made money", t.get("profitable", 0), "good" if t.get("profitable") else "bad"),
             ("Strong (most years)", t.get("strong", 0), "good" if t.get("strong") else ""),
             ("Trades", f"{t.get('trades', 0):,}", ""),
             ("Overall win rate", f"{t.get('win_rate', 0)}%", "")]
    out = ('<h2>Results</h2><div class="stats">' + "".join(
        f'<div class="stat"><span class="k">{_e(k)}</span><span class="v {c}">{_e(v)}</span></div>'
        for k, v, c in tiles) + "</div>")
    out += (f'<p class="muted small">{rep.get("years")} years · prices {span} · '
            f'{_e(", ".join(rep.get("symbols", [])))} on Binance · fees {rep["costs"]["taker_fee"] * 100:.2f}% '
            f'+ slippage {rep["costs"]["slippage_bps"]:.0f} bps per side · USDT column: a '
            f'{rep.get("account_usdt", 10000):,.0f} USDT account risking {rep.get("risk_pct", 1):.0f}% per trade, '
            f'not compounded · <a href="/reports/backtest.json">download the full report</a></p>')
    years = sorted({y for s in rep.get("strategies", []) for y in s.get("by_year", {})})
    head = ("<tr><th>#</th><th>Strategy</th><th>Verdict</th><th>Trades</th><th>Win %</th><th>R / trade</th>"
            "<th>Profit factor</th><th>Max DD (R)</th><th>Sharpe / Sortino</th><th>Fees / slippage (R per trade)</th>"
            "<th>Total R</th><th>USDT at 1%</th>"
            + "".join(f"<th>{_e(y)}</th>" for y in years) + "</tr>")
    rows = []
    for i, s in enumerate(rep.get("strategies", []), 1):
        a = s["all"]
        pf = "∞" if a["profit_factor"] is None else f'{a["profit_factor"]:.2f}'
        v = s["verdict"]
        vcls = "good" if v.startswith("strong") else "bad" if v.startswith("lost") else "warn" if "profitable" in v else ""
        tcls = "good" if a["total_r"] > 0 else "bad" if a["total_r"] < 0 else ""
        cells = "".join(
            f'<td class="{"good" if (s["by_year"].get(y) or {}).get("total_r", 0) > 0 else "bad" if (s["by_year"].get(y) or {}).get("total_r", 0) < 0 else "muted"}">'
            f'{_r((s["by_year"].get(y) or {}).get("total_r")) if s["by_year"].get(y) else "-"}</td>' for y in years)
        rows.append(
            f'<tr><td class=muted>{i}</td><td><b>{_e(s["name"])}</b><div class="muted small">{_e(s["source"][:60])}'
            f' · {_e(", ".join(s["timeframes"]))}</div></td><td><span class="badge {vcls}" title="{_e(v)}">{_e(_short(v))}</span></td>'
            f'<td>{a["trades"]:,}</td><td>{a["win_rate"]:.1f}</td><td class="{tcls}">{a["expectancy_r"]:+.3f}</td>'
            f'<td>{pf}</td>'
            f'<td>{a["max_drawdown_r"]:.1f}</td>'
            f'<td>{_pair(a, "sharpe", "sortino", "+.2f")}</td><td>{_pair(a, "fee_r", "slippage_r", ".3f")}</td>'
            f'<td class="{tcls}"><b>{a["total_r"]:+.1f}</b></td>'
            f'<td class="{tcls}">{s["usdt_at_1pct"]:+,.0f}</td>{cells}</tr>')
    out += f"<table>{head}{''.join(rows)}</table>"
    if rep.get("skipped"):
        out += ('<details class="card health"><summary>Not tested '
                f'({len(rep["skipped"])})</summary><ul class="small">'
                + "".join(f'<li>{_e(x["id"])}: {_e(x["why"])}</li>' for x in rep["skipped"]) + "</ul></details>")
    out += ('<p class="muted small">How to read it: R is the amount risked on a trade (entry to stop). '
            '+0.20R per trade means each trade made 20% of what it risked, on average, after costs. A win '
            'rate under 50% is normal for trend strategies whose winners are bigger than their losers. '
            'Past results don\'t guarantee future ones: the agent still paper-trades a strategy before any '
            'real money, and only strategies that pass the stricter out-of-sample gate do so.</p>')
    return out

