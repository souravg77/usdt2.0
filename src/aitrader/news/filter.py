"""News filter for new entries: the AI news brief can only make the agent more careful.

- market risk "high" (a fresh shock such as a new war or a big exchange failure): no new buys.
- market risk "elevated": new buys at half size.
- the same for one pair, from that pair's own risk.

A brief older than ``max_age_h`` hours, or no brief at all, is ignored: the rule is skipped,
never turned into a block, so a broken feed can't stop trading on its own (the calendar and
market-context filters still run).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .service import DEFAULT_DIR, load_latest

ELEVATED_SIZE = 0.5


@dataclass(frozen=True)
class NewsDecision:
    allow_entries: bool
    size_multiplier: float
    reasons: list[str] = field(default_factory=list)


class NewsFilter:
    def __init__(self, news_dir: str = DEFAULT_DIR, max_age_h: float = 12.0, cache_s: float = 60.0):
        self.news_dir, self.max_age_ms, self.cache_s = news_dir, max_age_h * 3_600_000, cache_s
        self._loaded_at, self._latest = 0.0, {}

    def _brief(self) -> tuple[dict | None, int]:
        if time.monotonic() - self._loaded_at > self.cache_s or not self._loaded_at:
            self._latest, self._loaded_at = load_latest(self.news_dir), time.monotonic()
        return self._latest.get("brief"), int(self._latest.get("generated") or 0)

    def decide(self, symbol: str, now_ms: int | None = None) -> NewsDecision:
        now_ms = now_ms or int(time.time() * 1000)
        brief, at = self._brief()
        if not brief:
            return NewsDecision(True, 1.0, ["no AI news brief yet: rule skipped"])
        if now_ms - at > self.max_age_ms:
            return NewsDecision(True, 1.0, ["news brief is stale: rule skipped"])
        allow, size, reasons = True, 1.0, []
        for scope, risk, why in (("market", brief.get("market_risk"), brief.get("market_risk_why", "")),
                                 *[(symbol, p.get("risk"), p.get("why", "")) for p in brief.get("pairs", [])
                                   if p.get("symbol") == symbol]):
            if risk == "high":
                allow = False
                reasons.append(f"news: {scope} risk high ({why}): no new buys")
            elif risk == "elevated":
                size *= ELEVATED_SIZE
                reasons.append(f"news: {scope} risk elevated ({why}): size x{ELEVATED_SIZE:g}")
        return NewsDecision(allow, size if allow else 0.0, reasons)
