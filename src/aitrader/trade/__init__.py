"""Phases 5 and 6: real orders on the testnet and, after the go/no-go gate, the live pilot.

- ``control``: pause and kill switch shared by every service
- ``risk``: position sizing and loss caps every order goes through
- ``executor``: mirrors the paper service's decisions with exchange orders
- ``golive``: the go/no-go gate, your approval, and the live guard
- ``report``: the weekly paper-vs-backtest (and live-vs-paper) report
"""

from .control import Control
from .executor import Executor, client_id
from .golive import GateConfig, GoLive
from .risk import HARD_MAX_RISK, RiskEngine, RiskLimits, Sizing

__all__ = ["Control", "Executor", "client_id", "GateConfig", "GoLive", "HARD_MAX_RISK",
           "RiskEngine", "RiskLimits", "Sizing"]
