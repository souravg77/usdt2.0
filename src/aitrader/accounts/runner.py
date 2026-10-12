"""The ``users`` service: one executor per user, each on the user's own exchange account.

Every user's executor copies the same paper decisions (the platform's strategies), sized by the
risk engine from that user's own balance. Each user has their own state, pause and kill switch
under ``data/users/<id>/``; the platform's pause and kill switch (``data/trade/control.json``)
also stop every user.

Nothing is sent to Bybit for a user until live is on: until then their page follows the paper
results. A user trades real money only when all of these hold:
the platform's live lock is open (``safety.assert_live_allowed``), the go/no-go approval passes
for the strategy, the admin allowed live for that user, the user switched to live with a pilot
budget, and their real-account key passed the no-withdrawal check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ..config import EXCHANGE, ExchangeKeys, Settings
from ..ops import EventLog
from ..safety import live_gate_problems
from ..trade import Control, Executor, RiskEngine, RiskLimits
from .live import live_stop_control
from .store import UserStore, Vault

log = logging.getLogger(__name__)


class CombinedControl:
    """A user's own switches plus the platform's (and, for the live executor, the account's live
    switch): any one of them pausing or killing applies."""

    def __init__(self, own: Control, platform: Control, extra: list[Control] | None = None):
        self.own, self.platform, self.extra = own, platform, list(extra or [])

    def state(self) -> dict:
        a, b = self.own.state(), self.platform.state()
        others = [c.state() for c in self.extra]
        s = dict(a)
        s["paused"] = bool(a["paused"] or b["paused"] or b["killed"] or any(o["paused"] for o in others))
        s["killed"] = bool(a["killed"] or b["killed"] or any(o["killed"] for o in others))
        if not a["paused"] and (b["paused"] or b["killed"]):
            s["reason"] = "platform: " + (b.get("reason") or "paused")
        if not a["killed"]:  # the kill that applies decides when flattening is due again
            for o in [b] + others:
                if o["killed"]:
                    s["since"], s["reason"] = o.get("since"), o.get("reason") or s.get("reason")
                    break
        return s

    @property
    def paused(self) -> bool:
        return self.state()["paused"]

    @property
    def killed(self) -> bool:
        return self.state()["killed"]

    def pause(self, reason: str, by: str = "") -> dict:
        return self.own.pause(reason, by)

    def kill(self, reason: str = "kill switch", by: str = "") -> dict:
        return self.own.kill(reason, by)

    def resume(self, by: str = "") -> dict:
        return self.own.resume(by)

    def mark_flattened(self) -> None:
        self.own.mark_flattened()
        for c in self.extra:
            if c.killed:
                c.mark_flattened()


def user_dir(root: str, user_id: int) -> str:
    return os.path.join(root, str(int(user_id)))


def user_control(root: str, user_id: int) -> Control:
    return Control(os.path.join(user_dir(root, user_id), "control.json"))


class UsersRunner:
    def __init__(self, store: UserStore, vault: Vault, settings: Settings,
                 paper_state: str = "data/learn/paper_state.json", root: str = "data/users",
                 platform_control: Control | None = None, events_path: str | None = None,
                 adapter_factory=None, live_guard=None, live_monitor=None, risk: RiskEngine | None = None,
                 clock=time.time):
        self.store, self.vault, self.settings = store, vault, settings
        self.paper_state, self.root = paper_state, root
        self.platform = platform_control or Control("data/trade/control.json")
        self.events_path = events_path
        self.adapter_factory = adapter_factory or _make_adapter
        self.live_guard, self.live_monitor = live_guard, live_monitor
        self.risk = risk or RiskEngine(RiskLimits.from_env())
        self.clock = clock
        self._ex: dict[int, tuple[tuple, Executor]] = {}

    def _wanted(self) -> dict[int, tuple]:
        """user id -> (mode, key fingerprint, pilot) for every user who should be running."""
        out = {}
        lock_open = not live_gate_problems(self.settings)
        for u in self.store.list():
            live_on = u.mode == "live" and u.live_allowed and u.status == "active" and lock_open
            draining = not live_on and _has_positions(os.path.join(user_dir(self.root, u.id), "live_state.json"))
            if draining:
                stop = live_stop_control(self.root, u.id)
                if not stop.killed:
                    stop.kill("live is off for this account", by="platform")
            if not (live_on or draining):
                continue  # not live: nothing to send to Bybit for this account
            info = self.store.key_info(u.id, EXCHANGE, testnet=False)
            if not info:
                continue
            out[u.id] = ("live", info[0]["added"], u.pilot_max_usdt)
        return out

    async def _executor(self, user_id: int, spec: tuple) -> Executor:
        cur = self._ex.get(user_id)
        if cur and cur[0] == spec:
            return cur[1]
        if cur:
            await _close(cur[1].adapter)
        mode, _added, pilot = spec
        keys = self.store.load_keys(self.vault, user_id, EXCHANGE, testnet=False)
        adapter = self.adapter_factory(EXCHANGE, self.settings, ExchangeKeys(*keys))
        d = user_dir(self.root, user_id)
        events = EventLog(self.events_path or os.path.join(d, "events.jsonl"), service=f"user{user_id}-{mode}")
        ex = Executor(adapter, mode, EXCHANGE, self.paper_state,
                      state_path=os.path.join(d, f"{mode}_state.json"),
                      control=CombinedControl(user_control(self.root, user_id), self.platform,
                                              [live_stop_control(self.root, user_id)]),
                      risk=self.risk, events=events, live_guard=self.live_guard,
                      monitor=self.live_monitor, pilot_max_usdt=pilot, clock=self.clock)
        self._ex[user_id] = (spec, ex)
        return ex

    async def tick(self) -> dict[int, dict]:
        wanted = self._wanted()
        for uid in [u for u in self._ex if u not in wanted]:
            await _close(self._ex.pop(uid)[1].adapter)
        results = {}
        for uid, spec in wanted.items():
            try:
                ex = await self._executor(uid, spec)
                results[uid] = await ex.tick()
            except Exception as exc:  # one user's problem never stops the others
                log.warning("user %s tick failed: %r", uid, exc)
                results[uid] = {"error": repr(exc)}
        return results

    async def run(self, interval: float = 15.0, stop_after: int | None = None) -> None:
        n = 0
        try:
            while stop_after is None or n < stop_after:
                await self.tick()
                n += 1
                if stop_after is None or n < stop_after:
                    await asyncio.sleep(interval)
        finally:
            for _spec, ex in self._ex.values():
                await _close(ex.adapter)


def _make_adapter(exchange: str, settings: Settings, keys: ExchangeKeys):
    from ..exchanges.ccxt_adapter import ADAPTERS

    return ADAPTERS[exchange](settings, keys=keys)


def _has_positions(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            positions = json.load(fh).get("positions", {})
    except (OSError, ValueError):
        return False
    return any(p.get("status") in ("open", "pending_entry", "pending_exit") for p in positions.values())


async def _close(adapter) -> None:
    try:
        await adapter.close()
    except Exception:
        pass
