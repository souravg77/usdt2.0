"""The per-account live trading switch.

Turning it **on** needs every one of these, checked here:

- the account is active and the admin allowed live for it;
- the platform's live lock in ``.env`` is open (only the server owner can do that);
- the go/no-go gate passed and at least one approved strategy still matches its approval;
- a verified real-account key (no withdrawal rights) is connected;
- the user re-entered their password, typed the confirmation phrase and set a budget cap.

Turning it **off** is instant and acts as a kill switch for real money: the account goes back to
paper at once, and the ``users`` service sells every position it bought on the real
account before stopping the live executor.

Every switch, on or off, by the user or the admin, is written to the audit log and the event
log (Telegram sends it).
"""

from __future__ import annotations

import os

from ..config import LIVE_CONFIRM_PHRASE
from ..ops import EventLog
from ..safety import live_gate_problems
from ..trade import Control
from .store import AccountError, UserStore

MAX_BUDGET_USDT = 1_000_000.0


def live_stop_control(root: str, user_id: int) -> Control:
    """Set (killed) while an account's real-money positions must be sold and live stays off."""
    return Control(os.path.join(root, str(int(user_id)), "live_stop.json"))


def gate_problems(golive) -> list[str]:
    """Why the go/no-go approval doesn't currently allow any live strategy (empty = it does)."""
    if golive is None:
        return ["the go/no-go gate isn't available"]
    approval = golive._load("approval.json")
    if not approval.get("strategies"):
        return ["the go/no-go gate hasn't been passed and approved yet"]
    problems = []
    for sid in approval["strategies"]:
        p = golive.guard(sid)
        if not p:
            return []
        problems += p
    return problems or ["no approved strategy is still valid"]


def turn_on(store: UserStore, settings, golive, root: str, user_id: int, password: str, phrase: str,
            budget_usdt: float, actor: str, events: EventLog | None = None) -> None:
    user = store.get(user_id)
    if user is None or user.status != "active":
        raise AccountError("this account isn't active")
    if store.authenticate(user.email, password or "") is None:
        store.log(user_id, actor, "live_on_refused", "wrong password")
        raise AccountError("the password is wrong")
    if (phrase or "").strip() != LIVE_CONFIRM_PHRASE:
        raise AccountError(f"type the phrase exactly: {LIVE_CONFIRM_PHRASE}")
    if not budget_usdt or budget_usdt <= 0 or budget_usdt > MAX_BUDGET_USDT:
        raise AccountError("set a budget cap: the most USDT the agent may use")
    if live_gate_problems(settings):
        raise AccountError("the platform's live lock is closed (the server owner opens it in .env)")
    problems = gate_problems(golive)
    if problems:
        raise AccountError("the go/no-go gate doesn't allow live trading: " + "; ".join(problems))
    store.set_mode(user_id, "live", budget_usdt)  # checks admin permission and the real-account key
    live_stop_control(root, user_id).resume(by=actor)
    store.log(user_id, actor, "live_on", f"budget {budget_usdt:,.2f} USDT")
    (events or EventLog(service="users")).emit(
        "live_switch", f"{user.email}: live trading ON (budget {budget_usdt:,.2f} USDT) by {actor}",
        level="warning", user_id=user_id)


def turn_off(store: UserStore, root: str, user_id: int, actor: str, reason: str = "switched off",
             events: EventLog | None = None) -> None:
    """Instant: back to paper, and the live executor sells what it bought."""
    user = store.get(user_id)
    if user is None:
        raise AccountError("no such account")
    was_live = user.mode == "live"
    with store.conn:
        store.conn.execute("UPDATE users SET mode='testnet' WHERE id=?", (user_id,))
    live_stop_control(root, user_id).kill(f"live {reason}", by=actor)
    store.log(user_id, actor, "live_off", reason)
    if was_live:
        (events or EventLog(service="users")).emit(
            "live_switch", f"{user.email}: live trading OFF by {actor} ({reason}); selling live positions",
            level="warning", user_id=user_id)
