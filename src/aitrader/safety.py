"""The live-trading lock. Real orders can only be sent when every condition here holds."""

from __future__ import annotations

from .config import LIVE_CONFIRM_PHRASE, Mode, Settings


class LiveTradingLocked(RuntimeError):
    """Raised whenever code tries to send a real-money order while the gate is closed."""


def live_gate_problems(settings: Settings) -> list[str]:
    problems = []
    if settings.mode is not Mode.LIVE:
        problems.append(f"AITRADER_MODE is '{settings.mode.value}', not 'live'")
    if not settings.live_unlocked:
        problems.append("AITRADER_LIVE_UNLOCKED is not true")
    if settings.live_confirm != LIVE_CONFIRM_PHRASE:
        problems.append(f"AITRADER_LIVE_CONFIRM must equal '{LIVE_CONFIRM_PHRASE}'")
    return problems


def assert_live_allowed(settings: Settings) -> None:
    problems = live_gate_problems(settings)
    if problems:
        raise LiveTradingLocked("Live trading is locked: " + "; ".join(problems))
