"""The agent's learning loop: read a book, write strategies as rules, backtest them, and keep
testing the survivors on paper while the market runs.

    extract  -> rule specs drafted by Claude from the book's text, with page citations
    rules    -> the safe rule language and the strategy built from a spec
    lab      -> the backtest gate (in-sample + out-of-sample, after costs)
    paper    -> paper trading of strategies that passed, with automatic retirement
    registry -> where every strategy and its results are kept

Nothing here can place a real order or unlock live trading.
"""

from .extract import Extraction, claude_caller, extract_strategies
from .lab import Gate, evaluate
from .paper import PaperConfig, PaperTrader, RetireRule
from .registry import Entry, Registry, make_strategy
from .rules import RuleError, RuleStrategy, spec_id, validate_spec

__all__ = [
    "Entry", "Extraction", "Gate", "PaperConfig", "PaperTrader", "Registry", "RetireRule",
    "RuleError", "RuleStrategy", "claude_caller", "evaluate", "extract_strategies",
    "make_strategy", "spec_id", "validate_spec",
]
