"""Order executor for the live pilot (roadmap Phase 6), on the real Bybit account.

The paper service (``learn paper``) decides: it finds signals, applies the AI filter and the
calendar, and manages each trade with the backtest's rules. The executor mirrors those decisions
with real orders on one exchange:

- a position the paper trader opens is bought at market, sized by the risk engine from the real
  account's equity (never more than the strategy's risk, capped at 1%);
- it is sold at market when the paper twin closes, or at once if the live price is at or below
  the stop;
- a stop-loss order also rests on the exchange for every open position (when the adapter
  supports it), so the position is protected while this server is down. It is cancelled before
  the executor sells, and a stop that fired is booked as the exit;
- every fill is compared with its paper twin, so real slippage is measured.

Short signals (the short twins) are sold on the coin's USDT perpetual instead (``trade.futures``):
isolated margin at ``AITRADER_LEVERAGE``, sized by the same risk engine from the stop distance
(only the margin comes out of free cash), a reduce-only stop resting on Bybit above the entry,
bought back with a reduce-only order. The account must already be in Isolated Margin mode.

Safety and failure handling:

- Orders go through the exchange adapter, which refuses every order unless the live lock is
  open (``safety.assert_live_allowed``). The executor also needs ``live_guard`` (the Phase 6
  go/no-go approval) to pass for the strategy.
- Each order carries a client id derived from the signal, and is written to the state file before
  it is sent, so a timeout or a restart can't buy twice: the next tick looks the id up first.
- Pause and kill come from ``trade.control``. Kill sells everything this executor bought.
- Stale paper signals, exchange errors and disconnects are reported to the event log; new entries
  stop while either is unhealthy, exits keep being tried.
- With a live price stream (``prices``, the Bybit WebSocket), stops are checked every second
  between ticks on streamed prices, and a coin whose stream has gone quiet gets no new entry.
- Safety halt (``Control.halt``): new entries stop, entry orders still waiting are cancelled and
  an alert goes out, until a person resumes, when
  (a) an open position has no confirmed stop resting on the exchange ``STOP_GRACE_S`` after it
      filled,
  (b) the account holds less than the ledger says (checked after a restart and every
      ``RECHECK_S``), or
  (c) the price stream for a coin with an open position has been quiet for ``feed_halt_s()``, or
  (d) ``HALT_AFTER_FAILURES`` ticks in a row failed, or
  (e) a short is within ``LIQ_HALT_DISTANCE`` of Bybit's own liquidation price, or its stop is
      no longer at most halfway from entry to that price (checked every ``LIQ_CHECK_S`` on the
      mark price, the price Bybit liquidates on).
  Resting stops and exits are left alone: the halt only stops new risk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from ..data import derivs
from ..models import TIMEFRAME_MS, OrderStatus, OrderType, Side
from ..ops import EventLog, Heartbeat
from . import futures
from .control import Control
from .risk import RiskEngine

log = logging.getLogger(__name__)

NETWORK_ERRORS = ("NetworkError", "ExchangeNotAvailable", "RequestTimeout", "DDoSProtection",
                  "OnMaintenance", "ConnectionError", "TimeoutError")
DISCONNECT_AFTER = 3  # consecutive failed ticks before a disconnect alert
STOP_GRACE_S = 120  # an open position must have a confirmed resting stop by then
RECHECK_S = 300  # how often the account is compared with the ledger
HALT_AFTER_FAILURES = 5  # consecutive failed ticks that halt new entries
LIQ_CHECK_S = 60  # how often open shorts are compared with Bybit's liquidation price
LIQ_HALT_DISTANCE = 0.03  # a short this close (3% of the mark price) to liquidation halts


def feed_halt_s() -> float:
    """How long the price stream may be quiet on a coin with an open position (AITRADER_FEED_HALT_S)."""
    try:
        return max(30.0, float(os.environ.get("AITRADER_FEED_HALT_S", "120")))
    except ValueError:
        return 120.0


def client_id(mode: str, key: str, signal_ts: int, leg: str) -> str:
    """Stable, exchange-safe id (letters and digits, 32 chars) for one order leg of a signal."""
    return "ait" + hashlib.sha1(f"{mode}|{key}|{signal_ts}|{leg}".encode()).hexdigest()[:29]


class Executor:
    def __init__(self, adapter, mode: str, exchange: str, paper_state_path: str,
                 state_path: str | None = None, strategies: set[str] | None = None,
                 control: Control | None = None, risk: RiskEngine | None = None,
                 events: EventLog | None = None, live_guard=None, monitor=None,
                 pilot_max_usdt: float | None = None, quote: str = "USDT",
                 max_signal_age_bars: int = 2, paper_stale_s: float = 300, clock=time.time,
                 prices=None, price_max_age: float = 10.0):
        if mode != "live":
            raise ValueError("mode must be live (paper trading is `learn paper`)")
        if live_guard is None:
            raise ValueError("live mode needs the go/no-go guard")
        self.adapter, self.mode, self.exchange = adapter, mode, exchange
        self.paper_state_path = paper_state_path
        self.state_path = state_path or f"data/trade/{mode}_state.json"
        self.strategies = strategies
        self.control = control or Control()
        self.risk = risk or RiskEngine()
        self.events = events or EventLog(service=f"executor-{mode}")
        self.live_guard, self.monitor = live_guard, monitor
        self.pilot_max_usdt, self.quote = pilot_max_usdt, quote
        self.max_signal_age_bars, self.paper_stale_s, self.clock = max_signal_age_bars, paper_stale_s, clock
        self.heartbeat = Heartbeat(self.events)
        self.prices, self.price_max_age = prices, price_max_age
        self.state = {"positions": {}, "closed": [], "orders": [], "skipped": {}, "failures": 0,
                      "disconnected": False, "paper_stale": False, "flattened": False, "checked": False}
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                self.state.update(json.load(fh))
        # positions opened on an exchange the agent no longer uses can't be managed here
        for key in [k for k in self.state["positions"] if k.split("|")[1] != exchange]:
            self.state.setdefault("other_exchange", {})[key] = self.state["positions"].pop(key)
            self.events.emit("error", f"{mode}: {key} was opened on another exchange; no longer managed "
                             "here (check that account yourself)", level="warning", key=key)

    # ---- one pass -------------------------------------------------------------------------------

    async def tick(self) -> dict:
        out = {"opened": [], "closed": [], "skipped": []}
        ctl = self.control.state()
        try:
            if not self.state["checked"] or self.clock() - self.state.get("holdings_at", 0) >= RECHECK_S:
                await self._check_holdings(out)
            await self._reconcile_pending(out)
            if ctl["killed"]:
                # flatten once per kill: a new kill (new "since") flattens again
                if not self.state["flattened"] or self.state.get("flattened_for") != ctl.get("since"):
                    self._kill_since = ctl.get("since")
                    await self._flatten(out)
            else:
                self.state["flattened"] = False
                paper = self._read_paper()
                exit_error = await self._manage_exits(paper, out)
                if paper is not None and not ctl["paused"] and exit_error is None:
                    await self._enter_new(paper, out)  # no new risk while a position can't be checked
                await self._arm_stops(out)
                await self._safety_checks(out)
                if exit_error is not None:
                    raise exit_error  # counted like any failed pass: repeated, it halts trading
            await self.record_equity()
            self._check_risk()
            self._healthy()
        except Exception as exc:
            self._failed(exc)
        self.heartbeat.beat(mode=self.mode, open=len(self._open()))
        self.save()
        return out

    async def run(self, interval: float = 15.0, stop_after: int | None = None) -> None:
        self.events.emit("start", f"{self.mode} executor started on {self.exchange}")
        n = 0
        try:
            while stop_after is None or n < stop_after:
                await self.tick()
                n += 1
                if stop_after is None or n < stop_after:
                    await self._wait(interval)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.events.emit("crash", f"{self.mode} executor stopped: {exc!r}", level="error")
            raise

    async def _wait(self, interval: float) -> None:
        """Sleep until the next tick, waking early when a streamed price reaches a stop."""
        if self.prices is None:
            await asyncio.sleep(interval)
            return
        end = time.monotonic() + interval
        while time.monotonic() < end and not self._stop_hit():
            await asyncio.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def _stop_hit(self) -> bool:
        for p in self._open():
            if p["status"] == "open" and not self.prices.stale(p["symbol"], self.price_max_age):
                if _short(p):  # the spot stream stands in for the perpetual's price
                    ask = self.prices.best_ask(p["symbol"])
                    if ask is not None and ask >= p["stop"]:
                        return True
                    continue
                bid = self.prices.best_bid(p["symbol"])
                if bid is not None and bid <= p["stop"]:
                    return True
        return False

    def _price_stale(self, symbol: str) -> bool:
        """The stream covers ``symbol`` but has said nothing for a while: don't trust prices."""
        return (self.prices is not None and symbol in self.prices.symbols
                and self.prices.stale(symbol, 3 * self.price_max_age))

    async def _bid(self, symbol: str) -> float | None:
        if self.prices is not None and not self.prices.stale(symbol, self.price_max_age):
            bid = self.prices.best_bid(symbol)
            if bid is not None:
                return bid
        return (await self.adapter.get_orderbook(symbol, 5)).best_bid

    async def _ask(self, p: dict) -> float | None:
        if self.prices is not None and not self.prices.stale(p["symbol"], self.price_max_age):
            ask = self.prices.best_ask(p["symbol"])
            if ask is not None:
                return ask
        return (await self.adapter.get_orderbook(_market(p), 5)).best_ask

    # ---- entries --------------------------------------------------------------------------------

    async def _enter_new(self, paper: dict, out: dict) -> None:
        now_ms = int(self.clock() * 1000)
        for key, pos in paper.get("open", {}).items():
            parts = key.split("|")
            if len(parts) != 4:  # a Futures AI paper long (key|futures): live longs are spot buys only
                continue
            entry_id, ex, symbol, tf = parts
            sig = pos["signal"]
            mark = f"{key}|{sig['ts']}"
            if ex != self.exchange or key in self.state["positions"] or mark in self.state["skipped"]:
                continue
            if self.strategies and entry_id not in self.strategies:
                continue
            short = sig.get("side", "buy") == Side.SELL.value
            if short and not getattr(self.adapter, "supports_perps", False):
                self._skip(mark, key, "short signal: this exchange adapter can't trade futures", out)
                continue
            market = futures.perp_symbol(symbol) if short else symbol
            if now_ms - sig["ts"] > self.max_signal_age_bars * TIMEFRAME_MS[tf]:
                self._skip(mark, key, "signal is too old to enter now", out)
                continue
            if self._price_stale(symbol):
                log.info("%s: no fresh streamed price for %s; entry waits", self.mode, symbol)
                continue  # not a skip: it may still enter once prices flow again
            if self.live_guard is not None:
                problems = self.live_guard(entry_id)
                if problems:
                    self._skip(mark, key, "live trading not approved: " + "; ".join(problems), out)
                    continue
            try:
                book = await self.adapter.get_orderbook(market, 5)
            except Exception as exc:
                if type(exc).__name__ != "BadSymbol":
                    raise
                # a pair may not be listed on this exchange: skip, don't fail the tick
                self._skip(mark, key, f"{market} isn't listed on {self.exchange} {self.mode}", out)
                continue
            lev = futures.leverage() if short else 1.0
            if short:
                price = book.best_bid
                if not price or price >= sig["stop"]:
                    self._skip(mark, key, f"price {price} is at or above the stop {sig['stop']}", out)
                    continue
                if not futures.short_stop_ok(price, sig["stop"], lev):
                    self._skip(mark, key, f"stop {sig['stop']} is too close to the liquidation price at {lev:g}x", out)
                    continue
                try:
                    await self.adapter.prepare_perp(market, lev)
                except Exception as exc:
                    if type(exc).__name__ != "PerpNotReady":
                        raise
                    self._skip(mark, key, str(exc), out)
                    continue
            else:
                price = book.best_ask
                if not price or price <= sig["stop"]:
                    self._skip(mark, key, f"price {price} is at or below the stop {sig['stop']}", out)
                    continue
            equity, free = await self._equity()
            sizing = self.risk.size(symbol, equity, free, price, sig["stop"], sig.get("risk_pct", 0.005),
                                    pos.get("size_mult", 1.0), self._exposure(), short=short, leverage=lev)
            if not sizing.ok:
                self._skip(mark, key, "; ".join(sizing.reasons), out)
                continue
            cid = client_id(self.mode, key, sig["ts"], "entry")
            self.state["positions"][key] = {
                "key": key, "entry_id": entry_id, "symbol": symbol, "timeframe": tf,
                "signal_ts": sig["ts"], "stop": sig["stop"], "target": sig.get("target"),
                "status": "pending_entry", "entry_cid": cid, "planned_units": sizing.units,
                "risk_usdt": sizing.risk_usdt, "ai_prob": pos.get("ai_prob"), "notes": sizing.reasons}
            if short:
                self.state["positions"][key].update(side="short", market=market, leverage=lev)
            self.save()  # written before sending, so a crash can't lose track of the order
            order = await self.adapter.place_order(market, Side.SELL if short else Side.BUY, sizing.units,
                                                   OrderType.MARKET, reason=f"{entry_id}: paper signal {sig['ts']}",
                                                   client_id=cid)
            await self._apply_entry(key, order, out)

    async def _apply_entry(self, key: str, order, out: dict) -> None:
        p = self.state["positions"][key]
        self._log_order(order, p, "entry")
        if order.filled > 0 and _short(p):
            avg = order.average or order.price
            fee_quote = await self._fee_in_quote(order, avg)
            # a short ties up no cash beyond its margin (still in the USDT balance), so cost is 0;
            # its notional counts as exposure
            p.update(status="open", amount=order.filled, entry=avg, entry_fee=fee_quote, cost=0.0,
                     notional=avg * order.filled, entry_ts=order.ts or int(self.clock() * 1000))
            if order.filled < p["planned_units"] * 0.999:
                p["notes"].append(f"partial fill {order.filled:g} of {p['planned_units']:g}")
            out["opened"].append(key)
            self.events.emit("trade_open", f"{self.mode} SELL SHORT {order.filled:g} {_market(p)} @ {avg:g} "
                             f"({p['entry_id']}, stop {p['stop']:g}, {p.get('leverage', 1):g}x isolated)",
                             key=key, mode=self.mode)
        elif order.filled > 0:
            base = p["symbol"].split("/")[0]
            amount = order.filled - (order.fee if order.fee_currency == base else 0.0)
            avg = order.average or order.price
            fee_quote = 0.0 if order.fee_currency == base else await self._fee_in_quote(order, avg)
            p.update(status="open", amount=amount, entry=avg, entry_fee=fee_quote,
                     entry_ts=order.ts or int(self.clock() * 1000), cost=avg * order.filled + fee_quote)
            if order.filled < p["planned_units"] * 0.999:
                p["notes"].append(f"partial fill {order.filled:g} of {p['planned_units']:g}")
            out["opened"].append(key)
            self.events.emit("trade_open", f"{self.mode} BUY {amount:g} {p['symbol']} @ {avg:g} "
                             f"({p['entry_id']}, stop {p['stop']:g})", key=key, mode=self.mode)
        elif order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            del self.state["positions"][key]
            self.state["skipped"][f"{key}|{p['signal_ts']}"] = f"entry {order.status.value}"  # no retry loop
            self.events.emit("error", f"{self.mode} entry for {key} was {order.status.value}: {order.reason}",
                             level="warning", key=key)
        # still OPEN with nothing filled: stays pending, reconciled next tick

    def _skip(self, mark: str, key: str, why: str, out: dict) -> None:
        self.state["skipped"][mark] = why
        if len(self.state["skipped"]) > 2000:
            for k in list(self.state["skipped"])[:500]:
                del self.state["skipped"][k]
        out["skipped"].append(key)
        self.events.emit("signal_skipped", f"{self.mode} skipped {key}: {why}", key=key)

    # ---- exits ----------------------------------------------------------------------------------

    async def _manage_exits(self, paper: dict | None, out: dict) -> Exception | None:
        """Check every open position. A position whose check fails (an order or price lookup
        raised) doesn't stop the others being checked; the first such error is returned."""
        first = None
        for key, p in list(self.state["positions"].items()):
            if p["status"] != "open":
                continue
            try:
                if p.get("stop_cid") and await self._stop_fired(key, out):
                    continue
                reason = None
                twin = (paper or {}).get("open", {}).get(key)
                if paper is not None and (twin is None or twin["signal"]["ts"] != p["signal_ts"]):
                    reason = "paper twin closed"
                elif _short(p):
                    ask = await self._ask(p)
                    if ask is not None:
                        p["mark"] = ask  # what buying it back costs now, for the loss cap
                    if ask is not None and ask >= p["stop"]:
                        reason = f"live price {ask:g} at or above the stop"
                else:
                    bid = await self._bid(p["symbol"])
                    if bid is not None:
                        p["mark"] = bid  # what selling it fetches now, for the loss cap
                    if bid is not None and bid <= p["stop"]:
                        reason = f"live price {bid:g} at or below the stop"
            except Exception as exc:
                log.warning("%s exit check for %s failed: %r", self.mode, key, exc)
                first = first or exc
                continue
            if reason:
                await self._guarded_exit(key, reason, out)
        return first

    async def _guarded_exit(self, key: str, reason: str, out: dict) -> None:
        """One position's failed sell must not stop the others (or the kill switch) from selling."""
        try:
            await self._exit(key, reason, out)
        except Exception as exc:
            # a sell that may have reached the exchange stays pending_exit: the next tick looks it up
            self.events.emit("error", f"{self.mode} exit for {key} failed: {type(exc).__name__}: {exc}",
                             level="error", key=key)

    async def _exit(self, key: str, reason: str, out: dict) -> None:
        p = self.state["positions"][key]
        if p.get("stop_cid"):
            # the resting stop must go first: left behind, it could later sell coins of another position
            if not await self.adapter.cancel_stop(p["stop_cid"], _market(p)):
                if await self._stop_fired(key, out, force=True, after_cancel=True):
                    return
            p.pop("stop_cid", None)
            p.pop("stop_armed", None)
            self.save()
        if _short(p):
            await self._exit_short(key, p, reason, out)
            return
        base = p["symbol"].split("/")[0]
        bal = await self.adapter.balance()
        amount = min(p["amount"], bal.free.get(base, 0.0))
        if amount <= 0:
            p["status"] = "missing"
            self.events.emit("error", f"{self.mode}: no {base} left to sell for {key}", level="error", key=key)
            return
        price = (await self.adapter.get_orderbook(p["symbol"], 5)).best_bid or p["entry"]
        if await self._sellable(p["symbol"], amount, price) <= 0:
            p["status"] = "dust"  # below the exchange's minimum order: can't be sold, stop managing it
            self.events.emit("error", f"{self.mode}: {amount:g} {base} left for {key} is below the exchange's "
                             "minimum order; left in the account as dust", level="warning", key=key)
            return
        cid = client_id(self.mode, key, p["signal_ts"], f"exit{len(p.get('exits', []))}")
        p.update(status="pending_exit", exit_cid=cid, exit_reason=reason)
        self.save()
        order = await self.adapter.place_order(p["symbol"], Side.SELL, amount, OrderType.MARKET,
                                               reason=f"{p['entry_id']}: {reason}", client_id=cid)
        await self._apply_exit(key, order, out)

    async def _exit_short(self, key: str, p: dict, reason: str, out: dict) -> None:
        held = await self.adapter.perp_position(_market(p))
        amount = min(p["amount"], held)
        if amount <= 0:
            p["status"] = "missing"
            self.events.emit("error", f"{self.mode}: no short left on {_market(p)} to close for {key}",
                             level="error", key=key)
            return
        cid = client_id(self.mode, key, p["signal_ts"], f"exit{len(p.get('exits', []))}")
        p.update(status="pending_exit", exit_cid=cid, exit_reason=reason)
        self.save()
        order = await self.adapter.place_order(_market(p), Side.BUY, amount, OrderType.MARKET,
                                               reason=f"{p['entry_id']}: {reason}", client_id=cid,
                                               reduce_only=True)
        await self._apply_exit(key, order, out)

    async def _apply_exit(self, key: str, order, out: dict) -> None:
        p = self.state["positions"][key]
        self._log_order(order, p, "exit")
        if order.filled <= 0:
            if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
                p["status"] = "open"  # try again next tick
                p.setdefault("exits", []).append({"status": order.status.value})
                self.events.emit("error", f"{self.mode} exit for {key} was {order.status.value}",
                                 level="error", key=key)
            return
        avg = order.average or order.price
        fee_quote = await self._fee_in_quote(order, avg)
        share = order.filled / p["amount"] if p["amount"] else 1.0
        if _short(p):  # sold at entry, bought back at avg; funding is added when the short is done
            pnl = (p["entry"] - avg) * order.filled - fee_quote - p.get("entry_fee", 0.0) * min(share, 1.0)
        else:
            proceeds = avg * order.filled - fee_quote
            pnl = proceeds - p["cost"] * min(share, 1.0)
        remaining = p["amount"] - order.filled
        exit_ts = order.ts or int(self.clock() * 1000)
        if remaining > p["amount"] * 0.001 and await self._sellable(_market(p), remaining, avg) > 0:
            p.update(status="open", amount=remaining, cost=p["cost"] * (1 - share),
                     realized=p.get("realized", 0.0) + pnl)
            if _short(p):
                p.update(entry_fee=p.get("entry_fee", 0.0) * (1 - share), notional=p["entry"] * remaining)
            p.setdefault("exits", []).append({"filled": order.filled, "avg": avg})
            return
        # what is left (if anything) is dust the exchange won't take as an order: the trade is done
        pnl += p.get("realized", 0.0)
        funding = await self._funding(p, exit_ts) if _short(p) else 0.0
        pnl -= funding
        row = {k: p.get(k) for k in ("key", "entry_id", "symbol", "timeframe", "signal_ts", "entry",
                                      "entry_ts", "stop", "risk_usdt", "ai_prob", "exit_reason")}
        row.update(exit=avg, exit_ts=exit_ts, amount=p["amount"], pnl_usdt=round(pnl, 6),
                   r=round(pnl / p["risk_usdt"], 4) if p["risk_usdt"] else 0.0, mode=self.mode,
                   side="sell" if _short(p) else "buy")
        if _short(p):
            row["funding_usdt"] = round(funding, 6)
        row.update(self._twin(p, avg))
        self.state["closed"].append(row)
        del self.state["positions"][key]
        self.state["skipped"][f"{key}|{p['signal_ts']}"] = "closed"  # never buy the same signal twice
        out["closed"].append(key)
        self.events.emit("trade_close", f"{self.mode} {'BUY BACK' if _short(p) else 'SELL'} {_market(p)} @ {avg:g}: {pnl:+.2f} "
                         f"{self.quote} ({row['r']:+.2f}R, {p['exit_reason']})", key=key, mode=self.mode,
                         r=row["r"], paper_r=row.get("paper_r"))

    def _twin(self, p: dict, exit_px: float) -> dict:
        """Compare with the paper trade for the same signal (roadmap: measure real slippage)."""
        paper = self._read_paper(ignore_age=True) or {}
        for t in reversed(paper.get("trades", [])):
            if t.get("key") == p["key"] and t.get("signal_ts") == p["signal_ts"]:
                sign = -1 if _short(p) else 1  # positive = worse than paper either way
                return {"paper_r": t["r"], "paper_entry": t["entry"], "paper_exit": t["exit"],
                        "slip_entry_bps": round(sign * (p["entry"] / t["entry"] - 1) * 1e4, 2),
                        "slip_exit_bps": round(sign * (t["exit"] / exit_px - 1) * 1e4, 2)}
        return {}

    async def _flatten(self, out: dict) -> None:
        targets = [k for k, p in self.state["positions"].items() if p["status"] in ("open", "pending_exit")]
        for key in targets:
            if self.state["positions"][key]["status"] == "open":
                await self._guarded_exit(key, "kill switch", out)
        left = [k for k, p in self.state["positions"].items() if p["status"] in ("open", "pending_exit")]
        if not left:
            self.state["flattened"] = True
            self.state["flattened_for"] = getattr(self, "_kill_since", None)
            self.control.mark_flattened()
            self.events.emit("kill", f"{self.mode}: kill switch done, {len(targets)} position(s) sold",
                             level="warning")

    # ---- resting stops on the exchange ---------------------------------------------------------

    async def _arm_stops(self, out: dict) -> None:
        """Give every open position a stop-loss order that waits on the exchange."""
        place = getattr(self.adapter, "place_stop", None)
        if place is None:
            return
        now = self.clock()
        for key, p in list(self.state["positions"].items()):
            if p["status"] != "open" or now < p.get("stop_retry_at", 0):
                continue
            if p.get("stop_cid"):
                if not p.get("stop_armed"):
                    await self._stop_fired(key, out, force=True)  # a send that may not have arrived
                continue
            amount = await self._sellable(_market(p), p["amount"], p["stop"])
            if amount <= 0:
                continue
            n = p.get("stops", 0)
            p.update(stops=n + 1, stop_cid=client_id(self.mode, key, p["signal_ts"], f"stop{n}"), stop_armed=False)
            self.save()  # written before sending, like every order
            try:
                if _short(p):
                    order = await place(_market(p), amount, p["stop"], p["stop_cid"],
                                        reason=f"{p['entry_id']}: resting stop", side=Side.BUY)
                else:
                    order = await place(p["symbol"], amount, p["stop"], p["stop_cid"],
                                        reason=f"{p['entry_id']}: resting stop")
            except Exception as exc:
                # it may still have reached the exchange: the next pass looks it up by its id
                self.events.emit("error", f"{self.mode}: resting stop for {key} not confirmed: "
                                 f"{type(exc).__name__}: {exc}", level="warning", key=key)
                continue
            if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
                p.pop("stop_cid")
                p["stop_retry_at"] = now + 300
                self.events.emit("error", f"{self.mode}: resting stop for {key} was {order.status.value} "
                                 f"({order.reason}); the executor still watches the stop itself",
                                 level="warning", key=key)
            else:
                p["stop_armed"] = True
                self.events.emit("stop_armed", f"{self.mode}: stop for {_market(p)} rests on "
                                 f"{self.exchange} at {p['stop']:g}", key=key)

    async def _stop_fired(self, key: str, out: dict, force: bool = False, after_cancel: bool = False) -> bool:
        """Look the resting stop up (once a minute unless ``force``). True when it sold
        (the sale is booked as the exit). A stop the exchange no longer holds is dropped, so a
        new one is placed. ``after_cancel``: a cancel just failed, so an open stop is an error."""
        p = self.state["positions"][key]
        now = self.clock()
        if not force and now - p.get("stop_checked", 0) < 60:
            return False
        p["stop_checked"] = now
        order = await self.adapter.find_order(p["stop_cid"], _market(p))
        if order is not None and order.filled > 0:
            p.update(status="pending_exit", exit_cid=p.pop("stop_cid"), exit_reason=f"stop on {self.exchange}")
            p.pop("stop_armed", None)
            await self._apply_exit(key, order, out)
            return True
        if order is not None and order.status is OrderStatus.OPEN:
            if after_cancel:  # triggered but not filled yet, most likely: the next tick looks again
                raise RuntimeError(f"resting stop for {key} is still open after cancelling it")
            p["stop_armed"] = True  # also confirms a send whose answer was lost
            return False
        p.pop("stop_cid", None)  # never arrived, cancelled or expired: arm a new one
        p.pop("stop_armed", None)
        return False

    async def _sellable(self, symbol: str, amount: float, price: float) -> float:
        check = getattr(self.adapter, "sellable", None)
        return amount if check is None else await check(symbol, amount, price)

    async def _fee_in_quote(self, order, avg: float) -> float:
        """The order's fee in the quote currency (USDT), whatever coin it was paid in."""
        cur, fee = order.fee_currency, order.fee or 0.0
        if not fee or cur in (None, self.quote):
            return fee
        if cur == order.symbol.split("/")[0]:
            return fee * avg
        try:  # e.g. paid in BNB: value it at BNB's own price
            bid = (await self.adapter.get_orderbook(f"{cur}/{self.quote}", 5)).best_bid
        except Exception:
            bid = None
        if not bid:
            self.events.emit("error", f"{self.mode}: can't price a {fee:g} {cur} fee; counted as 0", level="warning")
            return 0.0
        return fee * bid

    # ---- recovery -------------------------------------------------------------------------------

    async def _reconcile_pending(self, out: dict) -> None:
        for key, p in list(self.state["positions"].items()):
            if p["status"] == "pending_entry":
                order = await self.adapter.find_order(p["entry_cid"], _market(p))
                if order is None:
                    del self.state["positions"][key]  # never reached the exchange
                    log.info("entry %s was never placed; dropped", key)
                else:
                    await self._apply_entry(key, order, out)
            elif p["status"] == "pending_exit":
                order = await self.adapter.find_order(p["exit_cid"], _market(p))
                if order is None:
                    p["status"] = "open"  # the sell never arrived; exits retry it
                else:
                    await self._apply_exit(key, order, out)

    async def _check_holdings(self, out: dict) -> None:
        """After a restart and every ``RECHECK_S``: every open position must still be in the
        account. A shortfall halts trading."""
        missing: list[str] = []
        for key, p in list(self.state["positions"].items()):
            if p["status"] == "open" and p.get("stop_cid"):
                await self._stop_fired(key, out, force=True)  # it may have sold while the server was down
        bal = await self.adapter.balance()
        # positions on the same coin (or the same perpetual) share one balance: compare the totals
        groups: dict[tuple, list[str]] = {}
        for key, p in self.state["positions"].items():
            if p["status"] == "open":
                groups.setdefault((_short(p), _market(p)), []).append(key)
        for (short, market), keys in groups.items():
            held = await self.adapter.perp_position(market) if short else bal.total(market.split("/")[0])
            expected = sum(self.state["positions"][k]["amount"] for k in keys)
            if held < expected * 0.98:
                for key in keys:
                    self.state["positions"][key]["status"] = "missing"
                    self.events.emit("error", f"{self.mode}: {key} is in the ledger but the account holds "
                                     f"{held:g} of the {expected:g} expected on {market}; not managing it "
                                     "until checked", level="error", key=key)
                missing.append(f"{market} (ledger {expected:g}, account {held:g})")
        self.state["checked"], self.state["holdings_at"] = True, self.clock()
        if missing:
            await self._halt("the account doesn't match the ledger: " + "; ".join(missing), out)

    # ---- safety halt ----------------------------------------------------------------------------

    async def _safety_checks(self, out: dict) -> None:
        now = self.clock()
        if getattr(self.adapter, "place_stop", None) is not None:
            for key, p in self.state["positions"].items():
                if (p["status"] == "open" and not p.get("stop_armed") and not p.get("halted_stop")
                        and now - p.get("entry_ts", now * 1000) / 1000 >= STOP_GRACE_S):
                    p["halted_stop"] = True  # once per position, so a resume isn't undone at once
                    await self._halt(f"{key} has no confirmed stop resting on {self.exchange} "
                                     f"{STOP_GRACE_S}s after it filled", out)
        if self.prices is not None:
            quiet = sorted({p["symbol"] for p in self._open() if p["status"] == "open"
                            and p["symbol"] in self.prices.symbols
                            and self.prices.stale(p["symbol"], feed_halt_s())})
            if quiet and not self.state.get("feed_halted"):
                self.state["feed_halted"] = True  # once per quiet spell
                await self._halt(f"the price stream has been quiet for over {feed_halt_s():g}s on "
                                 + ", ".join(quiet), out)
            elif not quiet:
                self.state["feed_halted"] = False
        if (getattr(self.adapter, "perp_position_info", None) is not None
                and now - self.state.get("liq_at", 0) >= LIQ_CHECK_S):
            self.state["liq_at"] = now
            for key, p in list(self.state["positions"].items()):
                if p["status"] == "open" and _short(p) and not p.get("halted_liq"):
                    await self._check_liquidation(key, p, out)

    async def _check_liquidation(self, key: str, p: dict, out: dict) -> None:
        try:
            info = await self.adapter.perp_position_info(_market(p))
        except Exception as exc:  # reported; the next check tries again
            self.events.emit("error", f"{self.mode}: couldn't read the {_market(p)} position: "
                             f"{type(exc).__name__}: {exc}", level="warning", key=key)
            return
        mark, liq = info.get("mark"), info.get("liq_price")
        if not liq:
            return
        p.update(mark=mark, liq_price=liq)
        dist = derivs.liquidation_distance(mark, liq, short=True)
        why = None
        if dist is not None and dist < LIQ_HALT_DISTANCE:
            why = (f"{key} is {dist:.1%} from liquidation (mark {mark:g}, Bybit liquidates at {liq:g})")
        elif p.get("stop") and not derivs.stop_inside_liquidation(p["entry"], p["stop"], liq, short=True):
            why = (f"{key}'s stop {p['stop']:g} is more than halfway from entry {p['entry']:g} to "
                   f"Bybit's liquidation price {liq:g}")
        if why:
            p["halted_liq"] = True  # once per position
            await self._halt(why, out)

    async def _funding(self, p: dict, exit_ts: int) -> float:
        """USDT the funding settlements cost this short while it was open (Bybit settles them in
        the wallet, so the fill prices don't show them). 0 when the adapter can't tell."""
        reader = getattr(self.adapter, "funding_paid", None)
        if reader is None:
            return 0.0
        try:
            paid = await reader(_market(p), int(p.get("entry_ts") or exit_ts))
        except Exception as exc:
            self.events.emit("error", f"{self.mode}: couldn't read funding for {p.get('key')}: "
                             f"{type(exc).__name__}: {exc}", level="warning", key=p.get("key"))
            return 0.0
        return float(paid or 0.0)

    async def _halt(self, why: str, out: dict) -> None:
        """Stop new risk until a person resumes: pause entries, cancel entries still waiting,
        leave resting stops and exits alone, and alert."""
        self.control.halt(why, by=f"safety halt ({self.mode})")
        cancelled = await self._cancel_waiting_entries()
        out.setdefault("halted", []).append(why)
        self.events.emit("halt", f"{self.mode} HALTED: {why}. New entries are blocked until you press "
                         f"Resume{f'; {cancelled} waiting entry order(s) cancelled' if cancelled else ''}. "
                         "Resting stops stay in place.", level="error")

    async def _cancel_waiting_entries(self) -> int:
        n = 0
        for key, p in self.state["positions"].items():
            if p["status"] != "pending_entry":
                continue
            try:
                order = await self.adapter.find_order(p["entry_cid"], _market(p))
                if order is not None and order.status is OrderStatus.OPEN and not order.filled:
                    await self.adapter.cancel_order(order.id, _market(p))
                    n += 1
            except Exception as exc:  # the halt still stands; the next tick reconciles the order
                self.events.emit("error", f"{self.mode}: couldn't cancel the waiting entry {key}: "
                                 f"{type(exc).__name__}: {exc}", level="error", key=key)
        return n

    # ---- helpers --------------------------------------------------------------------------------

    def _read_paper(self, ignore_age: bool = False) -> dict | None:
        try:
            age = self.clock() - os.path.getmtime(self.paper_state_path)
            with open(self.paper_state_path, encoding="utf-8") as fh:
                paper = json.load(fh)
        except (OSError, ValueError):
            age, paper = float("inf"), None
        if ignore_age:
            return paper
        stale = paper is None or age > self.paper_stale_s
        if stale != self.state["paper_stale"]:
            self.state["paper_stale"] = stale
            if stale:
                self.events.emit("disconnect", f"{self.mode}: paper signals are stale; no new entries "
                                 "until the paper service is back", level="warning")
            else:
                self.events.emit("reconnect", f"{self.mode}: paper signals are flowing again")
        return None if stale else paper

    async def _equity(self) -> tuple[float, float]:
        bal = await self.adapter.balance()
        cash, free = bal.total(self.quote), bal.free.get(self.quote, 0.0)
        invested = sum(p.get("cost", 0.0) for p in self._open())
        equity = cash + invested
        if self.pilot_max_usdt is not None:  # the pilot only ever uses this much of the account
            # a short's margin stays in the USDT balance but is spoken for
            margin = sum(futures.margin(p.get("notional") or p.get("planned_units", 0) * p["stop"],
                                        p.get("leverage", 1.0)) for p in self._open() if _short(p))
            equity = min(equity, self.pilot_max_usdt)
            free = max(0.0, min(free, self.pilot_max_usdt - invested - margin))
        return equity, free

    def _open(self) -> list[dict]:
        return [p for p in self.state["positions"].values() if p["status"] in ("open", "pending_entry", "pending_exit")]

    def _exposure(self) -> list[dict]:
        return [{"symbol": p["symbol"], "notional": p.get("notional") or p.get("cost")
                 or p.get("planned_units", 0) * p["stop"]} for p in self._open()]

    def _open_loss(self) -> float:
        """What the open positions are down at their latest price (gains don't offset losses)."""
        loss = 0.0
        for p in self.state["positions"].values():
            if p["status"] != "open" or p.get("mark") is None or not p.get("amount"):
                continue
            if _short(p):
                u = (p["entry"] - p["mark"]) * p["amount"] - p.get("entry_fee", 0.0)
            else:
                u = p["mark"] * p["amount"] - p.get("cost", p["entry"] * p["amount"])
            loss += min(0.0, u)
        return loss

    def _check_risk(self) -> None:
        closed = self.state["closed"]
        open_loss = self._open_loss()
        if not (closed or open_loss) or not self.state.get("last_equity"):
            return
        why = self.risk.loss_breach(self.state["last_equity"], closed,
                                    datetime.fromtimestamp(self.clock(), timezone.utc), open_loss=open_loss)
        if why is None and self.monitor is not None:
            why = self.monitor(closed)
        if why and not self.control.paused:
            self.control.pause(why, by=f"risk engine ({self.mode})", auto=True)
            self.events.emit("risk_pause", f"{self.mode} paused: {why}", level="warning")

    def _healthy(self) -> None:
        if self.state["disconnected"]:
            self.events.emit("reconnect", f"{self.mode}: {self.exchange} reachable again")
        self.state["failures"], self.state["disconnected"] = 0, False

    def _failed(self, exc: Exception) -> None:
        name = type(exc).__name__
        self.state["failures"] += 1
        log.warning("%s executor tick failed: %r", self.mode, exc)
        if any(n in name for n in NETWORK_ERRORS) or isinstance(exc, (ConnectionError, TimeoutError)):
            if self.state["failures"] >= DISCONNECT_AFTER and not self.state["disconnected"]:
                self.state["disconnected"] = True
                self.events.emit("disconnect", f"{self.mode}: {self.exchange} unreachable "
                                 f"({self.state['failures']} tries): {exc}", level="error")
        else:
            self.events.emit("error", f"{self.mode} executor: {name}: {exc}", level="error")
        if self.state["failures"] == HALT_AFTER_FAILURES and not self.control.halted:
            why = f"{HALT_AFTER_FAILURES} executor passes in a row failed (last: {name}: {exc})"
            self.control.halt(why, by=f"safety halt ({self.mode})")
            self.events.emit("halt", f"{self.mode} HALTED: {why}. New entries are blocked until you press "
                             "Resume. Resting stops stay in place.", level="error")

    def _log_order(self, order, p: dict, leg: str) -> None:
        self.state["orders"].append({
            "ts": order.ts or int(self.clock() * 1000), "id": order.id, "leg": leg, "key": p["key"],
            "symbol": p["symbol"], "side": order.side.value, "amount": order.amount,
            "filled": order.filled, "average": order.average, "status": order.status.value,
            "fee": order.fee, "fee_currency": order.fee_currency, "reason": order.reason})
        del self.state["orders"][:-5000]

    async def record_equity(self) -> float:
        equity, _free = await self._equity()
        self.state["last_equity"] = equity
        curve = self.state.setdefault("equity_curve", [])
        now_ms = int(self.clock() * 1000)
        if not curve or now_ms - curve[-1][0] >= 15 * 60_000:  # one point per 15 minutes
            curve.append([now_ms, round(equity, 2)])
            del curve[:-3000]
        return equity

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1, default=str)
        os.replace(tmp, self.state_path)


def _short(p: dict) -> bool:
    return p.get("side") == "short"


def _market(p: dict) -> str:
    """Where the position trades: the spot pair, or the USDT perpetual for a short."""
    return p.get("market") or p["symbol"]
