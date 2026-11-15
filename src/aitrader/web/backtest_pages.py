"""Backtest page (``/backtest``): tick the strategies to test, pick pairs and a period, and run a
backtest as a background job (``learn backtest-select``) on the engine the dashboard is showing.

Spot AI tests on spot candles with long strategies; Futures AI on the perpetuals' own candles with
its long and short strategies, perp fees and funding. The result is a report only: nothing moves on
the ladder, nothing goes to paper or live.
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import time
import urllib.parse

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from ..learn import picks, select, wallet
from ..learn.picks import learns_for
from ..learn.registry import Registry
from . import backtest_chart

ENGINE_NAMES = {"spot": "Spot AI", "futures": "Futures AI"}
MONTHS = (1, 3, 6, 12, 24, 36)
KEEP_RUNS = 20
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TF_ORDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


def _tf_order(tf: str) -> int:
    return TF_ORDER.index(tf) if tf in TF_ORDER else len(TF_ORDER)


def _e(x) -> str:
    return html.escape(str(x))


def _r(x) -> str:
    return "–" if x is None else f"{x:+.3f}R"


def _oos(e, market: str) -> dict:
    bt = (e.backtest_futures if market == "futures" else e.backtest) or {}
    return bt.get("out_of_sample") or {}


def _tfs(e) -> str:
    spec = e.spec or {}
    return ", ".join(spec.get("timeframes") or (spec.get("base_spec") or {}).get("timeframes") or [])


def candidates(reg: Registry, market: str) -> list:
    """This engine's strategies, paper trading first, then by their last backtest result."""
    rows = [e for e in reg.entries.values() if learns_for(e, market) and e.status != "retired"]
    rows.sort(key=lambda e: (e.status != "paper", -_oos(e, market).get("expectancy_r", -9.0), e.id))
    return rows


def engine_pairs(settings, market: str) -> list[str]:
    """The pairs that have candles for this engine (what a backtest can run on)."""
    if market != "futures":
        return list(settings.symbols)
    from ..data.perp import futures_db_path
    from ..data.store import CandleStore
    from ..ops.doctor import _newest_candles

    path = futures_db_path(settings.db_path)
    if not os.path.exists(path):
        return []
    store = CandleStore(path)
    try:
        return sorted(_newest_candles(store.conn))
    finally:
        store.close()


