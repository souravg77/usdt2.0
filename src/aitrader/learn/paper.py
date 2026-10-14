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

Short signals (the short twins, ``strategies.mirror``) are paper-traded as Bybit USDT perpetual
shorts: perp fees and funding in the result, isolated margin at ``AITRADER_LEVERAGE`` (only the
margin counts against the wallet), and a short whose stop is too close to the
liquidation price is skipped. The AI trade filter learned from long trades, so it doesn't judge
shorts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from ..backtest import BacktestConfig, metrics
from ..backtest.engine import Trade, cost_r, simulate_signal, too_costly
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from ..market.regime import breakdown, classify
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal
from ..trade import futures
from . import wallet
from .lab import with_context
from .registry import Registry, make_strategy

log = logging.getLogger(__name__)

DEFAULT_STATE = "data/learn/paper_state.json"
WINDOW_BARS = 1000  # history loaded per market, enough to warm up EMA200 and friends
MIN_BARS = 250
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


class PaperTrader:
    def __init__(self, store: CandleStore, registry_path: str, exchanges: list[str],
                 symbols: list[str], cfg: PaperConfig | None = None,
                 state_path: str = DEFAULT_STATE, calendar=None, ai_filter=None, control=None,
                 context=None, events=None, news=None, teacher=None):
        self.store, self.registry_path = store, registry_path
        # ``symbols`` may be a function returning the list (the pair ranking, read each tick)
        self.exchanges, self.symbols = exchanges, symbols
        self.cfg = cfg or PaperConfig()
        self.state_path, self.calendar = state_path, calendar
        self.wallet_path = wallet.wallet_path(state_path)  # the starting amount you chose
        self.ai_filter = ai_filter  # Phase 4 trade filter (aitrader.ai.LiveFilter) or None
        self.control = control  # trade.Control: pause / kill switch, or None
        self.context = context  # market-context filter (funding, open interest...), or None
        self.events = events  # ops.EventLog for alerts and the stability record, or None
        self.news = news  # news.NewsFilter (AI web-search brief): can only shrink or pause entries
        self.teacher = teacher  # learn.teacher.TeacherFilter: follows the lessons a person turned on
        self.state = {"open": {}, "trades": [], "equity": {}, "last_signal": {}}
        self._seen: dict = {}  # market -> newest candle close already evaluated (this process)
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as fh:
                self.state.update(json.load(fh))

    # ---- one pass -------------------------------------------------------------------------------

    def tick(self, now_ms: int | None = None) -> dict:
        now_ms = now_ms or int(time.time() * 1000)
        reg = Registry(self.registry_path)  # re-read: `learn` commands may have changed it
        events = {"opened": [], "closed": [], "retired": [], "filtered": []}
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
        built, broken, checks = 0, 0, 0  # what this pass looked at, for the dashboard
        for entry in reg.with_status("paper"):
            try:
                strat = make_strategy(entry)
            except Exception:  # one broken strategy must not stop the others
                log.exception("can't build %s; skipping it", entry.id)
                broken += 1
                continue
            built += 1
            for ex in self.exchanges:
                for sym in symbols:
                    for tf in strat.spec.timeframes:
                        key = f"{entry.id}|{ex}|{sym}|{tf}"
                        if key in self.state["open"] or not allow:
                            continue
                        market = (ex, sym, tf)
                        if market not in frames:
                            df = self._frame(ex, sym, tf, now_ms - WINDOW_BARS * TIMEFRAME_MS[tf], now_ms)
                            close = int(df["ts"].iloc[-1]) + TIMEFRAME_MS[tf] if df is not None and len(df) else None
                            frames[market] = (df, close, close is not None and self._seen.get(market) != close)
                        df, newest_close, fresh = frames[market]
                        # signals only appear when a candle closes: skip markets with no new candle
                        if not fresh or df is None or len(df) < MIN_BARS:
                            continue
                        checks += 1
                        try:
                            sdf = with_context(strat, df, self.store, ex, sym, tf)
                            sigs = [s for s in strat.generate(sdf, ex, sym, tf) if s.ts == newest_close]
                        except Exception:  # one strategy failing on one market must not stop the rest
                            log.exception("%s failed on %s %s %s; skipping it this candle", entry.id, ex, sym, tf)
                            continue
                        if not sigs or sigs[-1].ts <= self.state["last_signal"].get(key, 0):
                            continue
                        sig = sigs[-1]
                        self.state["last_signal"][key] = sig.ts
                        if sig.side is Side.SELL and not futures.short_stop_ok(sig.entry, sig.stop, self._lev()):
                            self._log_signal(key, sig, "stop too close to liquidation")
                            events["filtered"].append(key)
                            log.info("paper short %s skipped: stop %.6g too close to liquidation at %gx",
                                     key, sig.stop, self._lev())
                            continue
                        if too_costly(sig, self.cfg.backtest):  # fees would eat too much of the risk
                            self._log_signal(key, sig, "fees too high for the stop",
                                             f"fees and slippage {cost_r(sig, self.cfg.backtest):.2f}R of the risk, "
                                             f"limit {self.cfg.backtest.max_cost_r:.2f}R")
                            events["filtered"].append(key)
                            log.info("paper signal %s skipped: fees and slippage are %.2fR of the risk "
                                     "(limit %.2fR)", key, cost_r(sig, self.cfg.backtest), self.cfg.backtest.max_cost_r)
                            continue
                        ctx_mult = self._context(ex, sym, now_ms)
                        if ctx_mult <= 0:
                            self._log_signal(key, sig, "market context or news filter")
                            events["filtered"].append(key)
                            continue
                        ai = self._ai_check(sig, entry.id, df)
                        if not ai["take"]:
                            self._note_filtered(key, sig, ai)
                            self._log_signal(key, sig, "AI filter")
                            events["filtered"].append(key)
                            continue
                        tch = self._teacher_check(sig, entry.id, df)
                        if not tch["take"]:
                            self._log_signal(key, sig, "backtest teacher")
                            events["filtered"].append(key)
                            log.info("paper signal %s skipped: the backtest teacher %s says avoid %s",
                                     key, tch["version"], tch["situation"])
                            continue
                        sig = tch["signal"]
                        if market not in regimes:
                            regimes[market] = _regime(df, tf)
                        self.state["open"][key] = {"entry_id": entry.id, "signal": _sig_dict(sig),
                                                   "situation": tch["situation"], "teacher": tch["version"],
                                                   "size_mult": size_mult * ctx_mult * ai["size"], "filled": False,
                                                   "ai_prob": ai["prob"], "ai_version": ai["version"],
                                                   "regime": regimes[market]}
                        events["opened"].append(key)
                        self._log_signal(key, sig, "")
                        log.info("paper signal %s entry~%.6g stop %.6g ai p=%s size x%.2f regime %s", key,
                                 sig.entry, sig.stop, ai["prob"], ai["size"], (regimes[market] or {}).get("label"))
        for market, (_df, close, _fresh) in frames.items():
            self._seen[market] = close
        newest = max((c for _d, c, _f in frames.values() if c), default=None)
        self.state["last_pass"] = {"at": now_ms, "strategies": built, "broken": broken, "pairs": len(symbols),
                                   "markets": len(frames), "new_candles": sum(1 for *_x, f in frames.values() if f),
                                   "checks": checks, "newest_candle": newest}
        if checks:
            log.info("paper pass: %d strategies checked %d new candles (%d markets), %d opened, %d skipped",
                     built, checks, len(frames), len(events["opened"]), len(events["filtered"]))
        self._score(reg, events)
        self._save_registry(reg, events["retired"])
        self.save()
        self._emit(events)
        return events

    def run(self, interval: float = 60.0, stop_after: int | None = None) -> None:
        from ..ops import Heartbeat

        beat = Heartbeat(self.events) if self.events is not None else None
        if self.events is not None:
            self.events.emit("start", "paper service started")
        n = 0
        try:
            while stop_after is None or n < stop_after:
                try:
                    ev = self.tick()
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
                    time.sleep(interval)
        except BaseException as exc:
            if self.events is not None and not isinstance(exc, KeyboardInterrupt):
                self.events.emit("crash", f"paper service stopped: {exc!r}", level="error")
            raise

    # ---- internals ------------------------------------------------------------------------------

    def _symbols(self) -> list[str]:
        if not callable(self.symbols):
            return self.symbols
        try:
            return list(self.symbols())
        except Exception:  # a broken ranking file must not stop paper trading
            log.exception("can't read the pair list; skipping new entries this tick")
            return []

    def _frame(self, ex, sym, tf, start, now_ms):
        step = TIMEFRAME_MS[tf]
        candles = [c for c in self.store.load(ex, sym, tf, start) if c.ts + step <= now_ms]
        return candles_to_frame(candles) if candles else None

    def _context(self, exchange: str, symbol: str, now_ms: int) -> float:
        """Size multiplier from the market-context and news filters (0 = no new entry)."""
        mult = 1.0
        for name, f, args in (("market-context", self.context, (exchange, symbol, now_ms)),
                              ("news", self.news, (symbol, now_ms))):
            if f is None:
                continue
            try:
                d = f.decide(*args)
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
            side = "SELL (short, perp)" if sig.get("side") == Side.SELL.value else "BUY"
            self.events.emit("trade_open", f"paper {side} signal {key} entry~{sig['entry']:.6g} "
                             f"stop {sig['stop']:.6g}" + (f" (AI p={ai:.2f})" if ai is not None else "")
                             + (f" regime {regime}" if regime else ""),
                             key=key, mode="paper")
        for key in events["closed"]:
            t = next((t for t in reversed(self.state["trades"]) if t["key"] == key), None)
            if t:
                self.events.emit("trade_close", f"paper close {key} {t['exit_reason']} {t['r']:+.2f}R "
                                 f"({t['pnl_usdt']:+.2f} USDT)", key=key, mode="paper", r=t["r"])
        for sid in events["retired"]:
            self.events.emit("error", f"paper: strategy {sid} retired by its results", level="warning")

    def _lev(self) -> float:
        return futures.leverage()

    def _ai_check(self, sig: Signal, entry_id: str, df) -> dict:
        if self.ai_filter is None or sig.side is Side.SELL:
            return {"take": True, "size": 1.0, "prob": None, "version": None}
        recent = [t["r"] for t in self.state["trades"]
                  if t["entry_id"] == entry_id and t["exit_ts"] < sig.ts]
        try:
            return self.ai_filter.check(sig, entry_id, df, recent)
        except Exception:  # a broken model must not stop paper trading; trade unfiltered
            log.exception("trade filter failed on %s; taking the signal unfiltered", entry_id)
            return {"take": True, "size": 1.0, "prob": None, "version": None}

    def _teacher_check(self, sig: Signal, entry_id: str, df) -> dict:
        passthrough = {"take": True, "signal": sig, "situation": None, "verdict": None, "version": None}
        if self.teacher is None:
            return passthrough
        try:
            return self.teacher.check(sig, entry_id, df)
        except Exception:  # broken lessons must not stop paper trading; take the signal as is
            log.exception("backtest teacher failed on %s; taking the signal as is", entry_id)
            return passthrough

    def _log_signal(self, key: str, sig: Signal, skipped: str, detail: str = "") -> None:
        """Every signal and what became of it (``skipped`` says why; empty means opened), so the
        dashboard can say why no trade opened."""
        log_ = self.state.setdefault("signals", [])
        log_.append({"ts": sig.ts, "key": key, "side": sig.side.value, "entry": sig.entry, "stop": sig.stop,
                     "skipped": skipped, "detail": detail})
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
        left = 0  # positions the close request covers that couldn't be priced this pass
        for key, pos in list(self.state["open"].items()):
            entry_id, ex, sym, tf = key.split("|")
            # a close request covers the positions opened before it, not those opened after
            closing = killed or (close_req is not None and pos["signal"]["ts"] <= close_req * 1000)
            if closing and not pos.get("filled"):
                del self.state["open"][key]
                events["closed"].append(key)
                log.info("paper %s cancelled (%s) before its entry", key, why)
                continue
            if ex not in self.exchanges:  # an exchange no longer used: its prices stopped updating
                del self.state["open"][key]
                log.info("paper %s dropped: %s is no longer one of the exchanges", key, ex)
                continue
            sig = _sig_from(pos["signal"])
            step = TIMEFRAME_MS[tf]
            df = self._frame(ex, sym, tf, sig.ts - WINDOW_BARS * step, now_ms)
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
                t = simulate_signal(sig, df, entry_i, self.cfg.backtest, {})
            except Exception:  # never close a trade on the wrong rule: keep it open and say so
                log.exception("paper %s: can't evaluate its exits; kept open", key)
                left += closing
                continue
            if t is None:
                del self.state["open"][key]
                log.info("paper %s skipped: price opened past the stop", key)
                continue
            equity = self.state["equity"].get(entry_id, self.cfg.start_equity)
            short = sig.side is Side.SELL
            if not pos["filled"]:
                # sized from the one paper wallet every strategy shares (learn.wallet)
                bal = wallet.balance(self.state, wallet.load(self.wallet_path))
                lev = self._lev() if short else 1.0
                risk_usdt = bal * sig.risk_pct * pos.get("size_mult", 1.0)
                units = risk_usdt / abs(sig.entry - sig.stop)
                units = min(units, bal * self.cfg.max_position_frac / t.entry)
                # all open positions together may not tie up more than the wallet
                # (a spot position ties up what it cost; a perp short only its margin)
                units = min(units, max(0.0, bal - wallet.in_use(self.state, skip=key)) * lev / t.entry)
                why = ("the wallet's money is all in open positions" if units <= 0 else
                       f"below Bybit's {wallet.MIN_ORDER_USDT:g} USDT minimum order"
                       if units * t.entry < wallet.MIN_ORDER_USDT else "")
                if why:
                    del self.state["open"][key]
                    self._mark_signal(key, sig.ts, why, f"{max(units, 0) * t.entry:.2f} USDT from a "
                                      f"{bal:,.2f} USDT wallet")
                    log.info("paper %s skipped: %s", key, why)
                    continue
                pos.update(filled=True, entry=t.entry, units=units, entry_ts=t.entry_ts)
                if short:
                    pos["leverage"] = lev
            if t.exit_reason == "end" and t.exit_ts == int(df["ts"].iloc[-1]):
                if not closing:
                    pos["mark_r"] = round(t.r, 3)  # still open
                    pos["mark"] = float(df["close"].iloc[-1])  # the price it was marked at, for the page
                    continue
                t.exit_reason = why
            pnl = t.r * abs(sig.entry - sig.stop) * pos["units"]
            self.state["equity"][entry_id] = equity + pnl
            row = {**_trade_dict(t), "key": key, "entry_id": entry_id, "units": pos["units"],
                   "side": sig.side.value,
                   "pnl_usdt": pnl, "equity_after": equity + pnl,
                   "wallet_after": wallet.balance(self.state, wallet.load(self.wallet_path)) + pnl,
                   "ai_prob": pos.get("ai_prob"), "ai_version": pos.get("ai_version"),
                   "regime": (pos.get("regime") or {}).get("label")}
            self.state["trades"].append(row)
            del self.state["open"][key]
            events["closed"].append(key)
            log.info("paper close %s %s %+.2fR %+.2f USDT", key, t.exit_reason, t.r, pnl)
        if close_req is not None and not left:  # any it couldn't price yet: tried again next pass
            self.control.clear_close_all(close_req)

    def _score(self, reg: Registry, events: dict) -> None:
        rule = self.cfg.retire
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
            if entry.status != "paper":
                continue
            why = None
            if m["max_drawdown_r"] > rule.max_drawdown:
                why = f"paper drawdown {m['max_drawdown_r']:.1f}R passed {rule.max_drawdown:g}R"
            elif m["trades"] >= rule.min_trades and m["expectancy_r"] < rule.min_expectancy:
                why = f"paper expectancy {m['expectancy_r']:+.3f}R after {m['trades']} trades"
            if why:
                entry.set_status("retired", why)
                events["retired"].append(entry.id)
                log.warning("retired %s: %s", entry.id, why)

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
