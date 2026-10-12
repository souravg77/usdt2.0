"""News and web search for the agent: free feeds, Claude's web search brief, and the news filter."""

from .brief import clean, write_brief
from .filter import NewsDecision, NewsFilter
from .service import load_latest, update

__all__ = ["NewsDecision", "NewsFilter", "clean", "load_latest", "update", "write_brief"]
