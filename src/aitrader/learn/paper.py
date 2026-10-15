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
from ..backtest.engine import Trade, as_perp, cost_r, simulate_signal, too_costly, too_little_reward
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from ..market import perp_limits
from ..market.regime import breakdown, classify
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal
from ..trade import futures
from . import guards, picks, wallet
from .lab import with_context
from .registry import Registry, make_strategy

log = logging.getLogger(__name__)

DEFAULT_STATE = "data/learn/paper_state.json"
WINDOW_BARS = 1000  # history loaded per market, enough to warm up EMA200 and friends
MIN_BARS = 250
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
                 context=None, events=None, news=None, teacher=None, market_symbols=None):
        self.store, self.registry_path = store, registry_path
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
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as fh:
                self.state.update(json.load(fh))

    # ---- one pass -------------------------------------------------------------------------------

    def tick(self, now_ms: int | None = None) -> dict:
        now_ms = now_ms or int(time.time() * 1000)
        reg = Registry(self.registry_path)  # re-read: `learn` commands may have changed it
        events = {"opened": [], "closed": [], "retired": [], "filtered": []}
        self._now = now_ms
        self._guards, self._trends, self._returns = guards.load(self.guards_path), {}, {}
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
        uses = wallet.filters(self.filters_path)
        built, broken, checks = 0, 0, 0  # what this pass looked at, for the dashboard
        for entry in reg.with_status("paper"):
            benched = self.state.get("benched") or {}
            trades = {m: picks.trades_now(entry, only_winners[m], m) and entry.id not in (benched.get(m) or {})
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
            for ex in self.exchanges:
                for sym in symbols:
                    live = {m: engines[m] and trades[m] and sym in per_market[m] for m in wallet.MARKETS}
                    tfs = {m: picks.allowed(entry, strat.spec.timeframes, tf_picks[m]) for m in wallet.MARKETS}
                    for tf in [t for t in strat.spec.timeframes if any(t in tfs[m] for m in wallet.MARKETS)]:
                        on = {m: live[m] and tf in tfs[m] for m in wallet.MARKETS}
                        key = f"{entry.id}|{ex}|{sym}|{tf}"
                        # a long takes the plain key on Spot and key|futures on Futures; a short
                        # (Futures only) takes the plain key
                        slots = ([key] if on["spot"] else []) + ([key, key + FUTURES_SUFFIX] if on["futures"] else [])
                        if not allow or all(k in self.state["open"] for k in slots):
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
                        late = now_ms - newest_close
                        if late > max_data_age(tf):  # the candle arrived late: its prices are old now
                            self._log_signal(key, sig, "stale prices",
                                             f"the candle closed {late / 60_000:.0f} min ago")
                            events["filtered"].append(key)
                            continue
                        legs = self._legs(key, sig, sym, on, events)
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
                        for k, perp in legs:
                            leg_mkt = "futures" if perp else "spot"
                            leg_ai = ai if (k, perp) in ai_legs else _NO_AI
                            leg_tch = tch if (k, perp) in tch_legs else _no_teacher(sig)
                            leg_sig = leg_tch["signal"]
                            self.state["open"][k] = {"entry_id": entry.id, "signal": _sig_dict(leg_sig),
                                                     "situation": leg_tch["situation"], "teacher": leg_tch["version"],
                                                     "size_mult": size_mult * ctx_mult * leg_ai["size"],
                                                     "filled": False,
                                                     "ai_prob": leg_ai["prob"], "ai_version": leg_ai["version"],
                                                     "regime": regimes[market], "perp": perp,
                                                     "split_tp": self._guards[leg_mkt]["split_tp"]}
                            events["opened"].append(k)
                            self._log_signal(k, leg_sig, "")
                            log.info("paper signal %s entry~%.6g stop %.6g ai p=%s size x%.2f regime %s", k,
                                     leg_sig.entry, leg_sig.stop, leg_ai["prob"], leg_ai["size"],
                                     (regimes[market] or {}).get("label"))
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

    def _symbols(self, market: str | None = None) -> list[str]:
        src = self.symbols if market is None else (lambda: self.market_symbols(market))
        if not callable(src):
            return list(src)
        try:
            return list(src())
        except Exception:  # a broken ranking file must not stop paper trading
            log.exception("can't read the pair list; skipping new entries this tick")
            return []

    def _frame(self, ex, sym, tf, start, now_ms):
        step = TIMEFRAME_MS[tf]
        candles = [c for c in self.store.load(ex, sym, tf, start) if c.ts + step <= now_ms]
        return candles_to_frame(candles) if candles else None

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
            g = self._guards[mkt]
            why = self._exposure_block(mkt, key.split("|")[1], sym, short, g)
            if why:
                self._log_signal(k, sig, "exposure cap", why)
                events["filtered"].append(k)
                continue
            if perp:
                lev = self._lev(sym)
                ok = futures.short_stop_ok if short else futures.long_stop_ok
                if not ok(sig.entry, sig.stop, lev):
                    self._log_signal(k, sig, "stop too close to liquidation")
                    events["filtered"].append(k)
                    log.info("paper %s %s skipped: stop %.6g too close to liquidation at %gx",
                             "short" if short else "perp long", k, sig.stop, lev)
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
                bad = guards.against(sig.side is Side.SELL, self._higher_trends(ex, sym, tf))
                if bad:
                    self._log_signal(k, sig, "against the higher-timeframe trend",
                                     f"the {bad} trend points {'up' if sig.side is Side.SELL else 'down'}")
                    events["filtered"].append(k)
                    continue
            legs.append((k, perp))
        return legs

    def _bcfg(self, perp: bool, min_rr: float = 0.0, split_tp: bool = False) -> BacktestConfig:
        base = as_perp(self.cfg.backtest) if perp else self.cfg.backtest
        return replace(base, min_rr=min_rr, split_tp=split_tp) if (min_rr or split_tp) else base

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
        mine_r = self._hourly_returns(ex, sym)
        if mine_r is None:
            return ""
        close = [o for o in sorted(others) if (r := self._hourly_returns(ex, o)) is not None
                 and guards.correlation(mine_r, r) >= g["corr_min"]]
        if len(close) >= int(g["max_correlated"]):
            return (f"{name} already holds {len(close)} {side} positions that move with {sym} "
                    f"(correlation {g['corr_min']:g} or more over the last week: {', '.join(close[:5])})")
        return ""

    def _hourly_returns(self, ex: str, sym: str):
        """Hourly returns of the last week, indexed by time (read once per pass), or None."""
        if (ex, sym) not in self._returns:
            now = self._now or int(time.time() * 1000)
            df = self._frame(ex, sym, "1h", now - (guards.CORR_BARS + 1) * TIMEFRAME_MS["1h"], now)
            self._returns[(ex, sym)] = (None if df is None or len(df) < 48 else
                                        df.set_index("ts")["close"].pct_change().dropna())
        return self._returns[(ex, sym)]

    def _higher_trends(self, ex: str, sym: str, tf: str) -> dict:
        """{higher timeframe: "up" / "down" / "flat"} for a signal on ``tf`` (read once per pass)."""
        out = {}
        now = self._now or int(time.time() * 1000)
        for htf in guards.HIGHER.get(tf, ()):
            if (ex, sym, htf) not in self._trends:
                df = self._frame(ex, sym, htf, now - guards.TREND_BARS * TIMEFRAME_MS[htf], now)
                self._trends[(ex, sym, htf)] = guards.trend_of(df)
            out[htf] = self._trends[(ex, sym, htf)]
        return out

    def _lev(self, symbol: str = "") -> float:
        """The Trading page's paper leverage when chosen (up to Bybit's maximum for ``symbol``),
        else AITRADER_LEVERAGE (1x-3x)."""
        chosen = wallet.paper_leverage(self.market_path)
        if not chosen:
            return futures.leverage()
        return futures.clamp_paper_leverage(chosen, perp_limits.max_leverage(symbol, self.perp_limits_path))

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
            if _asked_to_close(close_one.get(key), pos):
                closing, reason = True, "closed by hand"
            if closing and not pos.get("filled"):
                del self.state["open"][key]
                events["closed"].append(key)
                log.info("paper %s cancelled (%s) before its entry", key, reason)
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
                bcfg = self._bcfg(bool(pos.get("perp")), split_tp=bool(pos.get("split_tp")))
                t = simulate_signal(sig, df, entry_i, bcfg, {})
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
            mkt = "futures" if short or pos.get("perp") else "spot"
            if not pos["filled"]:
                # sized from its workspace's paper wallet (learn.wallet): Spot or Futures
                bal = wallet.balance(self.state, self._wallet(mkt), mkt)
                lev = self._lev(sym) if short or pos.get("perp") else 1.0
                risk_usdt = bal * sig.risk_pct * pos.get("size_mult", 1.0)
                units = risk_usdt / abs(sig.entry - sig.stop)
                units = min(units, bal * self.cfg.max_position_frac / t.entry)
                # all open positions together may not tie up more than the wallet
                # (a spot position ties up what it cost; a perp position only its margin)
                units = min(units, max(0.0, bal - wallet.in_use(self.state, skip=key, market=mkt)) * lev / t.entry)
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
            if t.exit_reason == "end" and t.exit_ts == int(df["ts"].iloc[-1]):
                if not closing:
                    pos["mark_r"] = round(t.r, 3)  # still open
                    pos["mark"] = float(df["close"].iloc[-1])  # the price it was marked at, for the page
                    continue
                t.exit_reason = reason
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
