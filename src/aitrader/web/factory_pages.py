"""NEXUS pages: Strategy Factory, Evolution Lab and Strategy Arena.

- **Strategy Factory** (``/factory``): make new rule strategies (``learn factory``) or breed the
  next generation (``learn evolve``) as a background job, then backtest them; shows how many
  families the agent has tried and the luck bar that sets.
- **Evolution Lab** (``/evolution``): every machine-made strategy by generation, what passed and
  what failed and why (luck bar, stress test), and one strategy's family tree.
- **Strategy Arena** (``/arena``): up to four strategies side by side on the same numbers.

Every number comes from the registry the backtests wrote; nothing is simulated for display. None
of these pages can move a strategy to paper or live by hand: that stays with the gate and your
go-live approval.
"""

from __future__ import annotations

import html
import re
import time
import urllib.parse

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..learn import evolve, factory, luck
from ..learn.registry import Registry

MAX_N = 200
ARENA_MAX = 4


def _e(x) -> str:
    return html.escape(str(x))


def _r(x) -> str:
    return "–" if x is None else f"{x:+.3f}R"


def _status(s: str) -> str:
    cls = {"paper": "good", "rejected": "bad"}.get(s, "muted")
    return f'<span class="{cls}">{_e(s)}</span>'


def _link(eid: str) -> str:
    return f'<a href="/strategies/{urllib.parse.quote(eid)}">{_e(eid)}</a>'


def machine_entries(reg: Registry) -> list:
    return [e for e in reg.entries.values() if luck.is_machine_made(e.spec)]


def _oos(e) -> dict:
    return (e.backtest or {}).get("out_of_sample") or {}


def _why(e) -> str:
    failed = (e.backtest or {}).get("failed") or []
    return "; ".join(failed)


def reason_kind(why: str) -> str:
    """A gate failure without its numbers, so the same kind of failure is counted together."""
    if why.startswith("stress test"):
        return "stress test: " + why.split(":")[0].replace("stress test ", "")
    if "could be luck" in why:
        return "luck bar (t too low)"
    head = why.split(" (")[0].split(":")[0]
    return re.sub(r"\s+", " ", re.sub(r"[-+]?\d[\d.,%]*R?", "", head)).strip()


def generation_rows(entries: list) -> list[dict]:
    """One row per generation: how many, tested, passed, rejected, best in-sample fitness."""
    gens: dict[int, dict] = {}
    for e in entries:
        g = int(luck.lineage_of(e.spec).get("generation", 0))
        row = gens.setdefault(g, {"generation": g, "made": 0, "tested": 0, "paper": 0, "rejected": 0,
                                  "under_review": 0, "best": None, "best_id": None})
        row["made"] += 1
        if e.backtest:
            row["tested"] += 1
        if e.status in ("paper", "rejected", "under_review"):
            row[e.status] += 1
        f = evolve.fitness(e)
        if f is not None and (row["best"] is None or f > row["best"]):
            row["best"], row["best_id"] = f, e.id
    return [gens[k] for k in sorted(gens)]


def arena_pick(reg: Registry, ids: list[str]) -> list:
    """The strategies to compare: the ones asked for, else the four best tested by out-of-sample R."""
    chosen = []
    for i in ids[:ARENA_MAX]:
        try:
            chosen.append(reg.get(i))
        except KeyError:
            continue
    if chosen:
        return chosen
    tested = [e for e in reg.entries.values() if _oos(e).get("trades", 0) > 0]
    tested.sort(key=lambda e: -_oos(e).get("expectancy_r", 0.0))
    return tested[:ARENA_MAX]


