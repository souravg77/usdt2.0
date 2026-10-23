"""Testing while trading: run the strategies that passed the backtest on paper, bar by bar.

Every tick the runner reads the latest *closed* candles that the collector stores, opens a paper
position when a strategy signals on the newest bar, and manages open positions with the same
fill rules as the backtest (``simulate_signal``): entry at the next bar's open plus slippage,
stop first, gaps fill at the open, fees on both sides. Results are kept per strategy and checked
against ``RetireRule``; a strategy whose paper results fall below the bar is retired and takes
no new trades. Trade sizes follow the paper wallet (``learn.wallet``): the starting amount chosen on
the Trading page plus every closed trade since. When a person has promoted a Phase 4 trade-filter model, each new signal is
checked by it first: skipped below its threshold, sized by its confidence above. Each signal
records the market regime of its bar (``aitrader.market.regime``) so results can be split by it;
the regime never blocks a paper trade here. When a person has turned on the backtest teacher's
lessons (``learn.teacher``), a signal in a market situation the strategy lost in is skipped, and a
1R profit-take is added where the lessons found it caught more. No exchange order is ever sent from here.

Two engines run side by side, each with its own wallet (``learn.wallet``), positions and results,
and each can be turned off on the Trading page:

- **Spot AI** paper-trades long signals as spot buys (no leverage, no shorts).
- **Futures AI** paper-trades USDT perpetual futures: short signals (the short twins,
  ``strategies.mirror``) as perp shorts, and long signals as perp longs (key suffix ``|futures``,
  so the same strategy can hold a spot buy and a perp long at once). Perp fees and funding are in
  the result, isolated margin at the chosen leverage (only the margin counts against the futures
  wallet), and a trade whose stop is too close to the liquidation price is skipped.

The AI trade filter judges shorts as the long on the flipped chart (``ai.features.as_long``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone

from ..backtest import BacktestConfig, metrics
from ..backtest.engine import Trade, as_perp, capped, cost_r, simulate_signal, too_costly, too_little_reward
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from ..market import perp_limits
from ..market.regime import breakdown, classify
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal
from ..trade import futures
from . import guards, keep, picks, score, wallet
from .lab import with_context
from .registry import Registry, make_strategy

log = logging.getLogger(__name__)

DEFAULT_STATE = "data/learn/paper_state.json"
WINDOW_BARS = 1000  # history loaded per market, enough to warm up EMA200 and friends
MIN_BARS = 250


def history_start(tf: str, now_ms: int | None = None) -> int:
    """How far back the collector keeps ``tf`` candles so a chart has the ``MIN_BARS`` the paper
    trader needs (with a margin for gaps)."""
    now_ms = now_ms or int(time.time() * 1000)
    return now_ms - (MIN_BARS + 50) * TIMEFRAME_MS[tf]
MAX_DATA_AGE_MS = 15 * 60_000  # a signal on a candle that closed longer ago than this (or a bar) is not taken


def max_data_age(tf: str) -> int:
    """How old a just-arrived candle may be before its signal is skipped as stale: 15 minutes, or one
    bar on longer timeframes, at most an hour."""
    return max(MAX_DATA_AGE_MS, min(TIMEFRAME_MS[tf], 3_600_000))


FUTURES_SUFFIX = "|futures"  # a perp long's position key: the same signal's spot buy keeps the plain key
MISSING_ENTRY_BARS = 3  # bars to wait for a missing entry candle to be filled in before moving on


@dataclass(frozen=True)
class RetireRule:
    min_trades: int = 20
    min_expectancy: float = 0.0  # R per trade after costs, judged once min_trades are in
    max_drawdown: float = 10.0  # R; retire at once if paper drawdown goes past this


@dataclass(frozen=True)
class PaperConfig:
    start_equity: float = 10_000.0
    max_position_frac: float = 0.5  # one position is worth at most half the account (shorts too)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    retire: RetireRule = field(default_factory=RetireRule)


HAND_PRICE_MS = 120_000  # a live price sent with a Close press is used for this long


def _asked_to_close(req: dict | None, pos: dict) -> bool:
    """A person's close request covers this open position (not a later one on the same key)."""
    if not isinstance(req, dict) or not pos:
        return False
    ts = req.get("signal_ts")
    return ts is None or ts == (pos.get("signal") or {}).get("ts")


