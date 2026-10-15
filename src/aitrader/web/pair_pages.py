"""The Pairs page: the Bybit spot pair ranking with its reasons, and each pair's market regime.

"Rank pairs" queues ``aitrader pairs rank`` (Bybit public data, no key). The regimes come from the
candles the collector stored, worked out on each page load from closed bars only. Paper and
backtest results are split by the regime their signals were taken in. Nothing here trades.
"""

from __future__ import annotations

import json
import os
import time

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..config import EXCHANGE
from ..data.store import CandleStore
from ..market import gainers
from ..market.pairs import configured_symbols, load_ranking, ranked_symbols
from ..market.regime import current_regimes
from .app import _csrf, _e, _ms, _q

REGIME_TIMEFRAMES = ["1h", "4h", "1d"]
REGIME_EXCHANGES = [EXCHANGE]  # the candles the collector stores
STALE_HOURS = 48
_CLASS = {"trend-up": "good", "trend-down": "bad", "high-volatility": "warn", "low-liquidity": "warn"}


def add_pair_routes(app, *, settings, auth, jobs, page, guard, ranking_path: str, paper_state: str,
                    report_path: str, clock=None) -> None:
    def running() -> dict | None:
        for j in jobs.list():
            if j.get("meta", {}).get("kind") == "pairs" and j["status"] in ("queued", "running"):
                return j
        return None

    def gainer_job() -> dict | None:
        for j in jobs.list():
            if j.get("meta", {}).get("kind") == "gainers" and j["status"] in ("queued", "running"):
                return j
        return None

    @app.post("/pairs/gainers")
    def gainers_switch(request: Request, csrf: str = Form(""), on: str = Form(""), count: str = Form(""),
                       market: str = Form("futures"), window: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        market = "spot" if market == "spot" else "futures"
        name = "Spot" if market == "spot" else "Futures"
        if on != "1":
            gainers.switch(False, path=gainers.path_for(market), by=user)
            return RedirectResponse("/pairs?msg=" + _q(f"{name} top gainers off: {name} AI is back to the usual pairs. "
                                                       "Open trades on them still close normally."), status_code=303)
        if gainer_job():
            return RedirectResponse("/pairs?error=" + _q("the top gainers are already being fetched"), status_code=303)
        n = gainers.clamp_top(count or gainers.DEFAULT_TOP)
        window = window if window in gainers.WINDOWS else gainers.window_of(gainers.load(gainers.path_for(market)))
        jobs.submit(f"Trade the top {n} {name.lower()} gainers ({window})",
                    [["pairs", "gainers", "--market", market, "--count", str(n), "--window", window]],
                    meta={"kind": "gainers", "by": user})
        return RedirectResponse("/pairs?msg=" + _q(f"Scanning Bybit {name.lower()} for the top {n} gainers over {window}. "
                                                   f"{name} AI paper trading and the Markets page add them as soon as the "
                                                   "list arrives; the collector downloads their prices on its next pass."),
                                status_code=303)

    @app.get("/pairs")
    def pairs(request: Request, msg: str = "", error: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        now = int((clock or time.time)() * 1000)
        rk = load_ranking(ranking_path)
        job = running()
        body = f'<p class="good">{_e(msg)}</p>' if msg else ""
        body += f'<p class="bad">{_e(error)}</p>' if error else ""
        if job:
            body += (f'<p class="warn">Ranking pairs ({_e(job["status"])}). '
                     f'<a href="/jobs/{_e(job["id"])}">Follow the job</a></p>')
        body += '<div class="grid">' + "".join(_gainers_card(auth, token, now, gainer_job(), m)
                                               for m in ("spot", "futures")) + "</div>"
        body += _run_card(auth, token, rk, job, now, settings.symbols, ranking_path)
        symbols = [p["symbol"] for p in rk.get("pairs", [])[:20]] or list(settings.symbols)
        try:
            db = CandleStore(settings.db_path)
            try:
                regimes = current_regimes(db, symbols, REGIME_TIMEFRAMES, REGIME_EXCHANGES, now_ms=now)
            finally:
                db.close()
        except Exception:  # no database yet: the page still shows the ranking
            regimes = []
        by_pair: dict = {}
        for r in regimes:
            by_pair.setdefault(r["symbol"], {})[r["timeframe"]] = r
        body += _ranking(rk, by_pair) if rk.get("pairs") else _regimes_only(symbols, by_pair)
        body += _by_regime(_load(paper_state), _load(report_path))
        body += ('<p class="muted small">The ranking describes today\'s market, not future returns. '
                 'Backtesting today\'s top pairs over past years leaves out coins that were delisted or '
                 'faded (survivorship bias), so it flatters results. Regimes use closed candles only and '
                 'do not block trades unless a strategy lists regimes to avoid.</p>')
        return page("Pairs", body, user, token, refresh=30 if job else None)

    @app.post("/pairs/rank")
    def rank(request: Request, csrf: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if running():
            return RedirectResponse("/pairs?error=" + _q("a ranking is already running"), status_code=303)
        jobs.submit("Rank Bybit spot USDT pairs", [["pairs", "rank", "--out", ranking_path]],
                    meta={"kind": "pairs", "by": user})
        return RedirectResponse("/pairs?msg=" + _q("Ranking started (Bybit public data)."), status_code=303)


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _run_card(auth, token, rk: dict, job, now: int, fixed: list[str], ranking_path: str) -> str:
    mode = (os.environ.get("AITRADER_PAIRS") or "fixed").strip().lower()
    if rk.get("generated"):
        age = (now - rk["generated"]) / 3_600_000
        last = f"Last ranking {_ms(rk['generated'])} ({age:.0f}h ago)" + (
            ", stale" if age > float(os.environ.get("AITRADER_PAIRS_MAX_AGE_HOURS") or STALE_HOURS) else "") + "."
    else:
        last = "No ranking yet."
    used = configured_symbols(fixed, path=ranking_path, now_ms=now)
    fresh = ranked_symbols(ranking_path, now_ms=now,
                           max_age_hours=float(os.environ.get("AITRADER_PAIRS_MAX_AGE_HOURS") or STALE_HOURS))
    source = ("the ranked list" if mode == "ranked" and fresh
              else "the fixed list" + (" (ranking missing or stale)" if mode == "ranked" else ""))
    disabled = " disabled" if job else ""
    return (f'<div class="card"><h3>Pair ranking</h3><p class="small">{_e(last)} Trading uses {_e(source)}: '
            f'{_e(", ".join(used[:20]))}{" ..." if len(used) > 20 else ""}. Set AITRADER_PAIRS=ranked in .env to '
            f'trade the top ranked pairs.</p><form method="post" action="/pairs/rank" class="row">'
            f'{_csrf(auth, token)}<button{disabled}>Rank pairs now</button></form></div>')


def _gainers_card(auth, token, now: int, job, market: str = "futures") -> str:
    """One scanner's switch, window and today's scored list."""
    path = gainers.path_for(market)
    name = "Spot" if market == "spot" else "Futures"
    data = gainers.load(path)
    on, top = bool(data.get("on")), gainers.clamp_top(data.get("top", gainers.DEFAULT_TOP))
    window = gainers.window_of(data)
    live = gainers.symbols(path, now_ms=now)

    def pct(x):
        return "-" if x is None else f'<span class="{"good" if x >= 0 else "bad"}">{x:+.2f}%</span>'

    def num(x, fmt: str, unit: str) -> str:
        return "-" if x is None else f"{x:{fmt}}{unit}"

    rows = "".join(
        f'<tr><td>{_e(p["symbol"])}</td><td><b>{p.get("score", p["change"]):+.1f}</b></td>'
        f'<td>{pct(p.get("change_1h"))}</td><td>{pct(p.get("change_4h"))}</td><td>{pct(p["change"])}</td>'
        f'<td>{num(p.get("relvol"), ".1f", "x")}</td><td>{num(p.get("spread"), ".3f", "%")}</td>'
        f'<td>{_usdt(p.get("turnover"))}</td>'
        f'<td>{"trading" if p["symbol"] in live else ("<span class=warn>" + _e(p["pump"]) + "</span>" if p.get("pump") else "-")}</td></tr>'
        for p in data.get("pairs", [])[:top])
    when = (f"List from {_ms(data['generated'])}, {data.get('scanned', len(data.get('pairs', [])))} candidates scanned."
            if data.get("generated") else "No list yet.")
    if job:
        state = f'<span class="warn">scanning</span> (<a href="/jobs/{_e(job["id"])}">job</a>)'
    elif on and live:
        state = f'<span class="good">ON</span>: {len(live)} trading in {name} AI paper'
    elif on:
        state = '<span class="warn">ON</span>, but the list is missing or old (the collector refreshes it every hour)'
    else:
        state = '<span class="muted">OFF</span>'
    csrf = _csrf(auth, token)
    opts = "".join(f'<option value="{w}"{" selected" if w == window else ""}>{w}</option>' for w in gainers.WINDOWS)
    form = (f'<form method="post" action="/pairs/gainers" class="row">{csrf}<input type="hidden" name="on" value="1">'
            f'<input type="hidden" name="market" value="{market}">'
            f'<label class="small">Top <input type="number" name="count" min="{gainers.MIN_TOP}" max="{gainers.MAX_TOP}" '
            f'value="{top}" style="width:4.5em"></label>'
            f'<label class="small">over <select name="window" style="width:5em">{opts}</select></label>'
            f'<button{" disabled" if job else ""}>{"Refresh" if on else "Trade the top gainers"}</button></form>')
    if on:
        form += (f'<form method="post" action="/pairs/gainers" class="row">{csrf}<input type="hidden" name="on" value="0">'
                 f'<input type="hidden" name="market" value="{market}">'
                 '<button class="ghost">Stop trading the top gainers</button></form>')
    table = ('<table><tr><th>Pair</th><th>Score</th><th>1h</th><th>4h</th><th>24h</th><th>Rel. volume</th>'
             f'<th>Spread</th><th>Turnover</th><th>Paper</th></tr>{rows}</table>' if rows else "")
    what = ("Bybit USDT spot pairs. Spot AI buys them." if market == "spot" else
            "Bybit USDT perpetual futures (also listed on spot, where the candles come from). Futures AI trades "
            "them long and short.")
    return (f'<div class="card"><h3>{name} top gainers</h3><p>{state}. Window <b>{_e(window)}</b>. {_e(when)}</p>'
            f'<p class="small muted">{what} Kept: over {gainers.MIN_TURNOVER / 1e6:.0f}M USDT of 24h turnover, spread '
            f'at most {gainers.MAX_SPREAD_PCT:g}%. Score = rise over the window (%) + 2 x (relative volume - 1), at most '
            '+6, - 5 x spread (%). Up over '
            f'{gainers.PUMP_1H_PCT:g}% in an hour or {gainers.PUMP_24H_PCT:g}% in a day is flagged as a pump and not '
            'traded. A high score is not a buy signal: the strategies still decide, with the usual risk limits. '
            'Refreshed every hour while on.</p>'
            f'{form}{table}</div>')


def _usdt(x) -> str:
    if x is None:
        return "-"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:.0f}"


def _badges(regs: dict) -> str:
    out = []
    for tf in REGIME_TIMEFRAMES:
        r = regs.get(tf)
        if not r or not r.get("label"):
            out.append(f'<span class="muted small">{tf} -</span>')
            continue
        out.append(f'<span class="badge {_CLASS.get(r["label"], "")}" title="{_e(r["reason"])} '
                   f'(confidence {r["confidence"]:.2f}, {_e(r["exchange"])})">{tf} {_e(r["label"])}</span>')
    return " ".join(out)


def _ranking(rk: dict, by_pair: dict) -> str:
    head = ("<tr><th>#</th><th>Pair</th><th>Score</th><th>24h turnover</th><th>Spread (bps)</th>"
            "<th>Depth ±1%</th><th>24h range</th><th>Regime now</th><th>Why</th></tr>")
    rows = []
    for p in rk["pairs"]:
        vol = "-" if p["volatility_pct"] is None else f'{p["volatility_pct"]:.1f}%'
        rows.append(f'<tr><td class=muted>{p["rank"]}</td><td><b>{_e(p["symbol"])}</b></td><td>{p["score"]:.1f}</td>'
                    f'<td>{_usdt(p["turnover_usdt"])}</td><td>{p["spread_bps"]:.1f}</td><td>{_usdt(p["depth_usdt"])}</td>'
                    f'<td>{vol}</td>'
                    f'<td>{_badges(by_pair.get(p["symbol"], {}))}</td>'
                    f'<td class="small">{_e("; ".join(p["reasons"]))}</td></tr>')
    out = (f'<h2>Ranked pairs</h2><p class="muted small">{_e(rk["exchange"])} {_e(rk["market_type"])} '
           f'{_e(rk["quote"])} pairs, {len(rk["pairs"])} eligible · {_e(rk.get("note", ""))}</p>'
           f"<table>{head}{''.join(rows)}</table>")
    if rk.get("excluded"):
        out += ('<details class="card health"><summary>Excluded '
                f'({len(rk["excluded"])})</summary><ul class="small">'
                + "".join(f'<li>{_e(x["symbol"])}: {_e(x["why"])}</li>' for x in rk["excluded"][:500])
                + "</ul></details>")
    return out


def _regimes_only(symbols: list[str], by_pair: dict) -> str:
    rows = "".join(f'<tr><td><b>{_e(s)}</b></td><td>{_badges(by_pair.get(s, {}))}</td></tr>' for s in symbols)
    return ('<h2>Market regime</h2><p class=muted>No pair ranking yet; showing the fixed pair list.</p>'
            f"<table><tr><th>Pair</th><th>Regime now</th></tr>{rows}</table>")


def _by_regime(paper: dict, report: dict) -> str:
    from ..backtest.engine import Trade
    from ..market.regime import breakdown

    trades = [(t.get("regime"), Trade("", "", "", 0, 0, 0.0, 0.0, None, r=float(t.get("r") or 0)))
              for t in paper.get("trades", [])]
    sections = [("Paper trades", breakdown(trades) if trades else {}),
                ("Backtest report (all strategies pooled)", report.get("by_regime") or {})]
    out = '<h2>Results by market regime</h2>'
    for title, rows in sections:
        if not rows:
            out += f'<p class="muted small">{_e(title)}: nothing yet.</p>'
            continue
        body = "".join(
            f'<tr><td>{_e(k)}</td><td>{v["trades"]:,}</td><td>{v["win_rate"] * 100:.1f}</td>'
            f'<td class="{"good" if v["expectancy_r"] > 0 else "bad" if v["expectancy_r"] < 0 else ""}">'
            f'{v["expectancy_r"]:+.3f}</td><td>{v["total_r"]:+.1f}</td></tr>' for k, v in rows.items())
        out += (f'<h3>{_e(title)}</h3><table><tr><th>Regime</th><th>Trades</th><th>Win %</th>'
                f'<th>R / trade</th><th>Total R</th></tr>{body}</table>')
    return out
