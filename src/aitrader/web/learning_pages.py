"""The learning windows: one page for Spot AI, one for Futures AI, each showing only what that
engine learns from and what it learned: its machine-learning strategies, its own strategy test, its
own AI trade filter and its own backtest teacher. Nothing here touches live trading."""

from __future__ import annotations

import html
import time

from fastapi import Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..learn.wallet import MARKETS

NAMES = {"spot": "Spot AI", "futures": "Futures AI"}
SOURCES = {"spot": "spot candles and Spot AI's own paper trades (long trades only)",
           "futures": "the perpetual futures' own candles, with futures fees and funding, and Futures AI's "
                      "own paper trades (longs and shorts)"}


def _e(x) -> str:
    return html.escape(str(x))


def _r(m: dict) -> str:
    if not m or not m.get("trades"):
        return '<span class="muted">not yet</span>'
    return f'{m.get("trades", 0)} trades · {m.get("expectancy_r", 0):+.3f}R'


def is_ml(entry) -> bool:
    """A machine-learning strategy (ml_boosted, ml_classifier), its short twin or a WIN copy of it."""
    spec = entry.spec or {}
    names = (entry.name, spec.get("base_name", ""), spec.get("builtin", ""),
             (spec.get("base_spec") or {}).get("builtin", "") if isinstance(spec.get("base_spec"), dict) else "")
    return any(str(n).startswith("ml_") or "ml_boosted" in str(n) or "ml_classifier" in str(n) for n in names)


def _engine_entries(reg, market: str):
    from ..learn.picks import learns_for

    return [e for e in reg.entries.values() if learns_for(e, market)]


def _backtest(entry, market: str) -> dict:
    from ..learn.keep import backtest_of

    return backtest_of(entry, market)


def _ml_section(entries, market: str, kept) -> str:
    name = NAMES[market]
    rows = []
    for e in sorted((e for e in entries if is_ml(e)), key=lambda e: e.id):
        bt = _backtest(e, market)
        test = _r(bt.get("out_of_sample") or {})
        if bt.get("failed"):
            test += f'<div class="bad small">failed: {_e("; ".join(bt["failed"])[:140])}</div>'
        elif (bt.get("out_of_sample") or {}).get("trades"):
            test += '<div class="good small">passed</div>'
        paper = ((e.paper or {}).get("markets") or {}).get(market) or {}
        trading = e.status == "paper" and (kept is None or e.id in kept or (e.spec or {}).get("pick"))
        rows.append(f'<tr><td>{_e(e.id)}</td><td>{"short" if e.kind == "short" else "long"}</td>'
                    f'<td class="{"good" if trading else "muted"}">{"trading" if trading else _e(e.status)}</td>'
                    f'<td>{test}</td><td>{_r(paper)}</td></tr>')
    body = ("<table><tr><th>Strategy</th><th>Side</th><th>On paper</th><th>Strategy test</th>"
            f"<th>{name} paper trades</th></tr>{''.join(rows)}</table>" if rows else
            '<p class="muted">No machine-learning strategy is loaded yet. Press "Turn on" for paper trading on the '
            'Trading page (it loads every strategy), or run <code>learn add-builtin ml_boosted</code>.</p>')
    return (f'<div class="card"><h3>Machine-learning strategies: {name}</h3>'
            f'<p class="small muted">These strategies have no hand-written entry rule: a model learns from the '
            f'chart\'s own past when to enter. For {name} each model trains only on {SOURCES[market]}, '
            f'one model per {"perpetual" if market == "futures" else "pair"}. They are retrained '
            'walk-forward (every 250 candles, on the last 1,500) so every result is on candles the model never '
            f'saw.</p>{body}</div>')


def _test_section(entries, market: str, form: str, busy) -> str:
    name = NAMES[market]
    passed = failed = untested = 0
    for e in entries:
        if (e.spec or {}).get("pick"):
            continue  # WIN copies are judged by Keep the winners, not by this test
        bt = _backtest(e, market)  # Futures AI: perpetual results only, never a spot one
        if not bt or "trades" not in (bt.get("out_of_sample") or {}):
            untested += 1
        elif bt.get("failed"):
            failed += 1
        else:
            passed += 1
    where = "the perpetuals' candles" if market == "futures" else "spot candles"
    button = (f'<p class="small">Testing now (<a href="/jobs/{_e(busy["id"])}">progress</a>)</p>' if busy else
              f'<form method="post" action="/learning/test">{form}<button class="ghost">Test every {name} strategy '
              f'on {where}</button></form>')
    return (f'<div class="card"><h3>Strategy test: {name}</h3>'
            f'<p>{passed} passed, {failed} failed, {untested} not tested yet.</p>'
            f'<p class="small muted">{name}\'s own backtest gate, on {where}: in-sample, out-of-sample, '
            'walk-forward, Monte Carlo and the untouched final part. The ranking in "Keep the best" uses these '
            f'results. The other engine\'s test is separate. The button tests the strategies on paper now, then '
            f'every one with no result on {where} yet. '
            'With many strategies it takes hours; the progress link shows each one.</p>'
            f'{button}</div>')