class PaperTrader:
    def __init__(self, store: CandleStore, registry_path: str, exchanges: list[str],
                 symbols: list[str], cfg: PaperConfig | None = None,
                 state_path: str = DEFAULT_STATE, calendar=None, ai_filter=None, control=None,
                 context=None, events=None, news=None, teacher=None, market_symbols=None,
                 futures_store: CandleStore | None = None):
        self.store, self.registry_path = store, registry_path
        # Futures AI reads only USDT perpetual candles (``data.perp``), from their own database;
        # without one (tests) both engines read ``store``
        self.futures_store = futures_store or store
        # ``symbols`` may be a function returning the list (the pair ranking, read each tick)
        self.exchanges, self.symbols = exchanges, symbols
        # ``market_symbols(market)``: the pairs Spot AI or Futures AI may open on (each adds its own
        # scanner's top gainers); None means both use ``symbols``
        self.market_symbols = market_symbols
        self.cfg = cfg or PaperConfig()
        self.state_path, self.calendar = state_path, calendar
        self.wallet_path = wallet.wallet_path(state_path)  # the Spot wallet's starting amount
        self.futures_wallet_path = wallet.wallet_path(state_path, "futures")
        self.market_path = wallet.market_path(state_path)  # the futures leverage chosen
        self.engines_path = wallet.engines_path(state_path)  # Spot AI / Futures AI on or off
        self.limits_path = wallet.limits_path(state_path)  # each engine's daily loss and open-position limits
        self.filters_path = wallet.filters_path(state_path)  # whether each engine uses the AI filter / teacher
        self.guards_path = guards.path_for(state_path)  # each engine's loss breakers, min reward, trend, split TP
        self._guards = guards.load(self.guards_path)
        self._trends: dict = {}  # (exchange, symbol, timeframe) -> trend of a higher timeframe, this pass
        self._returns: dict = {}  # (exchange, symbol) -> hourly returns of the last week, this pass
        self._now = 0
        self.perp_limits_path = perp_limits.DEFAULT_PATH  # each coin's maximum leverage on Bybit
        self.ai_filter = ai_filter  # Phase 4 trade filter (aitrader.ai.LiveFilter) or None
        self.control = control  # trade.Control: pause / kill switch, or None
        self.context = context  # market-context filter (funding, open interest...), or None
        self.events = events  # ops.EventLog for alerts and the stability record, or None
        self.news = news  # news.NewsFilter (AI web-search brief): can only shrink or pause entries
        self.teacher = teacher  # learn.teacher.TeacherFilter: follows the lessons a person turned on
        self.state = {"open": {}, "trades": [], "equity": {}, "last_signal": {}}
        self._seen: dict = {}  # market -> newest candle close already evaluated (this process)
        self._logged = False  # the first pass after a start is always in the scan log
        self.scan_path = wallet.scan_request_path(state_path)  # the Scan now button's request
        self.autoscan_path = wallet.autoscan_path(state_path)  # the Auto scan setting
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as fh:
                self.state.update(json.load(fh))

    # ---- one pass -------------------------------------------------------------------------------

    def tick(self, now_ms: int | None = None, manual: dict | None = None) -> dict:
        """One pass over the charts. ``manual`` (a Scan now request: who asked) re-reads every chart's
        newest closed candle instead of only the ones that closed since the last pass; a signal
        already handled is never taken twice."""
        now_ms = now_ms or int(time.time() * 1000)
        reg = Registry(self.registry_path)  # re-read: `learn` commands may have changed it
        events = {"opened": [], "closed": [], "retired": [], "filtered": []}
        self._now = now_ms
        self._guards, self._trends, self._returns = guards.load(self.guards_path), {}, {}
        # this pass's signals per engine, for the scan log: {market: {"opened": n, "skipped": n, "why": {...}}}
        self._pass_log = {m: {"opened": 0, "skipped": 0, "why": {}} for m in wallet.MARKETS}
        # why an engine read no chart this pass, for the scan log: {market: {reason: set of what}}
        idle: dict = {m: {} for m in wallet.MARKETS}
        for m in wallet.MARKETS:  # trip, hold or clear each engine's loss breaker
            br = guards.update_breaker(self.state, self._wallet(m), m, self._guards[m], now_ms)
            if br and br.get("at") == now_ms:
                log.warning("paper breaker: %s", br["why"])
                if self.events is not None:
                    self.events.emit("paper_breaker", br["why"], level="warning", market=m)
        self._manage_open(reg, now_ms, events)
        allow, size_mult, cal_why = self._calendar(now_ms)
        blocked = "" if allow else f"the economic-calendar filter: {cal_why}"
        # a person's pause or a kill stops paper entries; a live safety halt or loss-cap pause
        # doesn't, so paper keeps collecting the results the go-live gate needs
        if self.control is not None and getattr(self.control, "stops_paper", self.control.paused):
            allow, blocked = False, "a pause or the kill switch"
        self.state["blocked"] = {"why": blocked, "at": now_ms} if blocked else {}
        frames: dict = {}  # (exchange, symbol, timeframe) -> (frame, newest close, new candle since last tick)
        regimes: dict = {}  # (exchange, symbol, timeframe) -> regime of the newest closed bar
        symbols = self._symbols()
        per_market = {m: set(symbols if self.market_symbols is None else self._symbols(m)) for m in wallet.MARKETS}
        # each engine's timeframe picks: on a picked timeframe only its WIN copies trade there
        tf_picks = {m: picks.load(picks.picks_path(self.registry_path, m)) for m in wallet.MARKETS}
        engines = wallet.engines(self.engines_path)  # the Trading page's Spot AI / Futures AI switches
        # each engine's "WIN strategies only" switch and its use of the AI filter and the teacher
        only_winners = {m: picks.winners_only(self.registry_path, m) for m in wallet.MARKETS}
        # each engine's own "Keep only the best" ranking: a strategy kept by one never trades the other
        kept = {m: keep.kept_ids(self.registry_path, m) for m in wallet.MARKETS}
        uses = wallet.filters(self.filters_path)
        for m in wallet.MARKETS:
            if not engines[m]:
                idle[m]["it is turned off"] = {m}
            elif not per_market[m]:
                idle[m]["no pairs in its list" + (" (is the Futures top gainers scanner on?)" if m == "futures" else "")] = {m}
        strategies_on = {m: 0 for m in wallet.MARKETS}  # strategies each engine may trade
        tf_on = {m: 0 for m in wallet.MARKETS}  # ...that trade one of its ticked timeframes
        built, broken, checks = 0, 0, 0  # what this pass looked at, for the dashboard
        checks_by = {m: 0 for m in wallet.MARKETS}
        pending: list[tuple] = []  # (key, engine, signal, position) passing every check: opened after the pass
        for entry in reg.with_status("paper"):
            benched = self.state.get("benched") or {}
            trades = {m: picks.trades_now(entry, only_winners[m], m, kept[m]) and entry.id not in (benched.get(m) or {})
                      for m in wallet.MARKETS}
            if not any(trades.values()):
                continue
            try:
                strat = make_strategy(entry)
            except Exception:  # one broken strategy must not stop the others
                log.exception("can't build %s; skipping it", entry.id)
                broken += 1
                continue
            built += 1
            for m in wallet.MARKETS:
                strategies_on[m] += trades[m]
                tf_on[m] += trades[m] and any(t in self._guards[m]["timeframes"] for t in strat.spec.timeframes)
            for ex in self.exchanges:
                for sym in symbols:
                    live = {m: engines[m] and trades[m] and sym in per_market[m] for m in wallet.MARKETS}
                    # the timeframes each engine trades: its pick's, and only those chosen on the Trading page
                    tfs = {m: [t for t in picks.allowed(entry, strat.spec.timeframes, tf_picks[m])
                               if t in self._guards[m]["timeframes"]] for m in wallet.MARKETS}
                    for tf in [t for t in strat.spec.timeframes if any(t in tfs[m] for m in wallet.MARKETS)]:
                        on = {m: live[m] and tf in tfs[m] for m in wallet.MARKETS}
                        key = f"{entry.id}|{ex}|{sym}|{tf}"
                        for mkt in [m for m in wallet.MARKETS if on[m]]:
                            # a long takes the plain key on Spot and key|futures on Futures; a short
                            # (Futures only) takes the plain key
                            slots = [key] if mkt == "spot" else [key, key + FUTURES_SUFFIX]
                            market = (mkt, ex, sym, tf)
                            if not allow:
                                continue
                            if all(k in self.state["open"] for k in slots):
                                idle[mkt].setdefault("a position is already open", set()).add(key)
                                continue  # each engine its own candles: spot or perpetual
                            if market not in frames:
                                df = self._frame(ex, sym, tf, now_ms - WINDOW_BARS * TIMEFRAME_MS[tf], now_ms, mkt)
                                close = int(df["ts"].iloc[-1]) + TIMEFRAME_MS[tf] if df is not None and len(df) else None
                                frames[market] = (df, close, close is not None
                                                  and (manual is not None or self._seen.get(market) != close))
                            df, newest_close, fresh = frames[market]
                            # signals only appear when a candle closes: skip markets with no new candle
                            if df is None:
                                idle[mkt].setdefault("no candles collected", set()).add(market)
                                continue
                            if len(df) < MIN_BARS:
                                idle[mkt].setdefault(f"fewer than {MIN_BARS} candles", set()).add(market)
                                continue
                            if not fresh:
                                idle[mkt].setdefault("no new candle", set()).add(market)
                                continue
                            checks += 1
                            checks_by[mkt] += 1
                            try:
                                sdf = with_context(strat, df, self.store, ex, sym, tf)
                                sigs = [s for s in strat.generate(sdf, ex, sym, tf) if s.ts == newest_close]
                            except Exception:  # one strategy failing on one market must not stop the rest
                                log.exception("%s failed on %s %s %s; skipping it this candle", entry.id, ex, sym, tf)
                                continue
                            seen_key = key if mkt == "spot" else key + FUTURES_SUFFIX
                            if not sigs or sigs[-1].ts <= self.state["last_signal"].get(seen_key, 0):
                                continue
                            sig = sigs[-1]
                            self.state["last_signal"][seen_key] = sig.ts
                            late = now_ms - newest_close
                            if late > max_data_age(tf):  # the candle arrived late: its prices are old now
                                if market not in self._seen or manual is not None:
                                    # the first look at this market since the trader started (a restart or
                                    # update) or since the pair joined the list: a candle that closed hours
                                    # ago is history, not a missed trade, so it isn't listed as one
                                    continue
                                self._log_signal(key, sig, "stale prices",
                                                 f"the candle closed {late / 60_000:.0f} min ago", market=mkt)
                                events["filtered"].append(key)
                                continue
                            legs = self._legs(key, sig, sym, {m: m == mkt for m in wallet.MARKETS}, events)
                            if not legs:
                                continue
                            ctx_mult = self._context(ex, sym, now_ms, sig.side.value)
                            if ctx_mult <= 0:
                                for k, _perp in legs:
                                    self._log_signal(k, sig, "market context or news filter")
                                    events["filtered"].append(k)
                                continue
                            # each engine uses the AI filter and the teacher only if it is set to
                            ai_legs = [lg for lg in legs if uses["futures" if lg[1] else "spot"]["ai"]]
                            ai = self._ai_check(sig, entry.id, df) if ai_legs else _NO_AI
                            if not ai["take"]:
                                self._note_filtered(key, sig, ai)
                                for k, _perp in ai_legs:
                                    self._log_signal(k, sig, "AI filter")
                                    events["filtered"].append(k)
                                legs = [lg for lg in legs if lg not in ai_legs]
                            tch_legs = [lg for lg in legs if uses["futures" if lg[1] else "spot"]["teacher"]]
                            tch = self._teacher_check(sig, entry.id, df) if tch_legs else _no_teacher(sig)
                            if not tch["take"]:
                                for k, _perp in tch_legs:
                                    self._log_signal(k, sig, "backtest teacher")
                                    events["filtered"].append(k)
                                log.info("paper signal %s skipped: the backtest teacher %s says avoid %s",
                                         key, tch["version"], tch["situation"])
                                legs = [lg for lg in legs if lg not in tch_legs]
                            if not legs:
                                continue
                            if market not in regimes:
                                regimes[market] = _regime(df, tf)
                            try:  # the signal's quality score (the confirmation part is added after the pass)
                                quality = score.parts(df, sig.side is Side.SELL, self._higher_trends(ex, sym, tf, mkt))
                            except Exception:  # a score that can't be read must not stop the signal
                                log.exception("can't score %s on %s %s", entry.id, sym, tf)
                                quality = None
                            for k, perp in legs:
                                leg_mkt = "futures" if perp else "spot"
                                leg_ai = ai if (k, perp) in ai_legs else _NO_AI
                                leg_tch = tch if (k, perp) in tch_legs else _no_teacher(sig)
                                leg_sig = leg_tch["signal"]
                                min_rr = self._guards[leg_mkt]["min_rr"]
                                if leg_sig is not sig and too_little_reward(leg_sig, self._bcfg(perp, min_rr=min_rr)):
                                    # the teacher's shorter target must clear the minimum reward too
                                    self._log_signal(k, leg_sig, "reward too small",
                                                     f"the teacher's target is {leg_sig.r_multiple_target:.2f}R away, "
                                                     f"minimum {min_rr:g}R")
                                    events["filtered"].append(k)
                                    continue
                                pending.append((k, leg_mkt, leg_sig, {
                                    "entry_id": entry.id, "signal": _sig_dict(leg_sig),
                                    "situation": leg_tch["situation"], "teacher": leg_tch["version"],
                                    "size_mult": size_mult * ctx_mult * leg_ai["size"],
                                    "filled": False,
                                    "ai_prob": leg_ai["prob"], "ai_version": leg_ai["version"],
                                    "regime": regimes[market], "perp": perp, "quality": quality,
                                    "split_tp": self._guards[leg_mkt]["split_tp"],
                                    "breakeven": self._guards[leg_mkt]["breakeven"],
                                    "maker_entry": self._guards[leg_mkt]["maker_entry"]}))
        self._open_agreed(pending, events)
        for market, (_df, close, _fresh) in frames.items():
            self._seen[market] = close
        newest = max((c for _d, c, _f in frames.values() if c), default=None)
        self.state["last_pass"] = {"at": now_ms, "strategies": built, "broken": broken, "pairs": len(symbols),
                                   "markets": len({m[1:] for m in frames}), "new_candles": sum(1 for *_x, f in frames.values() if f),
                                   "checks": checks, "newest_candle": newest,
                                   # the shortest timeframe read: on 4h charts alone a candle closes every 4 hours
                                   "step": min((TIMEFRAME_MS[m[3]] for m in frames), default=None)}
        for m in wallet.MARKETS:
            if engines[m] and per_market[m] and not strategies_on[m]:
                idle[m]["no strategy is set to trade on it (WIN only or Keep the best)"] = {m}
            elif engines[m] and per_market[m] and not tf_on[m]:
                idle[m]["no strategy trades its ticked timeframes"] = {m}
            self._pass_log[m]["idle"] = {why: len(what) for why, what in idle[m].items()}
            self._pass_log[m]["thin"] = sorted(f"{x[2]} {x[3]}" for x in idle[m].get(f"fewer than {MIN_BARS} candles", ()))[:8]
        if checks or blocked or manual is not None or not self._logged:
            self._log_scan(now_ms, built, len(symbols), checks_by, blocked, manual)
            self._logged = True
        if checks:
            log.info("paper pass: %d strategies checked %d new candles (%d markets), %d opened, %d skipped",
                     built, checks, len(frames), len(events["opened"]), len(events["filtered"]))
        self._score(reg, events)
        self._save_registry(reg, events["retired"])
        self.save()
        self._emit(events)
        return events

    def run(self, interval: float = 60.0, stop_after: int | None = None) -> None:
        """A pass every ``interval`` seconds. With Auto scan on (the Scan log page) it also looks every
        few seconds, and runs a pass as soon as the collector has stored a new candle."""
        from ..ops import Heartbeat

        beat = Heartbeat(self.events) if self.events is not None else None
        if self.events is not None:
            self.events.emit("start", "paper service started")
        n, last, seen = 0, None, None  # passes run, when the last began, candle databases' versions then
        full = time.monotonic()  # when the last full scan (Scan now or Auto scan) ran
        try:
            while stop_after is None or n < stop_after:
                manual = wallet.take_scan_request(self.scan_path)
                auto = wallet.autoscan(self.autoscan_path)
                if manual is None and auto >= wallet.FULL_SCAN_SECONDS and time.monotonic() - full >= auto:
                    manual = {"by": "", "auto": auto}  # Auto scan's full scan is due
                if manual is not None:
                    full = time.monotonic()
                versions = self._versions()
                if (manual is not None or last is None or time.monotonic() - last >= interval - 0.5
                        or versions != seen or self._close_waiting()):
                    last, seen = time.monotonic(), versions
                    try:
                        ev = self.tick(manual=manual)
                        if any(ev.values()):
                            log.info("paper tick: %s", {k: len(v) for k, v in ev.items()})
                    except Exception as exc:  # keep the service alive; the next tick retries
                        log.exception("paper tick failed")
                        if self.events is not None:
                            self.events.emit("error", f"paper tick failed: {exc!r}", level="error")
                    if beat is not None:
                        beat.beat(open=len(self.state["open"]))
                    n += 1
                if stop_after is None or n < stop_after:
                    left = max(0.0, interval - (time.monotonic() - last))
                    self._wait(min(auto, left) if 0 < auto < wallet.FULL_SCAN_SECONDS else left)
        except BaseException as exc:
            if self.events is not None and not isinstance(exc, KeyboardInterrupt):
                self.events.emit("crash", f"paper service stopped: {exc!r}", level="error")
            raise

    def _versions(self) -> tuple:
        """Both candle databases' write counters: unchanged means no new candle to look at."""
        try:
            return self.store.version(), self.futures_store.version()
        except Exception:  # can't tell: treat it as new candles
            return (time.monotonic(),)

    def _wait(self, interval: float) -> None:
        """Sleep until the next pass, waking early when someone presses Scan now."""
        end = time.monotonic() + interval
        while (left := end - time.monotonic()) > 0:
            if os.path.exists(self.scan_path) or self._close_waiting():
                return
            time.sleep(min(1.0, left))

    # ---- internals ------------------------------------------------------------------------------

    def _symbols(self, market: str | None = None) -> list[str]:
        src = self.symbols if market is None else (lambda: self.market_symbols(market))
        if not callable(src):
            return list(src)
        try:
            return list(src())
        except Exception:  # a broken ranking file must not stop paper trading
            log.exception("can't read the pair list; skipping new entries this tick")
            return []

    def _frame(self, ex, sym, tf, start, now_ms, market: str = "spot"):
        """Closed candles of ``sym``: spot ones for Spot AI, the perpetual's for Futures AI."""
        step = TIMEFRAME_MS[tf]
        db = self.futures_store if market == "futures" else self.store
        candles = [c for c in db.load(ex, sym, tf, start) if c.ts + step <= now_ms]
        return candles_to_frame(candles) if candles else None

    def _price_now(self, ex, sym, market: str, now_ms: int, req: dict | None = None) -> tuple[float, int] | None:
        """The freshest price for closing by hand: the live Bybit price the dashboard read when the
        Close button was pressed (if under 2 minutes old), else the newest saved 1-minute candle's
        close (the collector keeps it a minute or two behind). None when neither is there."""
        if isinstance(req, dict) and req.get("price") and now_ms - int(req.get("price_at") or 0) <= HAND_PRICE_MS:
            return float(req["price"]), int(req["price_at"])
        for tf in ("1m", "5m", "15m"):
            df = self._frame(ex, sym, tf, now_ms - 30 * TIMEFRAME_MS[tf], now_ms, market)
            if df is not None and len(df):
                return float(df["close"].iloc[-1]), int(df["ts"].iloc[-1]) + TIMEFRAME_MS[tf]
        return None

    def _close_waiting(self) -> bool:
        """A person pressed Close or Close all: the paper trader acts on it straight away."""
        if self.control is None:
            return False
        try:
            asked = getattr(self.control, "close_all_requested", None)
            one = getattr(self.control, "close_requests", None)
            return bool((callable(asked) and asked() is not None) or (callable(one) and one()))
        except Exception:
            return False

    def _context(self, exchange: str, symbol: str, now_ms: int, side: str = "buy") -> float:
        """Size multiplier from the market-context and news filters (0 = no new entry). The
        market-context filter reads funding and open interest the other way round for a short."""
        mult = 1.0
        for name, f, args, kw in (("market-context", self.context, (exchange, symbol, now_ms), {"side": side}),
                                  ("news", self.news, (symbol, now_ms), {})):
            if f is None:
                continue
            try:
                d = f.decide(*args, **kw)
            except Exception:  # missing context data must never stop paper trading
                log.exception("%s filter failed; ignoring it this tick", name)
                continue
            if not d.allow_entries:
                return 0.0
            mult *= d.size_multiplier
        return mult

    def _emit(self, events: dict) -> None:
        if self.events is None:
            return
        for key in events["opened"]:
            sig = self.state["open"][key]["signal"]
            ai = self.state["open"][key].get("ai_prob")
            regime = (self.state["open"][key].get("regime") or {}).get("label")
            side = ("SELL (short, perp)" if sig.get("side") == Side.SELL.value else
                    "BUY (perp)" if self.state["open"][key].get("perp") else "BUY")
            self.events.emit("trade_open", f"paper {side} signal {key} entry~{sig['entry']:.6g} "
                             f"stop {sig['stop']:.6g}" + (f" (AI p={ai:.2f})" if ai is not None else "")
                             + (f" regime {regime}" if regime else ""),
                             key=key, mode="paper",
                             market="futures" if self.state["open"][key].get("perp") or sig.get("side") == Side.SELL.value
                             else "spot")
        for key in events["closed"]:
            t = next((t for t in reversed(self.state["trades"]) if t["key"] == key), None)
            if t:
                self.events.emit("trade_close", f"paper close {key} {t['exit_reason']} {t['r']:+.2f}R "
                                 f"({t['pnl_usdt']:+.2f} USDT)", key=key, mode="paper", r=t["r"],
                                 market=t.get("market") or wallet.market_of(t))
        for sid in events["retired"]:
            self.events.emit("error", f"paper: strategy {sid} retired by its results", level="warning")

    def _wallet(self, market: str) -> dict:
        return wallet.load(self.wallet_path if market == "spot" else self.futures_wallet_path)

    def _legs(self, key: str, sig: Signal, sym: str, engines: dict, events: dict) -> list[tuple[str, bool]]:
        """Where a signal is traded: [(position key, perp)]. A long is a spot buy on Spot AI and a
        perp long on Futures AI; a short is a perp short on Futures AI only. A leg is left out when
        its engine is off, its slot is taken, its stop is too near liquidation, or fees would eat
        too much of the risk (each skip is logged)."""
        short = sig.side is Side.SELL
        wanted = []
        if engines["futures"]:
            wanted.append((key if short else key + FUTURES_SUFFIX, True))
        if engines["spot"] and not short:
            wanted.insert(0, (key, False))
        legs = []
        lim = wallet.limits(self.limits_path)
        for k, perp in wanted:
            if k in self.state["open"]:
                continue
            mkt = "futures" if perp else "spot"
            now = self._now or int(time.time() * 1000)
            why = wallet.entry_block(self.state, self._wallet(mkt), mkt, lim[mkt], now)
            if why:
                self._log_signal(k, sig, "engine limit", why)
                events["filtered"].append(k)
                continue
            why = guards.block(self.state, mkt, now)
            if why:
                self._log_signal(k, sig, "loss breaker", why)
                events["filtered"].append(k)
                continue
            why = guards.streak_block(self.state, mkt, self._guards[mkt])
            if why:
                self._log_signal(k, sig, "losing streak", why)
                events["filtered"].append(k)
                continue
            g = self._guards[mkt]
            why = self._exposure_block(mkt, key.split("|")[1], sym, short, g)
            if why:
                self._log_signal(k, sig, "exposure cap", why)
                events["filtered"].append(k)
                continue
            if perp and not futures.safe_leverage(sig.entry, sig.stop, short, self._lev(sym), self._mmr(sym)):
                # a stop too wide for the setting trades at lower leverage (at the fill); only one
                # that even 1x can't hold before liquidation is skipped
                self._log_signal(k, sig, "stop too close to liquidation", "even at 1x")
                events["filtered"].append(k)
                log.info("paper %s %s skipped: stop %.6g too close to liquidation even at 1x",
                         "short" if short else "perp long", k, sig.stop)
                continue
            bcfg = self._bcfg(perp, min_rr=g["min_rr"])
            if too_costly(sig, bcfg):  # fees would eat too much of the risk
                self._log_signal(k, sig, "fees too high for the stop",
                                 f"fees and slippage {cost_r(sig, bcfg):.2f}R of the risk, "
                                 f"limit {bcfg.max_cost_r:.2f}R")
                events["filtered"].append(k)
                log.info("paper signal %s skipped: fees and slippage are %.2fR of the risk (limit %.2fR)",
                         k, cost_r(sig, bcfg), bcfg.max_cost_r)
                continue
            if too_little_reward(sig, bcfg):
                self._log_signal(k, sig, "reward too small",
                                 f"target {sig.r_multiple_target:.2f}R away, minimum {g['min_rr']:g}R")
                events["filtered"].append(k)
                continue
            if g["trend"]:
                ex, _sym, tf = key.split("|")[1:4]
                bad = guards.against(sig.side is Side.SELL, self._higher_trends(ex, sym, tf, mkt))
                if bad:
                    self._log_signal(k, sig, "against the higher-timeframe trend",
                                     f"the {bad} trend points {'up' if sig.side is Side.SELL else 'down'}")
                    events["filtered"].append(k)
                    continue
            legs.append((k, perp))
        return legs

    def _bcfg(self, perp: bool, min_rr: float = 0.0, split_tp: bool = False,
              maker_entry: bool = False, breakeven: bool = False, max_rr: float = 0.0) -> BacktestConfig:
        base = as_perp(self.cfg.backtest) if perp else self.cfg.backtest
        if min_rr or split_tp or maker_entry or max_rr:
            return replace(base, min_rr=min_rr, split_tp=split_tp, maker_entry=maker_entry,
                           breakeven=split_tp and breakeven, max_rr=max_rr)
        return base

    def _exposure_block(self, mkt: str, ex: str, sym: str, short: bool, g: dict) -> str:
        """Why one more position on this side would be too much for the engine, or "": its cap of
        longs or shorts, or too many open positions on the same side that move with ``sym``."""
        name = "Futures AI" if mkt == "futures" else "Spot AI"
        mine = [(k, o) for k, o in self.state["open"].items()
                if ("futures" if k.endswith(FUTURES_SUFFIX) or wallet.market_of(o) == "futures" else "spot") == mkt
                and ((o.get("signal") or {}).get("side") == "sell") == short]
        cap = int(g["max_short"] if short else g["max_long"])
        side = "short" if short else "long"
        if len(mine) >= cap:
            return f"{name} already holds {len(mine)} {side} positions (its limit is {cap})"
        others = {k.split("|")[2] for k, _o in mine} - {sym}
        if len(others) < int(g["max_correlated"]):
            return ""
        mine_r = self._hourly_returns(ex, sym, mkt)
        if mine_r is None:
            return ""
        close = [o for o in sorted(others) if (r := self._hourly_returns(ex, o, mkt)) is not None
                 and guards.correlation(mine_r, r) >= g["corr_min"]]
        if len(close) >= int(g["max_correlated"]):
            return (f"{name} already holds {len(close)} {side} positions that move with {sym} "
                    f"(correlation {g['corr_min']:g} or more over the last week: {', '.join(close[:5])})")
        return ""

    def _hourly_returns(self, ex: str, sym: str, market: str = "spot"):
        """Hourly returns of the last week, indexed by time (read once per pass), or None."""
        if (market, ex, sym) not in self._returns:
            now = self._now or int(time.time() * 1000)
            df = self._frame(ex, sym, "1h", now - (guards.CORR_BARS + 1) * TIMEFRAME_MS["1h"], now, market)
            self._returns[(market, ex, sym)] = (None if df is None or len(df) < 48 else
                                                df.set_index("ts")["close"].pct_change().dropna())
        return self._returns[(market, ex, sym)]

    def _higher_trends(self, ex: str, sym: str, tf: str, market: str = "spot") -> dict:
        """{higher timeframe: "up" / "down" / "flat"} for a signal on ``tf`` (read once per pass)."""
        out = {}
        now = self._now or int(time.time() * 1000)
        for htf in guards.HIGHER.get(tf, ()):
            if (market, ex, sym, htf) not in self._trends:
                df = self._frame(ex, sym, htf, now - guards.TREND_BARS * TIMEFRAME_MS[htf], now, market)
                self._trends[(market, ex, sym, htf)] = guards.trend_of(df)
            out[htf] = self._trends[(market, ex, sym, htf)]
        return out

    def _lev(self, symbol: str = "") -> float:
        """The Trading page's paper leverage when chosen (up to Bybit's maximum for ``symbol``),
        else AITRADER_LEVERAGE (1x-3x)."""
        chosen = wallet.paper_leverage(self.market_path)
        if not chosen:
            return futures.leverage()
        return futures.clamp_paper_leverage(chosen, perp_limits.max_leverage(symbol, self.perp_limits_path))

    def _mmr(self, symbol: str) -> float:
        """``symbol``'s maintenance margin rate on Bybit (from its maximum leverage)."""
        return futures.maintenance_rate(perp_limits.max_leverage(symbol, self.perp_limits_path))

    def _ai_check(self, sig: Signal, entry_id: str, df) -> dict:
        if self.ai_filter is None:
            return {"take": True, "size": 1.0, "prob": None, "version": None}
        recent = [t["r"] for t in self.state["trades"]
                  if t["entry_id"] == entry_id and t["exit_ts"] < sig.ts]
        try:
            return self.ai_filter.check(sig, entry_id, df, recent)
        except Exception:  # a broken model must not stop paper trading; trade unfiltered
            log.exception("trade filter failed on %s; taking the signal unfiltered", entry_id)
            return {"take": True, "size": 1.0, "prob": None, "version": None}

    def _teacher_check(self, sig: Signal, entry_id: str, df) -> dict:
        passthrough = _no_teacher(sig)
        if self.teacher is None:
            return passthrough
        try:
            return self.teacher.check(sig, entry_id, df)
        except Exception:  # broken lessons must not stop paper trading; take the signal as is
            log.exception("backtest teacher failed on %s; taking the signal as is", entry_id)
            return passthrough

    def _log_scan(self, now_ms: int, strategies: int, pairs: int, checks_by: dict, blocked: str,
                  manual: dict | None = None) -> None:
        """One row per pass that read new candles (or was blocked), for the Scan log page: what each
        engine checked and what became of its signals. A blocked pass is logged once per reason."""
        scans = self.state.setdefault("scans", [])
        if (not any(checks_by.values()) and manual is None and self._logged and scans
                and scans[-1].get("blocked") == blocked):
            return
        row = {"at": now_ms, "strategies": strategies, "pairs": pairs, "blocked": blocked,
               "engines": {m: {"charts": checks_by[m], **self._pass_log[m]} for m in wallet.MARKETS}}
        if manual is not None and manual.get("auto"):
            row["auto"] = int(manual["auto"])
        elif manual is not None:
            row["manual"] = str(manual.get("by") or "")
        elif not self._logged:
            row["started"] = True
        scans.append(row)
        del scans[:-300]

    def _open_agreed(self, pending: list[tuple], events: dict) -> None:
        """Open this pass's signals. With an engine's ``agree`` rule above 1, a coin is only traded
        when at least that many different strategies signal the same side on it in the same pass
        (the same candle close, any timeframe), and then once: by the agreeing signal with the
        furthest target in R (the first one on a tie)."""
        groups: dict[tuple, list[tuple]] = {}
        for c in pending:
            k, mkt, sig, _pos = c
            groups.setdefault((mkt, sig.exchange, sig.symbol, sig.side.value), []).append(c)
        take = set()
        for (mkt, _ex, sym, side), cands in groups.items():
            need = int(self._guards[mkt]["agree"])
            if need <= 1:
                take.update(c[0] for c in cands)
                continue
            ids = sorted({c[3]["entry_id"] for c in cands})
            if len(ids) < need:
                for k, _m, sig, _pos in cands:
                    self._log_signal(k, sig, "strategies don't agree",
                                     f"{len(ids)} of the {need} strategies needed signalled {side} {sym} on this candle")
                    events["filtered"].append(k)
                continue
            best = max(cands, key=lambda c: c[2].r_multiple_target or 0.0)
            take.add(best[0])
            best[3]["agreed"] = ids
            for k, _m, sig, _pos in cands:
                if k != best[0]:
                    self._log_signal(k, sig, "another agreeing strategy took it",
                                     f"{', '.join(ids)} agreed; {best[3]['entry_id']} opened the trade")
                    events["filtered"].append(k)
        lim = wallet.limits(self.limits_path)
        now = self._now or int(time.time() * 1000)
        # how many different strategies signalled each coin and side this pass (the score's confirmation)
        agreeing = {g: len({c[3]["entry_id"] for c in cands}) for g, cands in groups.items()}
        chosen = []
        for k, mkt, sig, pos in pending:
            if k not in take:
                continue
            n = agreeing.get((mkt, sig.exchange, sig.symbol, sig.side.value), 1)
            quality = pos.pop("quality", None)
            if quality is not None:
                pos["score"] = score.total(quality, n)
                pos["score_parts"] = quality
            chosen.append((n, pos.get("score") or 0.0, k, mkt, sig, pos))
        # priority: the coins most strategies agree on open first, then the higher score, so when a
        # cap leaves room for only some of them, those are the ones that open (Shawn)
        chosen.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for _n, _s, k, mkt, sig, pos in chosen:
            quality = pos.get("score_parts")
            need = self._guards[mkt]["min_score"]
            if need > 0 and quality is not None and pos["score"] < need:
                self._log_signal(k, sig, "signal score too low",
                                 f"score {pos['score']:.0f}/100, minimum {need:g} (weakest: {score.weakest(quality)})")
                events["filtered"].append(k)
                continue
            # the caps were checked against the positions open before this pass: check them again
            # with the ones this pass has opened so far
            why = wallet.entry_block(self.state, self._wallet(mkt), mkt, lim[mkt], now)
            label = "engine limit"
            if not why:
                why = self._exposure_block(mkt, sig.exchange, sig.symbol, sig.side is Side.SELL, self._guards[mkt])
                label = "exposure cap"
            if why:
                self._log_signal(k, sig, label, why)
                events["filtered"].append(k)
                continue
            self.state["open"][k] = pos
            events["opened"].append(k)
            self._log_signal(k, sig, "", "; ".join(x for x in (
                f"{len(pos['agreed'])} strategies agreed" if pos.get("agreed") else "",
                f"score {pos['score']:.0f}/100" if pos.get("score") is not None else "") if x))
            log.info("paper signal %s entry~%.6g stop %.6g ai p=%s size x%.2f regime %s", k, sig.entry, sig.stop,
                     pos["ai_prob"], pos["size_mult"], (pos["regime"] or {}).get("label"))

    def _log_signal(self, key: str, sig: Signal, skipped: str, detail: str = "",
                    market: str | None = None) -> None:
        """Every signal and what became of it (``skipped`` says why; empty means opened), so the
        dashboard can say why no trade opened. ``market`` is the engine when it is known before the
        signal has its legs (a Futures long's plain key and buy side would read as Spot)."""
        market = market or ("futures" if key.endswith(FUTURES_SUFFIX) or sig.side is Side.SELL else "spot")
        counts = getattr(self, "_pass_log", None)
        if counts is not None:
            c = counts[market]
            if skipped:
                c["skipped"] += 1
                c["why"][skipped] = c["why"].get(skipped, 0) + 1
            else:
                c["opened"] += 1
        log_ = self.state.setdefault("signals", [])
        log_.append({"ts": sig.ts, "key": key, "side": sig.side.value, "entry": sig.entry, "stop": sig.stop,
                     "skipped": skipped, "detail": detail, "market": market})
        del log_[:-500]

    def _mark_signal(self, key: str, ts: int, skipped: str, detail: str = "") -> None:
        """A signal logged as opened that couldn't be filled after all: say why."""
        for s in reversed(self.state.get("signals", [])):
            if s.get("key") == key and s.get("ts") == ts:
                s.update(skipped=skipped, detail=detail)
                return

    def _note_filtered(self, key: str, sig: Signal, ai: dict) -> None:
        skipped = self.state.setdefault("filtered", [])
        skipped.append({"key": key, "signal_ts": sig.ts, "prob": ai["prob"], "version": ai["version"]})
        del skipped[:-1000]
        log.info("paper signal %s skipped by trade filter %s (p=%.3f)", key, ai["version"], ai["prob"])

    def _manage_open(self, reg: Registry, now_ms: int, events: dict) -> None:
        # the kill switch, or a person's "close all open positions", closes every paper position
        # at the last closed candle's price (fees included) and cancels those still waiting for
        # their entry candle
        asked = getattr(self.control, "close_all_requested", None)
        close_req = asked() if callable(asked) else None
        why = "closed by hand" if close_req is not None else "kill switch"
        killed = self.control is not None and bool(getattr(self.control, "killed", False))
        one = getattr(self.control, "close_requests", None)  # a person's "close this position"
        close_one = one() if callable(one) else {}
        left = 0  # positions the close request covers that couldn't be priced this pass
        breakers = self.state.get("breakers") or {}
        for key, pos in list(self.state["open"].items()):
            entry_id, ex, sym, tf = key.split("|")[:4]
            # a close request covers the positions opened before it, not those opened after
            closing = killed or (close_req is not None and pos["signal"]["ts"] <= close_req * 1000)
            reason = why
            pos_mkt = "futures" if key.endswith(FUTURES_SUFFIX) or wallet.market_of(pos) == "futures" else "spot"
            if not closing and (breakers.get(pos_mkt) or {}).get("close"):  # the weekly loss limit
                closing, reason = True, "weekly loss limit"
            hand_req = None
            if _asked_to_close(close_one.get(key), pos):
                closing, reason, hand_req = True, "closed by hand", close_one.get(key)
            if closing and not pos.get("filled"):
                del self.state["open"][key]
                events["closed"].append(key)
                log.info("paper %s cancelled (%s) before its entry", key, reason)
                continue
            if ex not in self.exchanges:  # an exchange no longer used: its prices stopped updating
                del self.state["open"][key]
                log.info("paper %s dropped: %s is no longer one of the exchanges", key, ex)
                continue
            # the engine's furthest take-profit: the target the trade runs to (and the page shows),
            # from the strategy's own target, so changing the setting moves open trades too
            full = pos.setdefault("full_target", pos["signal"].get("target"))
            max_rr = self._guards[pos_mkt]["max_rr"]
            sig = capped(_sig_from({**pos["signal"], "target": full}), max_rr)
            pos["signal"]["target"] = sig.target
            step = TIMEFRAME_MS[tf]
            df = self._frame(ex, sym, tf, sig.ts - WINDOW_BARS * step, now_ms, pos_mkt)
            if df is None:
                left += closing
                continue
            ts = df["ts"].to_numpy()
            idx = {int(t): i for i, t in enumerate(ts)}
            entry_i = idx.get(sig.ts)
            if entry_i is None:
                later = [i for i, t in enumerate(ts) if int(t) > sig.ts]
                if not later or int(ts[-1]) - sig.ts < MISSING_ENTRY_BARS * step:
                    continue  # the entry bar hasn't closed yet (or may still be filled in)
                # the entry candle never arrived: enter on the next one rather than stay open forever
                entry_i = later[0]
                log.warning("paper %s: entry candle %d missing; entering at the next one", key, sig.ts)
            try:
                if entry_id in reg.entries:
                    make_strategy(reg.entries[entry_id])  # registers its custom exit rules
                bcfg = self._bcfg(bool(pos.get("perp")), split_tp=bool(pos.get("split_tp")),
                                  maker_entry=bool(pos.get("maker_entry")), breakeven=bool(pos.get("breakeven")),
                                  max_rr=max_rr)
                t = simulate_signal(sig, df, entry_i, bcfg, {})
            except Exception:  # never close a trade on the wrong rule: keep it open and say so
                log.exception("paper %s: can't evaluate its exits; kept open", key)
                left += closing
                continue
            if t is None:
                del self.state["open"][key]
                if pos.get("maker_entry") and not pos.get("filled"):
                    self._mark_signal(key, sig.ts, "limit order not filled",
                                      "price never came back to the limit price during the entry candle")
                    log.info("paper %s skipped: its limit entry was not filled", key)
                else:
                    log.info("paper %s skipped: price opened past the stop", key)
                continue
            equity = self.state["equity"].get(entry_id, self.cfg.start_equity)
            short = sig.side is Side.SELL
            mkt = "futures" if short or pos.get("perp") else "spot"
            if not pos["filled"]:
                # sized from its workspace's paper wallet (learn.wallet): Spot or Futures
                bal = wallet.balance(self.state, self._wallet(mkt), mkt)
                lev = 1.0
                if mkt == "futures":
                    # the chosen leverage, or less when the stop (from the fill price) is too wide for
                    # it: the stop must close the trade well before liquidation
                    chosen, mmr = self._lev(sym), self._mmr(sym)
                    lev = futures.safe_leverage(t.entry, sig.stop, short, chosen, mmr)
                    if not lev:
                        del self.state["open"][key]
                        self._mark_signal(key, sig.ts, "stop too close to liquidation",
                                          f"filled at {t.entry:.6g}: the stop is past liquidation even at 1x")
                        log.info("paper %s skipped: filled at %.6g, stop too close to liquidation", key, t.entry)
                        continue
                    setting = wallet.paper_leverage(self.market_path) or futures.leverage()
                    if lev < chosen:
                        pos["lev_why"] = f"stop too wide for {chosen:g}x"
                        log.info("paper %s: %gx instead of %gx so the stop comes well before liquidation",
                                 key, lev, chosen)
                    elif chosen < setting:
                        pos["lev_why"] = f"Bybit's maximum for {sym}"
                    pos["lev_setting"], pos["mmr"] = setting, mmr
                amount = self._guards[mkt]["trade_usdt"]
                if amount > 0:  # a fixed amount per trade: the cost on Spot, the margin on Futures
                    units = amount * pos.get("size_mult", 1.0) * lev / t.entry
                else:  # sized from the risk (the engine's setting, else the strategy's), at most half the wallet
                    pct = self._guards[mkt]["risk_pct"] / 100 or sig.risk_pct
                    risk_usdt = bal * pct * pos.get("size_mult", 1.0)
                    units = risk_usdt / abs(sig.entry - sig.stop)
                    # at most half the wallet put into it: the cost on Spot, the margin on Futures
                    units = min(units, bal * self.cfg.max_position_frac * lev / t.entry)
                # all open positions together may not tie up more than the wallet
                # (a spot position ties up what it cost; a perp position only its margin)
                units = min(units, max(0.0, bal - wallet.in_use(self.state, skip=key, market=mkt)) * lev / t.entry)
                cap = self._guards[mkt]["open_risk_pct"]
                per_unit = abs(t.entry - sig.stop)
                if cap > 0 and per_unit > 0:  # all open trades together may lose at most cap% at their stops
                    room = bal * cap / 100 - guards.open_risk(self.state, mkt, skip=key)
                    if units * per_unit > room:
                        units = max(0.0, room / per_unit)
                        if units * t.entry < wallet.MIN_ORDER_USDT:
                            del self.state["open"][key]
                            self._mark_signal(key, sig.ts, "open risk cap",
                                              f"the open trades already risk {bal * cap / 100 - room:,.2f} USDT "
                                              f"at their stops, the limit is {cap:g}% of {bal:,.2f} USDT")
                            log.info("paper %s skipped: open risk cap", key)
                            continue
                skip = ("the wallet's money is all in open positions" if units <= 0 else
                        f"below Bybit's {wallet.MIN_ORDER_USDT:g} USDT minimum order"
                        if units * t.entry < wallet.MIN_ORDER_USDT else "")
                if skip:
                    del self.state["open"][key]
                    self._mark_signal(key, sig.ts, skip, f"{max(units, 0) * t.entry:.2f} USDT from a "
                                      f"{bal:,.2f} USDT wallet")
                    log.info("paper %s skipped: %s", key, skip)
                    continue
                pos.update(filled=True, entry=t.entry, units=units, entry_ts=t.entry_ts)
                if short or pos.get("perp"):
                    pos["leverage"] = lev
            if mkt == "futures":  # isolated margin: a position can't lose more than its margin
                lev = pos.get("leverage", 1.0)
                margin = pos["units"] * pos["entry"] / lev
                per_r = abs(sig.entry - sig.stop) * pos["units"]
                if per_r > 0 and t.r * per_r < -margin:
                    liq = (futures.short_liquidation_price if short else futures.long_liquidation_price)(
                        pos["entry"], lev, pos.get("mmr", futures.MAINTENANCE_MARGIN))
                    t.r, t.exit, t.exit_reason = -margin / per_r, float(liq), "liquidated"
                    log.warning("paper %s liquidated at %.6g (%gx): lost its margin of %.2f USDT",
                                key, liq, lev, margin)
            if t.exit_reason == "end" and t.exit_ts == int(df["ts"].iloc[-1]):
                if not closing:
                    pos["mark_r"] = round(t.r, 3)  # still open
                    pos["mark"] = float(df["close"].iloc[-1])  # the price it was marked at, for the page
                    continue
                t.exit_reason = reason
                # closed by hand or by the kill switch: at the price now, not the close of the
                # trade's own candle (on 4h charts that could be hours old)
                now_px = self._price_now(ex, sym, pos_mkt, now_ms, hand_req)
                risk = abs(sig.entry - sig.stop)
                if now_px and risk > 0 and now_px[1] > t.exit_ts + step:
                    move = (t.exit - now_px[0]) if short else (now_px[0] - t.exit)
                    t.r += move / risk
                    t.exit, t.exit_ts = now_px[0], now_px[1]
                    if mkt == "futures":  # isolated margin: never more than the margin lost
                        per_r = risk * pos["units"]
                        t.r = max(t.r, -(pos["units"] * pos["entry"] / pos.get("leverage", 1.0)) / per_r)
            pnl = t.r * abs(sig.entry - sig.stop) * pos["units"]
            self.state["equity"][entry_id] = equity + pnl
            row = {**_trade_dict(t), "key": key, "entry_id": entry_id, "units": pos["units"],
                   "side": sig.side.value, "perp": short or bool(pos.get("perp")),
                   "pnl_usdt": pnl, "equity_after": equity + pnl,
                   "market": mkt,
                   "wallet_after": wallet.balance(self.state, self._wallet(mkt), mkt) + pnl,
                   "ai_prob": pos.get("ai_prob"), "ai_version": pos.get("ai_version"),
                   "regime": (pos.get("regime") or {}).get("label")}
            self.state["trades"].append(row)
            del self.state["open"][key]
            events["closed"].append(key)
            log.info("paper close %s %s %+.2fR %+.2f USDT", key, t.exit_reason, t.r, pnl)
        for m, br in breakers.items():  # the weekly close is done once the engine holds nothing
            if br.get("close") and not any(
                    ("futures" if k.endswith(FUTURES_SUFFIX) or wallet.market_of(o) == "futures" else "spot") == m
                    for k, o in self.state["open"].items()):
                br["close"] = False
        if close_req is not None and not left:  # any it couldn't price yet: tried again next pass
            self.control.clear_close_all(close_req)
        done = [k for k, req in close_one.items()  # closed, or gone already
                if not _asked_to_close(req, self.state["open"].get(k, {}))]
        if done:
            self.control.clear_close(done)

    def _score(self, reg: Registry, events: dict) -> None:
        rule = self.cfg.retire
        benched = {m: {} for m in wallet.MARKETS}  # engine -> {strategy: why it stopped trading there}
        for entry in reg.entries.values():
            rows = [r for r in self.state["trades"] if r["entry_id"] == entry.id]
            if not rows and entry.status != "paper":
                continue
            m = metrics([_trade_from(r) for r in rows])
            entry.paper = {
                "trades": m["trades"], "expectancy_r": round(m["expectancy_r"], 4),
                "win_rate": round(m["win_rate"], 4), "total_r": round(m["total_r"], 3),
                "max_drawdown_r": round(m["max_drawdown_r"], 3),
                "equity_usdt": round(self.state["equity"].get(entry.id, self.cfg.start_equity), 2),
                "open": sum(1 for p in self.state["open"].values() if p["entry_id"] == entry.id),
                "by_regime": breakdown((r.get("regime"), _trade_from(r)) for r in rows),
            }
            per = {}  # each engine's own results, for its own ranking (learn.keep)
            for mkt in wallet.MARKETS:
                mine = [r for r in rows if _row_market(r) == mkt]
                if mine:
                    mm = metrics([_trade_from(r) for r in mine])
                    per[mkt] = {"trades": mm["trades"], "expectancy_r": round(mm["expectancy_r"], 4),
                                "total_r": round(mm["total_r"], 3)}
            entry.paper["markets"] = per
            if entry.status != "paper":
                continue
            # each engine judges a strategy on its own trades: losing on one engine stops it there
            # only; it retires when every engine that traded it lost
            fails, traded = {}, []
            for mkt in wallet.MARKETS:
                mine = [r for r in rows if _row_market(r) == mkt]
                if not mine:
                    continue
                traded.append(mkt)
                why = _retire_why(metrics([_trade_from(r) for r in mine]), rule)
                if why:
                    fails[mkt] = why
                    benched[mkt][entry.id] = why
            if traded and len(fails) == len(traded):
                name = {"spot": "Spot AI", "futures": "Futures AI"}
                why = "; ".join(f"{name[k]}: {v}" for k, v in fails.items()) if len(traded) > 1 else fails[traded[0]]
                entry.set_status("retired", why)
                events["retired"].append(entry.id)
                log.warning("retired %s: %s", entry.id, why)
        self.state["benched"] = benched

    def _save_registry(self, reg: Registry, retired: list[str]) -> None:
        """Write back only what this tick changed (paper results, retirements) onto the file as
        it is now: a `learn` command or the dashboard may have changed it while the tick ran."""
        fresh = Registry(self.registry_path)
        for eid, e in reg.entries.items():
            f = fresh.entries.get(eid)
            if f is None:
                continue
            f.paper = e.paper
            if eid in retired and f.status == "paper":
                f.history.append(e.history[-1])
                f.status = "retired"
        fresh.save()

    def _calendar(self, now_ms: int) -> tuple[bool, float, str]:
        """(entries allowed, size multiplier, why), e.g. why = "CPI m/m (USD) at 18:00: no new
        entries before release"."""
        if self.calendar is None:
            return True, 1.0, ""
        self.calendar.refresh()
        d = self.calendar.decide(datetime.fromtimestamp(now_ms / 1000, timezone.utc))
        return d.allow_entries, d.size_multiplier, d.reason

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1)
        os.replace(tmp, self.state_path)