def add_backtest_routes(app, *, settings, page, guard, auth, paths, csrf, jobs):
    runs_dir = os.path.join(os.path.dirname(paths.report) or "data/reports", "selected")

    def _engine() -> str:
        return "futures" if wallet.futures_on(wallet.market_path(paths.paper_state)) else "spot"

    def _busy(market: str):
        return next((j for j in jobs.list() if j["status"] in ("queued", "running")
                     and j.get("meta", {}).get("kind") == "backtest"
                     and j.get("meta", {}).get("market") == market), None)

    def _runs(market: str) -> list[str]:
        return sorted(glob.glob(os.path.join(runs_dir, f"{market}-*.json")), reverse=True)

    def _load(path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _added_list(market: str, token: str) -> str:
        """This engine's WIN strategies that were added from a backtest, with tick boxes to remove them."""
        added = picks.added_from_backtests(picks.load(picks.picks_path(paths.registry, market)))
        if not added:
            return ""
        name = ENGINE_NAMES[market]
        rows = "".join(
            f'<tr><td><input type="checkbox" name="items" value="{_e(r["id"])}:{_e(tf)}" aria-label="remove {_e(r["id"])}"></td>'
            f'<td>{_e(r["id"])}</td><td>{_e(tf)}</td><td>{r["trades"]}</td>'
            f'<td class="{"good" if r["total_r"] > 0 else "bad"}">{r["total_r"]:+.2f}R</td>'
            f'<td class="small">{_e(r.get("from_backtest", ""))}{" by " + _e(r["added_by"]) if r.get("added_by") else ""}</td></tr>'
            for tf, r in sorted(added, key=lambda x: (_tf_order(x[0]), x[1]["id"])))
        return (f'<div class="card"><details open><summary><b>{name} WIN strategies you added from a backtest '
                f'({len(added)})</b></summary><form method="post" action="/backtest/remove-win">{csrf(token)}'
                f'<input type="hidden" name="engine" value="{market}"><table><tr><th></th><th>Strategy</th>'
                '<th>Timeframe</th><th>Trades</th><th>Total</th><th>Backtest</th></tr>' + rows + '</table>'
                '<p><button class="ghost">Remove the ticked ones from the WIN strategies</button></p>'
                '<p class="small muted">A removed one stops opening paper trades on the next scan; its open trades '
                'close normally. The other WIN strategies stay.</p></form></details></div>')

    def _coverage(rep: dict) -> str:
        """Where the stored candles start inside the period: before then nothing could be tested,
        so a longer period gives the same result."""
        late: dict[str, int] = {}
        for r in rep.get("strategies", []):
            for k, ts in (r.get("tested_from") or {}).items():
                late[k] = ts
        if not late:
            return ""
        by_tf: dict[str, list[int]] = {}
        for k, ts in late.items():
            by_tf.setdefault(k.rsplit(" ", 1)[-1], []).append(ts)
        fmt = "%Y-%m-%d"
        parts = [f"{tf}: {len(v)} pairs, from {time.strftime(fmt, time.gmtime(min(v) / 1000))} at the earliest, "
                 f"{time.strftime(fmt, time.gmtime(max(v) / 1000))} at the latest"
                 for tf, v in sorted(by_tf.items(), key=lambda kv: _tf_order(kv[0]))]
        return ('<p class="bad small"><b>Not the whole period:</b> this server\'s stored candles start after the '
                'period began, so trades before then could not be tested and a longer period gives the same '
                f'result. {_e("; ".join(parts))}. Each pair also needs {select.MIN_BARS} candles to warm up first.</p>')

    def _downloads(run_name: str) -> str:
        if not run_name:
            return ""
        q = urllib.parse.quote(run_name)
        links = [("trades.csv", "Trades (CSV)"), ("summary.csv", "Summary (CSV)"), ("chart.svg", "Chart (SVG image)"),
                 ("report.json", "Full report (JSON)")]
        return ('<p class="small">Download: ' + " · ".join(
            f'<a href="/backtest/download?run={q}&amp;what={w}" download>{t}</a>' for w, t in links) + '</p>')

    def _chart(rep: dict) -> str:
        if not any("trade_list" in r for r in rep.get("strategies", [])):
            return ('<div class="card"><p class="muted small">This run is from before the chart was added: run it '
                    'again to see the chart and the trade list.</p></div>')
        drawn = backtest_chart.svg(rep)
        if not drawn:
            return '<div class="card"><p class="muted">No trades in this period, so there is nothing to chart.</p></div>'
        return ('<div class="card chart"><h3>Running total in R</h3><p class="small muted">Each strategy\'s result '
                'added up trade by trade, at the moment each trade closed (after costs).</p>' + drawn + '</div>')

    def _trade_list(rep: dict) -> str:
        rows = [(r["id"], t) for r in rep.get("strategies", []) for t in r.get("trade_list") or []]
        if not rows:
            return ""
        rows.sort(key=lambda rt: -rt[1]["exit_ts"])
        shown = rows[:200]

        def when(ms):
            return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else "–"

        body = "".join(
            f'<tr><td class="small">{_e(sid)}</td><td>{_e(t["symbol"])} {_e(t["timeframe"])}</td><td>{_e(t["side"])}</td>'
            f'<td class="small">{when(t["entry_ts"])}</td><td class="small">{when(t["exit_ts"])}</td>'
            f'<td>{t["entry"]:.6g}</td><td>{t["exit"]:.6g}</td><td class="small">{_e(t["exit_reason"])}</td>'
            f'<td class="{"good" if t["r"] > 0 else "bad"}">{t["r"]:+.2f}R</td></tr>' for sid, t in shown)
        more = f" (newest {len(shown)} of {len(rows)}; the CSV has them all)" if len(rows) > len(shown) else ""
        return (f'<div class="card"><details><summary>Every trade{more}</summary><table><tr><th>Strategy</th>'
                '<th>Pair</th><th>Side</th><th>Opened (UTC)</th><th>Closed (UTC)</th><th>Entry</th><th>Exit</th>'
                f'<th>Why closed</th><th>Result</th></tr>{body}</table></details></div>')

    def _result(rep: dict, token: str = "", run_name: str = "", market: str = "spot") -> str:
        fmt = "%Y-%m-%d"
        made = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(rep.get("made", 0)))
        head = (f'<div class="card"><h3>Result: {_e(rep.get("engine", ""))}, '
                f'{time.strftime(fmt, time.gmtime(rep["start"] / 1000))} to '
                f'{time.strftime(fmt, time.gmtime((rep["end"] - 1) / 1000))}</h3>'
                f'<p class="small muted">Run {made} on {len(rep["pairs"])} pairs: {_e(", ".join(rep["pairs"]))}. '
                'A simulation on the real Bybit candles stored on this server: no order is placed. '
                'Out of the period only trades that opened inside it count; after fees, slippage and (Futures) '
                'funding. Fewer than 30 trades proves little either way.</p>'
                + _coverage(rep) + _downloads(run_name) + '</div>' + _chart(rep) + _trade_list(rep))

        def line(name, m, extra="", tick=""):
            return (f'<tr><td>{tick}</td><td>{name}</td><td>{m.get("trades", 0)}</td><td>{m.get("win_rate", 0):.0%}</td>'
                    f'<td>{_r(m.get("expectancy_r"))}</td><td>{m.get("total_r", 0):+.1f}R</td>'
                    f'<td>{m.get("profit_factor", 0):.2f}</td><td>{m.get("max_drawdown_r", 0):.1f}R</td>'
                    f'<td>{m.get("t_stat", 0):.2f}</td><td class="small">{extra}</td></tr>')

        rows = []
        for r in sorted(rep["strategies"], key=lambda r: -(r.get("total_r") or 0)):
            name = f'<a href="/strategies/{urllib.parse.quote(r["id"])}">{_e(r["id"])}</a>'
            if r.get("error"):
                rows.append(f'<tr><td></td><td>{name}</td><td colspan="8" class="bad small">could not run: {_e(r["error"])}</td></tr>')
                continue
            pairs = r.get("by_pair") or {}
            detail = ""
            if pairs:
                inner = "".join(f'<li>{_e(k)}: {v["trades"]} trades, {v["win_rate"]:.0%} win, '
                                f'{_r(v["expectancy_r"])} each, {v["total_r"]:+.1f}R</li>'
                                for k, v in sorted(pairs.items(), key=lambda kv: -kv[1]["total_r"]))
                detail = f'<details><summary>by pair</summary><ul>{inner}</ul></details>'
            if r.get("no_data"):
                detail += f'<span class="muted">no candles for {len(r["no_data"])} pair/timeframe</span>'
            tick = (f'<input type="checkbox" name="ids" value="{_e(r["id"])}" aria-label="add {_e(r["id"])}">'
                    if run_name and r.get("trades") else "")
            rows.append(line(name + f' <span class="small muted">{_e(", ".join(r.get("timeframes") or []))}</span>',
                             r, detail, tick))
        rows.append(line("<b>All together</b>", rep["total"]))
        table = ('<table><tr><th></th><th>Strategy</th><th>Trades</th><th>Win</th><th>Per trade</th><th>Total</th>'
                 '<th>Profit factor</th><th>Worst drop</th><th>t</th><th></th></tr>' + "".join(rows) + '</table>')
        if not run_name:
            return head + table
        have = [tf for tf, cur in picks.load(picks.picks_path(paths.registry, market)).items() if (cur or {}).get("results")]
        name = ENGINE_NAMES.get(market, "")
        lists = (f"{name} has WIN strategies on {', '.join(sorted(have, key=_tf_order))}"
                 if have else f"{name} has no WIN strategies on any timeframe yet")
        return (head + f'<form method="post" action="/backtest/add-win">{csrf(token)}'
                f'<input type="hidden" name="run" value="{_e(run_name)}"><input type="hidden" name="engine" value="{market}">'
                + table + '<div class="card"><p><button>Add the ticked ones to the WIN strategies</button></p>'
                f'<p class="small muted">Each ticked strategy becomes a {name} WIN strategy on its own timeframe, '
                'with this result saved next to it, and starts paper trading on the next scan. Only timeframes '
                f'that already have WIN strategies take one: {_e(lists)}. The WIN strategies already there stay, '
                'and the other engine is not changed. Fewer than 30 trades proves little; you can remove one again '
                'with the tick boxes on the Trading page.</p></div></form>')

    @app.get("/backtest")
    def backtest_page(request: Request, ids: str = "", msg: str = "", run: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        eng = _engine()
        name = ENGINE_NAMES[eng]
        reg = Registry(paths.registry)
        cands = candidates(reg, eng)
        ticked = {i.strip() for i in ids.split(",") if i.strip()}
        pairs = engine_pairs(settings, eng)
        busy = _busy(eng)
        data = ("the perpetual futures' own candles (long and short strategies, perp fees and funding)"
                if eng == "futures" else "spot candles (long strategies only)")
        parts = [f'<p class="good">{_e(msg)}</p>' if msg else "",
                 f'<div class="card"><h3>Backtest: {name}</h3><p>Tick the strategies to test, choose the pairs '
                 f'and the period, and run. It tests on {data}. Switch engine at the top for the other one.</p>'
                 '<p class="small muted">A report only: nothing moves to paper or live, and the strategies\' '
                 'own test results are not changed.</p></div>']
        if busy:
            parts.append(f'<div class="card"><p>Running now: {_e(busy["title"])} '
                         f'(<a href="/jobs/{_e(busy["id"])}">progress</a>). The result appears here when it is done.</p></div>')
        parts.append(_added_list(eng, token))
        runs = _runs(eng)
        shown = os.path.join(runs_dir, os.path.basename(run)) if run else (runs[0] if runs else "")
        rep = _load(shown) if shown and shown in runs else None
        if rep:
            parts.append(_result(rep, token, os.path.basename(shown), eng))
            if len(runs) > 1:
                links = " · ".join(f'<a href="/backtest?run={urllib.parse.quote(os.path.basename(p))}">'
                                   f'{_e(os.path.basename(p)[len(eng) + 1:-5])}</a>' for p in runs[:KEEP_RUNS])
                parts.append(f'<p class="small">Earlier runs: {links}</p>')
        if not cands:
            parts.append(f'<p class="muted">{name} has no strategies to test yet.</p>')
            return page("Backtest", "".join(parts), user, token, refresh=30 if busy else None)

        def row(e):
            o = _oos(e, eng)
            on = "checked" if e.id in ticked else ""
            return (f'<tr><td><input type="checkbox" name="ids" value="{_e(e.id)}" {on}></td>'
                    f'<td>{_e(e.id)}</td><td>{_e(e.status)}</td><td>{_e(_tfs(e))}</td>'
                    f'<td>{o.get("trades", "–")}</td><td>{_r(o.get("expectancy_r"))}</td></tr>')

        paper = [e for e in cands if e.status == "paper"]
        rest = [e for e in cands if e.status != "paper"]
        head = '<tr><th></th><th>Strategy</th><th>Status</th><th>Timeframes</th><th>Last test trades</th><th>Per trade</th></tr>'
        pair_boxes = "".join(f'<label class="small"><input type="checkbox" name="pairs" value="{_e(p)}" checked> '
                             f'{_e(p)}</label> ' for p in pairs)
        month_opts = "".join(f'<option value="{m}"{" selected" if m == 6 else ""}>last {m} month{"s" if m > 1 else ""}</option>'
                             for m in MONTHS)
        form = (f'<form method="post" action="/backtest/run">{csrf(token)}'
                f'<input type="hidden" name="engine" value="{eng}">'
                '<div class="card"><h3>1. Strategies</h3>'
                f'<p class="small muted">Up to {select.MAX_STRATEGIES}. '
                '<label><input type="checkbox" name="all_paper" value="1"> all strategies paper trading now '
                f'({len(paper)})</label></p>'
                + (f'<table>{head}{"".join(row(e) for e in paper)}</table>' if paper else "")
                + (f'<details{" open" if ticked & {e.id for e in rest} else ""}><summary>Other strategies ({len(rest)}: '
                   f'tested, rejected or not tested yet)</summary><table>{head}{"".join(row(e) for e in rest)}</table></details>'
                   if rest else "")
                + '</div><div class="card"><h3>2. Pairs</h3>'
                + (f'<p>{pair_boxes}</p>' if pairs else '<p class="muted">No stored candles for this engine yet.</p>')
                + '</div><div class="card"><h3>3. Period</h3>'
                f'<p><select name="months">{month_opts}</select> or from '
                '<input type="date" name="start"> to <input type="date" name="end"> '
                '<span class="small muted">(dates win when given)</span></p></div>'
                + ('<button disabled>A backtest is already running</button>' if busy else '<button>Run backtest</button>')
                + '</form>')
        parts.append(form)
        return page("Backtest", "".join(parts), user, token, refresh=30 if busy else None)

    @app.post("/backtest/run")
    async def backtest_run(request: Request):
        form = await request.form()
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, form.get("csrf", "")):
            return PlainTextResponse("bad form token; reload the page", status_code=403)

        def back(msg: str, ids: list[str] | None = None):
            q = {"msg": msg}
            if ids:
                q["ids"] = ",".join(ids)
            return RedirectResponse("/backtest?" + urllib.parse.urlencode(q), status_code=303)

        eng = str(form.get("engine", ""))
        if eng not in ENGINE_NAMES:
            eng = _engine()
        if _busy(eng):
            return back("A backtest is already running for this engine; see its progress.")
        reg = Registry(paths.registry)
        allowed = {e.id: e for e in candidates(reg, eng)}
        ids = [i for i in form.getlist("ids") if i in allowed]
        if form.get("all_paper"):
            ids = list(dict.fromkeys(ids + [i for i, e in allowed.items() if e.status == "paper"]))
        if not ids:
            return back("Tick at least one strategy.")
        if len(ids) > select.MAX_STRATEGIES:
            return back(f"At most {select.MAX_STRATEGIES} strategies at a time ({len(ids)} ticked).", ids[:select.MAX_STRATEGIES])
        have = engine_pairs(settings, eng)
        pairs = [p for p in form.getlist("pairs") if p in have]
        if not pairs:
            return back("Tick at least one pair.", ids)
        start, end = str(form.get("start", "")).strip(), str(form.get("end", "")).strip()
        if (start and not DATE.match(start)) or (end and not DATE.match(end)):
            return back("Dates must look like 2026-05-01.", ids)
        try:
            months = int(form.get("months", 6))
        except ValueError:
            months = 6
        months = months if months in MONTHS else 6
        try:
            select.period(months, start, end)
        except ValueError as exc:
            return back(f"Bad period: {exc}.", ids)
        os.makedirs(runs_dir, exist_ok=True)
        for old in _runs(eng)[KEEP_RUNS - 1:]:  # keep the newest runs only
            try:
                os.remove(old)
            except OSError:
                pass
        out = os.path.join(runs_dir, f"{eng}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json")
        step = ["learn", "backtest-select", "--market", eng, "--ids", ",".join(ids), "--pairs", ",".join(pairs),
                "--months", str(months), "--out", out]
        if start:
            step += ["--start", start]
        if end:
            step += ["--end", end]
        when = f"{start or '…'} to {end or 'now'}" if (start or end) else f"last {months} months"
        jobs.submit(f"Backtest ({ENGINE_NAMES[eng]}): {len(ids)} strategies, {len(pairs)} pairs, {when}",
                    [step], meta={"kind": "backtest", "market": eng})
        return back(f"Backtesting {len(ids)} strategies on {len(pairs)} pairs ({when}). The result appears below "
                    "when it is done.")

    @app.post("/backtest/add-win")
    async def backtest_add_win(request: Request):
        """Add the strategies ticked on a Backtest result to this engine's WIN strategies, on their
        timeframes that already have some (``learn win-add``, a background job like the other picks)."""
        form = await request.form()
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, form.get("csrf", "")):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        eng = str(form.get("engine", ""))
        if eng not in ENGINE_NAMES:
            eng = _engine()
        name = ENGINE_NAMES[eng]
        run = os.path.basename(str(form.get("run", "")))
        path = os.path.join(runs_dir, run)

        def back(msg: str):
            q = {"msg": msg}
            if run:
                q["run"] = run
            return RedirectResponse("/backtest?" + urllib.parse.urlencode(q), status_code=303)

        rep = _load(path) if path in _runs(eng) else None
        if not rep or rep.get("market", "spot") != eng:
            return back(f"That backtest result isn't one of {name}'s; run it again.")
        ids = list(dict.fromkeys(str(i) for i in form.getlist("ids")))
        rows = {r["id"]: r for r in rep.get("strategies", []) if not r.get("error")}
        ids = [i for i in ids if i in rows]
        if not ids:
            return back("Tick at least one strategy in the result.")
        if any(j["status"] in ("queued", "running") and j.get("meta", {}).get("kind") == "pick" for j in jobs.list()):
            return back("A WIN pick is already running; try again when it ends.")
        saved = picks.load(picks.picks_path(paths.registry, eng))
        reg = Registry(paths.registry)
        ok, refused = [], []
        for i in ids:
            for tf in rows[i].get("timeframes") or []:
                why = picks.can_add(reg.entries.get(i), tf, saved, eng)
                if not why and picks.tf_result(rows[i], tf) is None:
                    why = f"no trades on {tf} in this backtest"
                (refused if why else ok).append(f"{i} {tf}" + (f" ({why})" if why else ""))
        if not ok:
            return back("Nothing added: " + "; ".join(refused) + ".")
        jobs.submit(f"{name}: add {len(ok)} backtested strategies to the WIN strategies",
                    [["learn", "win-add", "--report", path, "--ids", ",".join(ids), "--market", eng, "--by", user]],
                    meta={"kind": "pick", "by": user, "market": eng})
        msg = (f"Adding to {name}'s WIN strategies: {', '.join(ok)}. They paper trade from the next scan.")
        if refused:
            msg += " Not added: " + "; ".join(refused) + "."
        return back(msg)

    @app.get("/backtest/download")
    def backtest_download(request: Request, run: str = "", what: str = ""):
        """One saved Backtest result as a file: the trades or the summary as CSV, the chart as an
        SVG image, or the whole report as JSON."""
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        name = os.path.basename(run)
        path = os.path.join(runs_dir, name)
        if path not in _runs("spot") + _runs("futures"):
            return PlainTextResponse("no such backtest result", status_code=404)
        rep = _load(path)
        if rep is None:
            return PlainTextResponse("that result can't be read", status_code=404)
        stem = name[:-5]
        if what == "trades.csv":
            body, kind = select.trades_csv(rep), "text/csv"
        elif what == "summary.csv":
            body, kind = select.summary_csv(rep), "text/csv"
        elif what == "chart.svg":
            body, kind = backtest_chart.svg(rep, standalone=True), "image/svg+xml"
            if not body:
                return PlainTextResponse("no trades in this result, so there is no chart", status_code=404)
        elif what == "report.json":
            body, kind = json.dumps(rep, indent=1), "application/json"
        else:
            return PlainTextResponse("unknown download", status_code=404)
        return Response(body, media_type=kind,
                        headers={"Content-Disposition": f'attachment; filename="backtest-{stem}-{what}"',
                                 "X-Content-Type-Options": "nosniff"})

    @app.post("/backtest/remove-win")
    async def backtest_remove_win(request: Request):
        """Take ticked strategies that were added from a backtest out of this engine's WIN strategies
        (``learn win-remove``, a background job like the other picks)."""
        form = await request.form()
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, form.get("csrf", "")):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        eng = str(form.get("engine", ""))
        if eng not in ENGINE_NAMES:
            eng = _engine()
        name = ENGINE_NAMES[eng]

        def back(msg: str):
            return RedirectResponse("/backtest?" + urllib.parse.urlencode({"msg": msg}), status_code=303)

        known = {f"{r['id']}:{tf}" for tf, r in
                 picks.added_from_backtests(picks.load(picks.picks_path(paths.registry, eng)))}
        items = [i for i in dict.fromkeys(str(v) for v in form.getlist("items")) if i in known]
        if not items:
            return back("Tick at least one WIN strategy to remove.")
        if any(j["status"] in ("queued", "running") and j.get("meta", {}).get("kind") == "pick" for j in jobs.list()):
            return back("A WIN pick is already running; try again when it ends.")
        jobs.submit(f"{name}: remove {len(items)} WIN strategies added from backtests",
                    [["learn", "win-remove", "--items", ",".join(items), "--market", eng, "--by", user]],
                    meta={"kind": "pick", "by": user, "market": eng})
        shown = ", ".join(i.replace(":", " ") for i in items)
        return back(f"Removing from {name}'s WIN strategies: {shown}. They stop opening paper trades on the next "
                    "scan; their open trades close normally.")
