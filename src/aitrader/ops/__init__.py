"""Operations: the shared event log every service writes to (trades, errors, disconnects,
heartbeats). The dashboard reads it, the Telegram service sends it, and the Phase 6 gate uses
it to judge stability."""

from .events import EventLog, Heartbeat

__all__ = ["EventLog", "Heartbeat"]
