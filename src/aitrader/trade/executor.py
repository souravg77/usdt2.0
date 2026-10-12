"""Order executor for the testnet and the live pilot (roadmap Phases 5 and 6).

The paper service (``learn paper``) decides: it finds signals, applies the AI filter and the
calendar, and manages each trade with the backtest's rules. The executor mirrors those decisions
with real orders on one exchange:

- a position the paper trader opens is bought at market, sized by the risk engine from the real
  account's equity (never more than the strategy's risk, capped at 1%);
- it is sold at market when the paper twin closes, or at once if the live price is at or below
  the stop;
- every fill is compared with its paper twin, so real slippage is measured.

Safety and failure handling:

- Orders go through the exchange adapter, which refuses real-money orders unless the live lock
  is open (``safety.assert_live_allowed``) and testnet orders unless ``AITRADER_MODE=testnet``.
  Live mode also needs ``live_guard`` (the Phase 6 go/no-go approval) to pass for the strategy.
- Each order carries a client id derived from the signal, and is written to the state file before
  it is sent, so a timeout or a restart can't buy twice: the next tick looks the id up first.
- Pause and kill come from ``trade.control``. Kill sells everything this executor bought.
- Stale paper signals, exchange errors and disconnects are reported to the event log; new entries
  stop while either is unhealthy, exits keep being tried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from ..models import TIMEFRAME_MS, OrderStatus, OrderType, Side
from ..ops import EventLog, Heartbeat
from .control import Control
from .risk import RiskEngine

log = logging.getLogger(__name__)

NETWORK_ERRORS = ("NetworkError", "ExchangeNotAvailable", "RequestTimeout", "DDoSProtection",
                  "OnMaintenance", "ConnectionError", "TimeoutError")
DISCONNECT_AFTER = 3  # consecutive failed ticks before a disconnect alert


def client_id(mode: str, key: str, signal_ts: int, leg: str) -> str:
    """Stable, exchange-safe id (letters and digits, 32 chars) for one order leg of a signal."""
    return "ait" + hashlib.sha1(f"{mode}|{key}|{signal_ts}|{leg}".encode()).hexdigest()[:29]


class Executor:
    def __init__(self, adapter, mode: str, exchange: str, paper_state_path: str,
                 state_path: str | None = None, strategies: set[str] | None = None,
                 control: Control | None = None, risk: RiskEngine | None = None,
                 events: EventLog | None = None, live_guard=None, monitor=None,
                 pilot_max_usdt: float | None = None, quote: str = "USDT",
                 max_signal_age_bars: int = 2, paper_stale_s: float = 300, clock=time.time):
        if mode not in ("testnet", "live"):
            raise ValueError("mode must be testnet or live (paper trading is `learn paper`)")
        if mode == "live" and live_guard is None:
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
        self.state = {"positions": {}, "closed": [], "orders": [], "skipped": {}, "failures": 0,
                      "disconnected": False, "paper_stale": False, "flattened": False, "checked": False}
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                self.state.update(json.load(fh))

    # ---- one pass -------------------------------------------------------------------------------

    async def tick(self) -> dict:
        out = {"opened": [], "closed": [], "skipped": []}
        ctl = self.control.state()
        try:
            if not self.state["checked"]:
                await self._check_holdings()
            await self._reconcile_pending(out)
            if ctl["killed"]:
                # flatten once per kill: a new kill (new "since") flattens again
                if not self.state["flattened"] or self.state.get("flattened_for") != ctl.get("since"):
                    self._kill_since = ctl.get("since")
                    await self._flatten(out)
            else:
                self.state["flattened"] = False
                paper = self._read_paper()
                await self._manage_exits(paper, out)
                if paper is not None and not ctl["paused"]:
                    await self._enter_new(paper, out)
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
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.events.emit("crash", f"{self.mode} executor stopped: {exc!r}", level="error")
            raise

    # ---- entries --------------------------------------------------------------------------------

    async def _enter_new(self, paper: dict, out: dict) -> None:
        now_ms = int(self.clock() * 1000)
        for key, pos in paper.get("open", {}).items():
            entry_id, ex, symbol, tf = key.split("|")
            sig = pos["signal"]
            mark = f"{key}|{sig['ts']}"
            if ex != self.exchange or key in self.state["positions"] or mark in self.state["skipped"]:
                continue
            if self.strategies and entry_id not in self.strategies:
                continue
            if now_ms - sig["ts"] > self.max_signal_age_bars * TIMEFRAME_MS[tf]:
                self._skip(mark, key, "signal is too old to enter now", out)
                continue
            if self.live_guard is not None:
                problems = self.live_guard(entry_id)
                if problems:
                    self._skip(mark, key, "live trading not approved: " + "; ".join(problems), out)
                    continue
            try:
                book = await self.adapter.get_orderbook(symbol, 5)
            except Exception as exc:
                if type(exc).__name__ != "BadSymbol":
                    raise
                # Binance's testnet lists fewer coins than the real exchange: skip, don't fail the tick
                self._skip(mark, key, f"{symbol} isn't listed on {self.exchange} {self.mode}", out)
                continue
            price = book.best_ask
            if not price or price <= sig["stop"]:
                self._skip(mark, key, f"price {price} is at or below the stop {sig['stop']}", out)
                continue
            equity, free = await self._equity()
            sizing = self.risk.size(symbol, equity, free, price, sig["stop"], sig.get("risk_pct", 0.005),
                                    pos.get("size_mult", 1.0), self._exposure())
            if not sizing.ok:
                self._skip(mark, key, "; ".join(sizing.reasons), out)
                continue
            cid = client_id(self.mode, key, sig["ts"], "entry")
            self.state["positions"][key] = {
                "key": key, "entry_id": entry_id, "symbol": symbol, "timeframe": tf,
                "signal_ts": sig["ts"], "stop": sig["stop"], "target": sig.get("target"),
                "status": "pending_entry", "entry_cid": cid, "planned_units": sizing.units,
                "risk_usdt": sizing.risk_usdt, "ai_prob": pos.get("ai_prob"), "notes": sizing.reasons}
            self.save()  # written before sending, so a crash can't lose track of the order
            order = await self.adapter.place_order(symbol, Side.BUY, sizing.units, OrderType.MARKET,
                                                   reason=f"{entry_id}: paper signal {sig['ts']}",
                                                   client_id=cid)
            self._apply_entry(key, order, out)

    def _apply_entry(self, key: str, order, out: dict) -> None:
        p = self.state["positions"][key]
        self._log_order(order, p, "entry")
        if order.filled > 0:
            base = p["symbol"].split("/")[0]
            amount = order.filled - (order.fee if order.fee_currency == base else 0.0)
            fee_quote = order.fee if order.fee_currency == self.quote else 0.0
            avg = order.average or order.price
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

    async def _manage_exits(self, paper: dict | None, out: dict) -> None:
        for key, p in list(self.state["positions"].items()):
            if p["status"] != "open":
                continue
            reason = None
            twin = (paper or {}).get("open", {}).get(key)
            if paper is not None and (twin is None or twin["signal"]["ts"] != p["signal_ts"]):
                reason = "paper twin closed"
            else:
                bid = (await self.adapter.get_orderbook(p["symbol"], 5)).best_bid
                if bid is not None and bid <= p["stop"]:
                    reason = f"live price {bid:g} at or below the stop"
            if reason:
                await self._exit(key, reason, out)

    async def _exit(self, key: str, reason: str, out: dict) -> None:
        p = self.state["positions"][key]
        base = p["symbol"].split("/")[0]
        bal = await self.adapter.balance()
        amount = min(p["amount"], bal.free.get(base, 0.0))
        if amount <= 0:
            p["status"] = "missing"
            self.events.emit("error", f"{self.mode}: no {base} left to sell for {key}", level="error", key=key)
            return
        cid = client_id(self.mode, key, p["signal_ts"], f"exit{len(p.get('exits', []))}")
        p.update(status="pending_exit", exit_cid=cid, exit_reason=reason)
        self.save()
        order = await self.adapter.place_order(p["symbol"], Side.SELL, amount, OrderType.MARKET,
                                               reason=f"{p['entry_id']}: {reason}", client_id=cid)
        self._apply_exit(key, order, out)

    def _apply_exit(self, key: str, order, out: dict) -> None:
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
        fee_quote = order.fee if order.fee_currency == self.quote else order.fee * avg
        proceeds = avg * order.filled - fee_quote
        share = order.filled / p["amount"] if p["amount"] else 1.0
        pnl = proceeds - p["cost"] * min(share, 1.0)
        remaining = p["amount"] - order.filled
        exit_ts = order.ts or int(self.clock() * 1000)
        if remaining > p["amount"] * 0.001:
            p.update(status="open", amount=remaining, cost=p["cost"] * (1 - share),
                     realized=p.get("realized", 0.0) + pnl)
            p.setdefault("exits", []).append({"filled": order.filled, "avg": avg})
            return
        pnl += p.get("realized", 0.0)
        row = {k: p.get(k) for k in ("key", "entry_id", "symbol", "timeframe", "signal_ts", "entry",
                                      "entry_ts", "stop", "risk_usdt", "ai_prob", "exit_reason")}
        row.update(exit=avg, exit_ts=exit_ts, amount=p["amount"], pnl_usdt=round(pnl, 6),
                   r=round(pnl / p["risk_usdt"], 4) if p["risk_usdt"] else 0.0, mode=self.mode)
        row.update(self._twin(p, avg))
        self.state["closed"].append(row)
        del self.state["positions"][key]
        self.state["skipped"][f"{key}|{p['signal_ts']}"] = "closed"  # never buy the same signal twice
        out["closed"].append(key)
        self.events.emit("trade_close", f"{self.mode} SELL {p['symbol']} @ {avg:g}: {pnl:+.2f} "
                         f"{self.quote} ({row['r']:+.2f}R, {p['exit_reason']})", key=key, mode=self.mode,
                         r=row["r"], paper_r=row.get("paper_r"))

    def _twin(self, p: dict, exit_px: float) -> dict:
        """Compare with the paper trade for the same signal (roadmap: measure real slippage)."""
        paper = self._read_paper(ignore_age=True) or {}
        for t in reversed(paper.get("trades", [])):
            if t.get("key") == p["key"] and t.get("signal_ts") == p["signal_ts"]:
                return {"paper_r": t["r"], "paper_entry": t["entry"], "paper_exit": t["exit"],
                        "slip_entry_bps": round((p["entry"] / t["entry"] - 1) * 1e4, 2),
                        "slip_exit_bps": round((t["exit"] / exit_px - 1) * 1e4, 2)}
        return {}

    async def _flatten(self, out: dict) -> None:
        targets = [k for k, p in self.state["positions"].items() if p["status"] in ("open", "pending_exit")]
        for key in targets:
            if self.state["positions"][key]["status"] == "open":
                await self._exit(key, "kill switch", out)
        left = [k for k, p in self.state["positions"].items() if p["status"] in ("open", "pending_exit")]
        if not left:
            self.state["flattened"] = True
            self.state["flattened_for"] = getattr(self, "_kill_since", None)
            self.control.mark_flattened()
            self.events.emit("kill", f"{self.mode}: kill switch done, {len(targets)} position(s) sold",
                             level="warning")

    # ---- recovery -------------------------------------------------------------------------------

    async def _reconcile_pending(self, out: dict) -> None:
        for key, p in list(self.state["positions"].items()):
            if p["status"] == "pending_entry":
                order = await self.adapter.find_order(p["entry_cid"], p["symbol"])
                if order is None:
                    del self.state["positions"][key]  # never reached the exchange
                    log.info("entry %s was never placed; dropped", key)
                else:
                    self._apply_entry(key, order, out)
            elif p["status"] == "pending_exit":
                order = await self.adapter.find_order(p["exit_cid"], p["symbol"])
                if order is None:
                    p["status"] = "open"  # the sell never arrived; exits retry it
                else:
                    self._apply_exit(key, order, out)

    async def _check_holdings(self) -> None:
        """After a restart: every open position must still be in the account."""
        bal = await self.adapter.balance()
        for key, p in self.state["positions"].items():
            if p["status"] != "open":
                continue
            held = bal.total(p["symbol"].split("/")[0])
            if held < p["amount"] * 0.98:
                p["status"] = "missing"
                self.events.emit("error", f"{self.mode}: {key} expects {p['amount']:g} but the account "
                                 f"holds {held:g}; not managing it until checked", level="error", key=key)
        self.state["checked"] = True

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
            equity = min(equity, self.pilot_max_usdt)
            free = max(0.0, min(free, self.pilot_max_usdt - invested))
        return equity, free

    def _open(self) -> list[dict]:
        return [p for p in self.state["positions"].values() if p["status"] in ("open", "pending_entry", "pending_exit")]

    def _exposure(self) -> list[dict]:
        return [{"symbol": p["symbol"], "notional": p.get("cost", p.get("planned_units", 0) * p["stop"])}
                for p in self._open()]

    def _check_risk(self) -> None:
        closed = self.state["closed"]
        if not closed or not self.state.get("last_equity"):
            return
        why = self.risk.loss_breach(self.state["last_equity"], closed,
                                    datetime.fromtimestamp(self.clock(), timezone.utc))
        if why is None and self.monitor is not None:
            why = self.monitor(closed)
        if why and not self.control.paused:
            self.control.pause(why, by=f"risk engine ({self.mode})")
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