def add_factory_routes(app, *, page, guard, auth, paths, csrf, jobs, clock=time.time):
    def _busy():
        return next((j for j in jobs.list() if j["status"] in ("queued", "running")
                     and j.get("meta", {}).get("kind") == "factory"), None)

    def _event(text: str) -> None:
        from ..ops import EventLog

        EventLog(paths.events, service="dashboard").emit("factory", text)

    @app.get("/factory")
    def factory_page(request: Request, msg: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        reg = Registry(paths.registry)
        made = machine_entries(reg)
        n_fam = luck.families(reg.entries.values())
        by_status: dict[str, int] = {}
        for e in made:
            by_status[e.status] = by_status.get(e.status, 0) + 1
        parents = evolve.parents(reg)
        busy = _busy()
        form = csrf(token)
        tf_boxes = "".join(f'<label class="small"><input type="checkbox" name="timeframes" value="{t}" checked> {t}</label> '
                           for t in factory.TIMEFRAMES)
        counts = " · ".join(f"{_status(s)} {k}" for s, k in sorted(by_status.items())) or '<span class="muted">none yet</span>'
        parts = [f'<p class="good">{_e(msg)}</p>' if msg else "",
                 '<div class="card"><h3>Strategy Factory</h3>'
                 '<p class="small muted">The agent writes new strategies from 30 indicator building blocks (trend, '
                 'trigger, confirmation, volatility filter, stop, exit, timeframe), checked by the same safe rule '
                 'language as book strategies. Each one starts at RESEARCH and has to pass the full backtest gate, '
                 'a stress test, and a luck bar that rises with every idea tried. Most will fail: that is the gate '
                 'working. Nothing here can trade real money.</p></div>',
                 f'<div class="card"><p>{len(made)} machine-made strategies in <b>{n_fam}</b> families: {counts}</p>',
                 f'<p>A new machine-made strategy needs an out-of-sample t of at least '
                 f'<b>{luck.required_t(n_fam):.2f}</b> (book strategies: 2.00). '
                 f'<span class="small muted">Wide random search raises this quickly; evolution that only changes '
                 f'numbers does not.</span></p></div>']
        if busy:
            parts.append(f'<div class="card"><p>Working now: {_e(busy["title"])} '
                         f'(<a href="/jobs/{_e(busy["id"])}">progress</a>)</p></div>')
        else:
            parts.append(
                '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:12px 0">'
                f'<form method="post" action="/factory/generate" class="card">{form}<h3>Generate</h3>'
                f'<p><label>How many <input type="number" name="n" value="20" min="1" max="{MAX_N}"></label></p>'
                f'<p>{tf_boxes}</p>'
                '<p><label class="small"><input type="checkbox" name="shorts" value="1" checked> '
                'also make short twins for Futures AI</label></p>'
                '<p><label class="small"><input type="checkbox" name="test" value="1" checked> '
                'backtest them straight away</label></p>'
                '<button>Generate strategies</button></form>'
                f'<form method="post" action="/factory/evolve" class="card">{form}<h3>Evolve</h3>'
                f'<p class="small muted">{len(parents)} parents qualify (factory strategies with '
                f'{evolve.MIN_TRADES}+ in-sample trades and a positive in-sample result; out-of-sample results are '
                'never used to pick parents).</p>'
                f'<p><label>Children <input type="number" name="n" value="20" min="1" max="{MAX_N}"></label></p>'
                '<p><label>Crossover share <input type="number" name="crossover" value="0.5" min="0" max="1" '
                'step="0.1"></label></p>'
                '<p><label class="small"><input type="checkbox" name="shorts" value="1" checked> short twins too</label></p>'
                '<p><label class="small"><input type="checkbox" name="test" value="1" checked> '
                'backtest them straight away</label></p>'
                + ('<button>Breed the next generation</button>' if len(parents) >= 2 else
                   '<button disabled>Needs 2 tested parents</button>')
                + '</form></div>')
        recent = sorted(made, key=lambda e: -e.added)[:25]
        if recent:
            rows = "".join(
                f'<tr><td>{_link(e.id)}</td><td>{_e(e.kind)}</td>'
                f'<td>{luck.lineage_of(e.spec).get("generation", 0)}</td><td>{_status(e.status)}</td>'
                f'<td>{_oos(e).get("trades", "–")}</td><td>{_r(_oos(e).get("expectancy_r"))}</td>'
                f'<td class="small">{_e(_why(e))[:240]}</td></tr>' for e in recent)
            parts.append('<h2>Newest</h2><table><tr><th>Strategy</th><th>Side</th><th>Gen</th><th>Status</th>'
                         '<th>OOS trades</th><th>OOS per trade</th><th>Why rejected</th></tr>' + rows + '</table>')
        return page("Strategy Factory", "".join(parts), user, token, refresh=60 if busy else None)

    def _post(request: Request, csrf_value: str, action):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf_value):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        return RedirectResponse(f"/factory?msg={urllib.parse.quote(action(user))}", status_code=303)

    def _test_steps(test: bool, shorts: bool) -> list[list[str]]:
        if not test:
            return []
        steps = [["learn", "test"]]
        if shorts:
            steps.append(["learn", "test", "--market", "futures"])
        return steps

    @app.post("/factory/generate")
    async def factory_generate(request: Request):
        form = await request.form()

        def act(user):
            if _busy():
                return "The factory is already working; see its progress."
            try:
                n = max(1, min(MAX_N, int(form.get("n", 20))))
            except ValueError:
                return "How many must be a whole number."
            tfs = [t for t in form.getlist("timeframes") if t in factory.TIMEFRAMES]
            if not tfs:
                return "Pick at least one timeframe."
            shorts, test = bool(form.get("shorts")), bool(form.get("test"))
            step = ["learn", "factory", "--n", str(n), "--timeframes", ",".join(tfs)] + (["--shorts"] if shorts else [])
            jobs.submit(f"Strategy Factory: make {n} strategies" + (" and backtest them" if test else ""),
                        [step, *_test_steps(test, shorts)], meta={"kind": "factory"})
            _event(f"strategy factory started from the dashboard by {user}: {n} strategies on {', '.join(tfs)}")
            return f"Making {n} strategies" + (" and backtesting them" if test else "") + ". Results appear below."
        return _post(request, form.get("csrf", ""), act)

    @app.post("/factory/evolve")
    async def factory_evolve(request: Request):
        form = await request.form()

        def act(user):
            if _busy():
                return "The factory is already working; see its progress."
            try:
                n = max(1, min(MAX_N, int(form.get("n", 20))))
                share = min(1.0, max(0.0, float(form.get("crossover", 0.5))))
            except ValueError:
                return "Children and crossover share must be numbers."
            shorts, test = bool(form.get("shorts")), bool(form.get("test"))
            step = ["learn", "evolve", "--n", str(n), "--crossover", f"{share:g}"] + (["--shorts"] if shorts else [])
            jobs.submit(f"Evolution: breed {n} children" + (" and backtest them" if test else ""),
                        [step, *_test_steps(test, shorts)], meta={"kind": "factory"})
            _event(f"strategy evolution started from the dashboard by {user}: {n} children")
            return f"Breeding {n} children" + (" and backtesting them" if test else "") + "."
        return _post(request, form.get("csrf", ""), act)

    @app.get("/evolution")
    def evolution_page(request: Request, id: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        reg = Registry(paths.registry)
        made = machine_entries(reg)
        parts = ['<div class="card"><h3>Evolution Lab</h3><p class="small muted">Every strategy the agent made '
                 'itself, by generation. Generation 0 comes from the factory; each later one is bred from the best '
                 'of the earlier ones on their in-sample results. A child passes only on data its parents were '
                 'never chosen on.</p></div>']
        if id:
            try:
                tree = evolve.family_tree(reg, reg.get(id).id)
                parts.append(f'<h2>Family tree of {_e(id)}</h2><pre class="small">{_e(chr(10).join(tree))}</pre>')
            except KeyError:
                parts.append(f'<p class="bad">No strategy {_e(id)}.</p>')
        rows = generation_rows(made)
        if not rows:
            parts.append('<p class="muted">No machine-made strategies yet. Start on the '
                         '<a href="/factory">Strategy Factory</a> page.</p>')
            return page("Evolution Lab", "".join(parts), user, token, refresh=None)
        parts.append('<h2>Generations</h2><table><tr><th>Gen</th><th>Made</th><th>Tested</th><th>Paper</th>'
                     '<th>Under review</th><th>Rejected</th><th>Best in-sample</th></tr>' + "".join(
                         f'<tr><td>{r["generation"]}</td><td>{r["made"]}</td><td>{r["tested"]}</td>'
                         f'<td class="good">{r["paper"]}</td><td>{r["under_review"]}</td><td>{r["rejected"]}</td>'
                         f'<td>{_r(r["best"])}' + (f' <a href="/evolution?id={urllib.parse.quote(r["best_id"])}">tree</a>'
                                                     if r["best_id"] else "") + '</td></tr>' for r in rows)
                     + '</table>')
        winners = [e for e in made if e.status in ("paper", "under_review")]
        if winners:
            parts.append('<h2>Survivors</h2><table><tr><th>Strategy</th><th>Gen</th><th>How made</th><th>Status</th>'
                         '<th>OOS</th><th>Luck bar</th><th>Stress</th></tr>' + "".join(
                             _row(e) for e in sorted(winners, key=lambda e: -_oos(e).get("expectancy_r", 0))) + '</table>')
        failed = [e for e in made if e.status == "rejected"]
        reasons: dict[str, int] = {}
        for e in failed:
            for w in (e.backtest or {}).get("failed") or []:
                key = reason_kind(w)
                reasons[key] = reasons.get(key, 0) + 1
        if reasons:
            parts.append('<h2>Why candidates were rejected</h2><table><tr><th>Reason</th><th>Strategies</th></tr>' + "".join(
                f'<tr><td>{_e(k)}</td><td>{v}</td></tr>' for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:15])
                + '</table>')
        return page("Evolution Lab", "".join(parts), user, token, refresh=None)

    def _row(e) -> str:
        lin = luck.lineage_of(e.spec)
        bt = e.backtest or {}
        lk = bt.get("luck") or {}
        st = bt.get("stress") or {}
        how = _e(lin.get("op", ""))
        if lin.get("parents"):
            how += " of " + ", ".join(_link(p) for p in lin["parents"])
        stress = ("–" if not st else '<span class="good">passed</span>' if st.get("passed")
                  else '<span class="bad">failed</span>')
        o = _oos(e)
        return (f'<tr><td>{_link(e.id)} <a class="small" href="/evolution?id={urllib.parse.quote(e.id)}">tree</a></td>'
                f'<td>{lin.get("generation", 0)}</td><td class="small">{how}</td><td>{_status(e.status)}</td>'
                f'<td>{o.get("trades", 0)} · {_r(o.get("expectancy_r"))} · t {o.get("t_stat", 0):.2f}</td>'
                f'<td>{lk.get("min_t_oos", "–")}</td><td>{stress}</td></tr>')

    @app.get("/arena")
    def arena_page(request: Request, ids: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        reg = Registry(paths.registry)
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        chosen = arena_pick(reg, wanted)
        parts = ['<div class="card"><h3>Strategy Arena</h3><p class="small muted">Up to four strategies side by '
                 'side on the numbers their own backtests and paper trading recorded (out of sample after fees, '
                 'slippage and funding). Without a choice it shows the four best by out-of-sample result. '
                 'A result from few trades means little: check the trade count and t first.</p>'
                 f'<form method="get" action="/arena"><input name="ids" size="60" value="{_e(",".join(wanted))}" '
                 'placeholder="strategy ids, comma separated"> <button class="ghost">Compare</button></form></div>']
        if not chosen:
            parts.append('<p class="muted">No tested strategies yet.</p>')
            return page("Strategy Arena", "".join(parts), user, token, refresh=None)

        def cell(e, f):
            try:
                return f(e)
            except Exception:
                return "–"

        def bt(e):
            return e.backtest or {}

        metrics_rows = [
            ("Status", lambda e: _status(e.status)),
            ("Made by", lambda e: _e(luck.lineage_of(e.spec).get("op", "") or e.source or e.kind)),
            ("Timeframes", lambda e: _e(", ".join((e.spec or {}).get("timeframes") or
                                                  ((e.spec or {}).get("base_spec") or {}).get("timeframes") or []))),
            ("OOS trades", lambda e: str(_oos(e).get("trades", 0))),
            ("OOS per trade", lambda e: _r(_oos(e).get("expectancy_r"))),
            ("OOS t-statistic", lambda e: f'{_oos(e).get("t_stat", 0):.2f}'),
            ("Win rate", lambda e: f'{_oos(e).get("win_rate", 0):.0%}'),
            ("Profit factor", lambda e: f'{_oos(e).get("profit_factor", 0):.2f}'),
            ("Max drawdown", lambda e: f'{_oos(e).get("max_drawdown_r", 0):.1f}R'),
            ("Walk-forward", lambda e: f'{bt(e)["walk_forward"]["positive"]}/{bt(e)["walk_forward"]["counted"]} windows > 0R'),
            ("Monte Carlo p95 DD", lambda e: f'{bt(e)["monte_carlo"]["max_drawdown_r_p95"]:.1f}R'),
            ("Holdout", lambda e: (f'{bt(e)["holdout"]["trades"]} trades {_r(bt(e)["holdout"]["expectancy_r"])}'
                                   if bt(e)["holdout"].get("evaluated") else "not looked at")),
            ("Luck bar", lambda e: str((bt(e).get("luck") or {}).get("min_t_oos", "2.0 (book)"))),
            ("Stress test", lambda e: ("–" if not bt(e).get("stress") else
                                       "passed" if bt(e)["stress"]["passed"] else "failed")),
            ("Paper trades", lambda e: str((e.paper or {}).get("trades", 0))),
            ("Paper per trade", lambda e: _r((e.paper or {}).get("expectancy_r"))),
            ("Why rejected", lambda e: f'<span class="small">{_e(_why(e))[:300]}</span>' or "–"),
        ]
        head = "".join(f"<th>{_link(e.id)}</th>" for e in chosen)
        body = "".join(f"<tr><td>{_e(name)}</td>" + "".join(f"<td>{cell(e, f)}</td>" for e in chosen) + "</tr>"
                       for name, f in metrics_rows)
        parts.append(f"<table><tr><th></th>{head}</tr>{body}</table>")
        return page("Strategy Arena", "".join(parts), user, token, refresh=None)
