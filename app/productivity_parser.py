from __future__ import annotations

import re

# Duration anywhere in the text: "90 minutes", "2h", "1.5 hours", "30m", "2hrs"
_DURATION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(h(?:rs?|ours?)?|min(?:utes?)?|m(?![a-z]))\b",
    re.IGNORECASE,
)

# Category keywords — ordered most-specific first. Covers common verb forms.
_CATEGORY_PATTERNS = [
    (re.compile(r"\bpersonal\s+project\b|\bside\s+project\b", re.IGNORECASE), "Personal Project"),
    (re.compile(r"\bwork(?:ed|ing)?\b", re.IGNORECASE), "Work"),
    (re.compile(r"\bstud(?:y|ied|ying)\b|\blearn(?:ed|ing)?\b", re.IGNORECASE), "Study"),
]

# Filler words to strip from the note after removing duration + category
_FILLER_RE = re.compile(
    r"\b(for|on|of|the|a|an|in|at|doing|spent|i|today|yesterday)\b",
    re.IGNORECASE,
)

PRODUCTIVITY_CATEGORIES = ["Work", "Study", "Personal Project"]


def parse_productivity(text: str) -> dict | None:
    """Return {duration_hours, category, note} or None if not a productivity entry.

    Handles both 'duration-first' ("2h work") and 'verb-first' ("Studied 90 minutes").
    Requires both a duration unit AND a category keyword to avoid false positives.
    """
    text = text.strip()

    # Both must be present — this is the guard against false positives
    dm = _DURATION_RE.search(text)
    if not dm:
        return None

    category = None
    cat_match = None
    for pattern, cat in _CATEGORY_PATTERNS:
        cm = pattern.search(text)
        if cm:
            category = cat
            cat_match = cm
            break

    if category is None:
        return None

    quantity = float(dm.group(1).replace(",", "."))
    unit = dm.group(2).lower()
    duration_hours = quantity if unit.startswith("h") else quantity / 60

    # Build note from what remains after removing duration span and category span
    spans = sorted([dm.span(), cat_match.span()])
    remaining = text
    for start, end in reversed(spans):
        remaining = remaining[:start] + " " + remaining[end:]
    remaining = _FILLER_RE.sub(" ", remaining)
    remaining = re.sub(r"\s+", " ", remaining).strip()

    return {
        "duration_hours": round(duration_hours, 2),
        "category": category,
        "note": remaining[:200],
    }
