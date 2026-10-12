"""Agents A-L (master prompt section 5) as typed interfaces: see ``core`` and ``messages``."""

from .core import (AGENTS, AuditorAgent, BreakoutAgent, DerivativesAgent, ExecutionAgent, IndicatorAgent,
                   MarketDataAgent, PortfolioAgent, RegimeAgent, ResearchAgent, ReversalAgent, RiskAgent,
                   TrendAgent)
from .messages import REGIME_ENUM, SCHEMA_VERSION, Board
from .pipeline import analyse

__all__ = ["AGENTS", "AuditorAgent", "Board", "BreakoutAgent", "DerivativesAgent", "ExecutionAgent",
           "IndicatorAgent", "MarketDataAgent", "PortfolioAgent", "REGIME_ENUM", "RegimeAgent", "ResearchAgent",
           "ReversalAgent", "RiskAgent", "SCHEMA_VERSION", "TrendAgent", "analyse"]