def _teacher_section(paths, market: str) -> str:
    from ..learn.teacher import Teacher
    from ..learn.wallet import engine_path

    name = NAMES[market]
    t = Teacher(engine_path(paths.teacher, market))
    act, cand = t.active, t.candidate
    if act:
        state = f'<b class="good">ON</b>: following lessons {_e(act["version"])}'
    elif cand:
        state = (f'<b class="bad">OFF</b>: latest lessons {_e(cand["version"])} '
                 + ("passed the check and can be turned on" if cand["passed"] else "did not pass the check"))
    else:
        state = '<b class="bad">OFF</b>: nothing learned yet'
    return (f'<div class="card"><h3>Backtest teacher: {name}</h3><p>{state}</p>'
            f'<p class="small muted">Learns in which market situations (trend, volatility, volume) each of '
            f'{name}\'s strategies won or lost, from {SOURCES[market].split(" and ")[0]}.</p>'
            f'<p><a href="/teacher?market={market}">Open {name}\'s teacher</a></p></div>')


OLLAMA_NOTE = ('<div class="card"><h3>Where Ollama fits</h3><p class="small muted">Ollama is the local language '
               'model on this server. It reads the books you submit (turning their rules into strategies) and '
               'writes the news brief. It does not train or run the trading models: those are number models '
               '(gradient-boosted trees and logistic regression) that learn from candles and trade results, '
               'which a language model cannot do better. The strategies Ollama writes from books then go '
               'through each engine\'s own strategy test like every other strategy.</p></div>')


def add_learning_routes(app, *, page, guard, auth, paths, csrf, jobs, ai_card, clock=time.time):
    def _busy(market: str):
        return next((j for j in jobs.list() if j["status"] in ("queued", "running")
                     and j.get("meta", {}).get("kind") == "learn-test"
                     and j.get("meta", {}).get("market") == market), None)

    def learning_page(request: Request, market: str, msg: str = "", bad: str = ""):
        from ..learn import Registry
        from ..learn.keep import kept_ids

        user, token, redirect = guard(request)
        if redirect:
            return redirect
        name, other = NAMES[market], "futures" if market == "spot" else "spot"
        reg = Registry(paths.registry)
        entries = _engine_entries(reg, market)
        back = f"/learning/{market}"
        form = csrf(token) + (f'<input type="hidden" name="market" value="{market}">'
                              f'<input type="hidden" name="back" value="{back}">')
        parts = [f'<p class="{"bad" if bad else "good"}">{_e(msg)}</p>' if msg else "",
                 f'<p class="small muted">Everything {name} learns, in one place. {name} learns only from '
                 f'{SOURCES[market]}; nothing from {NAMES[other]} is shown or used here.</p>',
                 _ml_section(entries, market, kept_ids(paths.registry, market)),
                 _test_section(entries, market, form, _busy(market)),
                 ai_card(paths, jobs.list(), csrf(token), market, back=back),
                 _teacher_section(paths, market),
                 OLLAMA_NOTE]
        return page(f"{name} learning", "".join(parts), user, token, refresh=120)

    @app.get("/learning/spot")
    def learning_spot(request: Request, msg: str = "", bad: str = ""):
        return learning_page(request, "spot", msg, bad)

    @app.get("/learning/futures")
    def learning_futures(request: Request, msg: str = "", bad: str = ""):
        return learning_page(request, "futures", msg, bad)

    @app.post("/learning/test")
    def learning_test(request: Request, csrf: str = Form(""), market: str = Form("")):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        if not auth.check_csrf(token, csrf):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        if market not in MARKETS:
            return PlainTextResponse("unknown engine", status_code=400)
        name = NAMES[market]
        if _busy(market):
            text = f"{name}'s strategies are already being tested; see the progress link."
        else:
            # the ones trading now first, then every one with no result on these candles yet
            jobs.submit(f"{name}: test every strategy on its own candles",
                        [["learn", "test", "--paper", "--market", market],
                         ["learn", "test", "--missing", "--market", market]],
                        meta={"kind": "learn-test", "market": market})
            text = (f"Testing every {name} strategy on its own candles. The results show here when it is done; "
                    "nothing changes in live trading.")
        from urllib.parse import quote

        return RedirectResponse(f"/learning/{market}?msg={quote(text)}", status_code=303)
