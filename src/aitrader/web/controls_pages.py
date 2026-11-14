"""The Controls page: the user controls from the NEXUS UI plan in one place.

Every control writes a setting the engines already read (``learn.guards``, ``learn.wallet``) or the
controls file (``ops.controls``: blocked pairs, automatic generation). Nothing here can unlock live
trading: the page shows the live lock and says how it is opened, at a terminal.
"""

from __future__ import annotations

import html
import time
import urllib.parse

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..clock import local
from ..learn import guards, wallet
from ..ops import controls
from ..trade import futures

LEVEL_TEXT = {
    "conservative": "0.25% risk per trade, pause after a 2% day loss, close all at a 5% week loss, 1x futures leverage",
    "balanced": "0.5% risk per trade, pause after a 3% day loss, close all at a 6% week loss, 2x futures leverage",
    "aggressive": "1% risk per trade (the hard cap), pause after a 5% day loss, close all at a 10% week loss, "
                  "3x futures leverage",
}
NAMES = {"spot": "Spot AI", "futures": "Futures AI"}


def _e(x) -> str:
    return html.escape(str(x))


def add_controls_routes(app, *, settings, page, guard, auth, paths, csrf, clock=time.time):
    cpath = getattr(paths, "controls", None) or controls.path_from_env()
    gpath = guards.path_for(paths.paper_state)
    mpath = wallet.market_path(paths.paper_state)

    def _event(text: str) -> None:
        from ..ops import EventLog

        EventLog(paths.events, service="dashboard").emit("controls", text)

    def _leverage() -> float:
        chosen = wallet.paper_leverage(mpath)
        return futures.clamp_paper_leverage(chosen) if chosen else futures.leverage()

    @app.get("/controls")
    def controls_page(request: Request, msg: str = "", bad: str = ""):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        form = csrf(token)
        g = guards.load(gpath)
        lev = _leverage()
        c = controls.load(cpath)
        auto = c["auto"]
        level = controls.risk_level(g, lev)
        live_open = settings.mode.value == "live" and settings.live_unlocked
        parts = ['<style>.checks{display:flex;flex-wrap:wrap;gap:6px 16px;margin:6px 0 10px}'
                 '.checks label{display:inline-flex;align-items:center;gap:6px;margin:0}</style>',
                 f'<p class="{"bad" if bad else "good"}">{_e(msg)}</p>' if msg else ""]

        # mode and emergency stop
        parts.append(
            '<div class="card"><h3>Mode and emergency stop</h3>'
            f'<p>Mode <b>{_e(settings.mode.value)}</b> · live orders '
            + ('<b class="bad">UNLOCKED</b>' if live_open else '<b class="good">locked</b>') + '</p>'
            '<p class="small muted">Paper trading uses real Bybit prices with simulated fills (Bybit removed its demo '
            'trading in Oct 2026). Live trading can only be unlocked at a terminal on the server: '
            '<code>AITRADER_MODE=live</code>, <code>AITRADER_LIVE_UNLOCKED=true</code> and the confirmation phrase in '
            '<code>.env</code>, plus your <code>golive approve</code> for each strategy. No button here can do that.</p>'
            f'<div class="row"><form method="post" action="/trading/pause">{form}<button>Pause everything</button></form>'
            f'<form method="post" action="/trading/resume">{form}<button class="ghost">Resume</button></form></div>'
            f'<form method="post" action="/trading/kill" class="kill">{form}<label>Kill switch: stops entries, closes '
            'every paper position and sells everything the live executor bought. Type KILL to confirm '
            '<input name="confirm" autocomplete="off" required placeholder="KILL"></label>'
            '<button class="danger">Pull the kill switch</button></form></div>')

        # risk level
        btns = "".join(
            f'<form method="post" action="/controls/risk-level">{form}'
            f'<input type="hidden" name="level" value="{k}"><button class="{"" if level == k else "ghost"}">'
            f'{k.title()}</button></form> ' for k in controls.RISK_LEVELS)
        rows = "".join(
            f'<tr><td>{NAMES[m]}</td><td>{g[m]["risk_pct"] or "auto"}%</td><td>{g[m]["day_pause_pct"]:g}%</td>'
            f'<td>{g[m]["week_close_pct"]:g}%</td><td>{g[m]["open_risk_pct"] or "off"}'
            f'{"%" if g[m]["open_risk_pct"] else ""}</td></tr>' for m in wallet.MARKETS)
        parts.append(
            f'<div class="card"><h3>Risk level: {_e(level.title())}</h3><div class="row">{btns}</div>'
            + "".join(f'<p class="small"><b>{k.title()}</b>: {_e(t)}</p>' for k, t in LEVEL_TEXT.items())
            + '<table><tr><th>Engine</th><th>Risk per trade</th><th>Pause after a day loss of</th>'
              '<th>Close all after a week loss of</th><th>All open trades\' risk at most</th></tr>'
            + rows + f'</table><p class="small">Paper futures leverage: <b>{lev:g}x</b></p>'
            '<p class="small muted">A level fills in these settings for both paper engines. You can still change any '
            'of them below or on the Trading page; the level then shows "Custom". The live trader keeps its own '
            'limits from <code>.env</code> (by default 0.25% per trade, a 1% day loss cap and 1x leverage, never above 3x).</p></div>')

        # limits
        parts.append(
            f'<form method="post" action="/controls/limits" class="card">{form}<h3>Limits</h3>'
            f'<p><label>Maximum leverage (paper futures, 1-{futures.PAPER_MAX_LEVERAGE:g}x; live is capped at '
            f'{futures.MAX_LEVERAGE:g}x in code) <input type="number" name="leverage" min="1" '
            f'max="{futures.PAPER_MAX_LEVERAGE:g}" step="1" value="{lev:g}"></label></p>'
            + "".join(f'<p><label>{NAMES[m]}: maximum daily loss, % of the wallet (then it opens nothing new for '
                      f'{g[m]["pause_hours"]:g} h) <input type="number" name="day_{m}" min="0.5" max="50" step="0.5" '
                      f'value="{g[m]["day_pause_pct"]:g}"></label></p>' for m in wallet.MARKETS)
            + '<button>Save limits</button></form>')

        # timeframes
        tf_rows = "".join(
            f'<p>{NAMES[m]}:</p><div class="checks">' + "".join(
                f'<label class="small"><input type="checkbox" name="tf_{m}" value="{t}"'
                f'{" checked" if t in g[m]["timeframes"] else ""}> {t}</label> ' for t in guards.TIMEFRAMES) + '</div>'
            for m in wallet.MARKETS)
        parts.append(f'<form method="post" action="/controls/timeframes" class="card">{form}<h3>Trading timeframes</h3>'
                     f'{tf_rows}<p class="small muted">The candle timeframes each engine opens new trades on. Open '
                     'trades on the others run to their exits.</p><button>Save timeframes</button></form>')

        # pairs
        blocked = set(c["blocked_pairs"])
        listed = list(dict.fromkeys([*settings.symbols, *sorted(blocked)]))
        boxes = "".join(f'<label class="small"><input type="checkbox" name="allow" value="{_e(p)}"'
                        f'{"" if p in blocked else " checked"}> {_e(p)}</label> ' for p in listed)
        parts.append(
            f'<form method="post" action="/controls/pairs" class="card">{form}<h3>Allowed trading pairs</h3>'
            f'<input type="hidden" name="listed" value="{_e(",".join(listed))}"><div class="checks">{boxes}</div>'
            '<p><label>Also never trade (comma separated, e.g. PEPE, WIF/USDT) '
            '<input name="extra" placeholder="none"></label></p>'
            '<p class="small muted">An unticked pair is never opened by either paper engine, from any list (your pairs, '
            'the ranking, the top gainers or the squeeze scanner). Positions already open on it still run to their '
            'exits. Takes effect within a minute.</p><button>Save pairs</button></form>')

        # automation
        auto_tfs = "".join(f'<label class="small"><input type="checkbox" name="timeframes" value="{t}"'
                           f'{" checked" if t in auto["timeframes"] else ""}> {t}</label> ' for t in controls.TIMEFRAMES)
        last = (f'Last run {local(auto["last_run"])}: {_e(auto["last_result"])}' if auto["last_run"]
                else "Never run yet.")

        def chk(name, on, label):
            return (f'<p><label class="small"><input type="checkbox" name="{name}" value="1"{" checked" if on else ""}> '
                    f'{label}</label></p>')

        parts.append(
            f'<form method="post" action="/controls/auto" class="card">{form}<h3>AI automation</h3>'
            + chk("generate", auto["generate"], "<b>Automatic strategy generation</b>: the Strategy Factory makes new "
                                                "strategies on a schedule")
            + chk("evolve", auto["evolve"], "<b>Strategy evolution</b>: breed children from the best tested ones")
            + chk("shorts", auto["shorts"], "Also make short twins for Futures AI")
            + f'<p><label>Strategy candidates per run <input type="number" name="n" min="{controls.AUTO_N[0]}" '
              f'max="{controls.AUTO_N[1]}" value="{auto["n"]}"></label></p>'
            + f'<p><label>Run every (hours) <input type="number" name="every_hours" min="1" max="168" '
              f'value="{auto["every_hours"]:g}"></label></p>'
            + f'<p>Timeframes to build on:</p><div class="checks">{auto_tfs}</div>'
            + f'<p class="small">{last}</p>'
            + '<p class="small muted">Each run backtests what it made; the ones that pass every test (including the '
              'stress test and the luck bar) go to paper trading as usual. It never trades real money and never '
              'changes risk settings. Needs the <code>factory</code> service running on the server. More candidates per '
              'run raise the bar every new strategy must pass, so 10-20 a day is plenty.</p>'
            + '<button>Save automation</button></form>')
        parts.append(f'<p class="small muted">Last change by {_e(c["by"] or "–")}.</p>' if c["by"] else "")
        return page("Controls", "".join(parts), user, token, refresh=None)

    async def _post(request: Request, action):
        user, token, redirect = guard(request)
        if redirect:
            return redirect
        form = await request.form()
        if not auth.check_csrf(token, form.get("csrf", "")):
            return PlainTextResponse("bad form token; reload the page", status_code=403)
        try:
            text, ok = action(user, form), True
        except ValueError as exc:
            text, ok = f"Not saved: {exc}", False
        return RedirectResponse(f"/controls?msg={urllib.parse.quote(text)}{'' if ok else '&bad=1'}", status_code=303)

    @app.post("/controls/risk-level")
    async def controls_risk_level(request: Request):
        def act(user, form):
            level = str(form.get("level", ""))
            controls.apply_risk_level(level, paths.paper_state, by=user)
            _event(f"risk level set to {level} from the Controls page by {user}")
            return f"Risk level {level.title()} applied to both paper engines."
        return await _post(request, act)

    @app.post("/controls/limits")
    async def controls_limits(request: Request):
        def act(user, form):
            try:
                lev = futures.clamp_paper_leverage(float(form.get("leverage", "")))
                days = {m: float(form.get(f"day_{m}", "")) for m in wallet.MARKETS}
            except ValueError:
                raise ValueError("leverage and daily loss must be numbers") from None
            wallet.set_leverage(mpath, lev, by=user)
            for m, d in days.items():
                guards.save(gpath, m, {"day_pause_pct": d}, by=user)
            g = guards.load(gpath)
            _event(f"limits changed from the Controls page by {user}: leverage {lev:g}x, daily loss "
                   + ", ".join(f"{NAMES[m]} {g[m]['day_pause_pct']:g}%" for m in wallet.MARKETS))
            return (f"Saved: paper futures leverage {lev:g}x; daily loss "
                    + ", ".join(f"{NAMES[m]} {g[m]['day_pause_pct']:g}%" for m in wallet.MARKETS) + ".")
        return await _post(request, act)

    @app.post("/controls/timeframes")
    async def controls_timeframes(request: Request):
        def act(user, form):
            for m in wallet.MARKETS:
                chosen = [t for t in form.getlist(f"tf_{m}") if t in guards.TIMEFRAMES]
                if not chosen:
                    raise ValueError(f"pick at least one timeframe for {NAMES[m]}")
                guards.save(gpath, m, {"timeframes": chosen}, by=user)
            _event(f"trading timeframes changed from the Controls page by {user}")
            return "Trading timeframes saved."
        return await _post(request, act)

    @app.post("/controls/pairs")
    async def controls_pairs(request: Request):
        def act(user, form):
            listed = [p for p in str(form.get("listed", "")).split(",") if p]
            allowed = set(form.getlist("allow"))
            extra = [controls.normalize_pair(p) for p in str(form.get("extra", "")).split(",")]
            bad = [p for p in str(form.get("extra", "")).split(",") if p.strip() and not controls.normalize_pair(p)]
            if bad:
                raise ValueError(f"not a pair: {', '.join(bad)}")
            blocked = [p for p in listed if p not in allowed] + [p for p in extra if p]
            c = controls.save(cpath, {"blocked_pairs": blocked}, by=user)
            _event(f"blocked pairs set from the Controls page by {user}: {', '.join(c['blocked_pairs']) or 'none'}")
            return (f"Never traded now: {', '.join(c['blocked_pairs'])}." if c["blocked_pairs"]
                    else "Every pair is allowed.")
        return await _post(request, act)

    @app.post("/controls/auto")
    async def controls_auto(request: Request):
        def act(user, form):
            tfs = [t for t in form.getlist("timeframes") if t in controls.TIMEFRAMES]
            if not tfs:
                raise ValueError("pick at least one timeframe to build on")
            try:
                n, hours = int(form.get("n", 20)), float(form.get("every_hours", 24))
            except ValueError:
                raise ValueError("candidates and hours must be numbers") from None
            c = controls.save(cpath, {"auto": {"generate": bool(form.get("generate")), "evolve": bool(form.get("evolve")),
                                               "shorts": bool(form.get("shorts")), "n": n, "every_hours": hours,
                                               "timeframes": tfs}}, by=user)
            a = c["auto"]
            on = [x for x, k in (("generation", "generate"), ("evolution", "evolve")) if a[k]]
            _event(f"AI automation set from the Controls page by {user}: {', '.join(on) or 'off'}")
            return (f"Automatic {' and '.join(on)} on: {a['n']} candidates every {a['every_hours']:g} h."
                    if on else "Automatic generation and evolution are off.")
        return await _post(request, act)