def _regime(df, timeframe: str) -> dict | None:
    try:
        return classify(df, timeframe).as_dict()
    except Exception:  # the label is for reports only: never let it stop a paper trade
        log.exception("regime classifier failed on %s", timeframe)
        return None


def _row_market(row: dict) -> str:
    """The engine a closed trade belongs to."""
    return row.get("market") if row.get("market") in wallet.MARKETS else wallet.market_of(row)


def _retire_why(m: dict, rule) -> str | None:
    """Why paper results this bad stop a strategy, or None."""
    if m["max_drawdown_r"] > rule.max_drawdown:
        return f"paper drawdown {m['max_drawdown_r']:.1f}R passed {rule.max_drawdown:g}R"
    if m["trades"] >= rule.min_trades and m["expectancy_r"] < rule.min_expectancy:
        return f"paper expectancy {m['expectancy_r']:+.3f}R after {m['trades']} trades"
    return None


_NO_AI = {"take": True, "size": 1.0, "prob": None, "version": None}  # an engine that doesn't use the AI filter


def _no_teacher(sig: Signal) -> dict:
    """The teacher's answer for an engine that doesn't use it (or when no lessons are turned on)."""
    return {"take": True, "signal": sig, "situation": None, "verdict": None, "version": None}


def _sig_dict(sig: Signal) -> dict:
    d = asdict(sig)
    d["side"] = sig.side.value
    return d


def _sig_from(d: dict) -> Signal:
    return Signal(**{**d, "side": Side(d["side"])})


def _trade_dict(t: Trade) -> dict:
    d = asdict(t)
    d.pop("features", None)
    return d


def _trade_from(d: dict) -> Trade:
    fields = Trade.__dataclass_fields__
    return Trade(**{k: v for k, v in d.items() if k in fields})
