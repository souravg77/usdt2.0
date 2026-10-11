"""Strategies already read out of the books Shawn supplied, shipped with the code.

Each file in ``library/`` holds one book's strategies as rule specs, written by reading the book
in full (the same format ``learn read`` produces with the Claude API). ``learn add-library``
registers them as candidates so they go through the same backtest gate and paper stage as any
other strategy. Specs whose ``needs`` lists a feature the rule language doesn't have yet are
skipped and reported.
"""

from __future__ import annotations

import json
from importlib import resources

from .rules import RuleError, validate_spec


def library_books() -> list[dict]:
    out = []
    for f in sorted(resources.files(__package__).joinpath("library").iterdir(), key=lambda p: p.name):
        if f.name.endswith(".json"):
            out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def library_specs(book_filter: str | None = None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Return (valid specs, [(name, why skipped)])."""
    good, skipped = [], []
    for book in library_books():
        title = book["book"]
        if book_filter and book_filter.lower() not in title.lower():
            continue
        for raw in book["strategies"]:
            name = raw.get("name", "?")
            if raw.get("needs"):
                skipped.append((name, "needs " + ", ".join(raw["needs"])))
                continue
            raw = {**raw, "source": {**raw.get("source", {}), "book": title}}
            try:
                good.append(validate_spec(raw))
            except RuleError as exc:
                skipped.append((name, str(exc)))
    return good, skipped
