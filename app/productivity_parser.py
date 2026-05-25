from __future__ import annotations

import re

# Matches a leading duration: "2h", "45min", "1.5h", "30m", "2hrs", "2 hours"
_DURATION_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*(h(?:rs?|ours?)?|min(?:utes?)?|m(?![a-z]))\b",
    re.IGNORECASE,
)

# Ordered most-specific first so "personal project" wins over "personal"
_CATEGORY_PATTERNS = [
    (re.compile(r"\bpersonal\s+project\b|\bside\s+project\b", re.IGNORECASE), "Personal Project"),
    (re.compile(r"\bwork(?:ing)?\b", re.IGNORECASE), "Work"),
    (re.compile(r"\bstudy(?:ing)?\b|\blearn(?:ing)?\b", re.IGNORECASE), "Study"),
]

PRODUCTIVITY_CATEGORIES = ["Work", "Study", "Personal Project"]


def parse_productivity(text: str) -> dict | None:
    """Return {duration_hours, category, note} or None if not a productivity entry."""
    text = text.strip()
    m = _DURATION_RE.match(text)
    if not m:
        return None

    quantity = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    duration_hours = quantity if unit.startswith("h") else quantity / 60

    remainder = text[m.end():].strip()
    if not remainder:
        return None

    category = None
    note_text = remainder
    for pattern, cat in _CATEGORY_PATTERNS:
        cm = pattern.search(remainder)
        if cm:
            category = cat
            note_text = (remainder[:cm.start()] + remainder[cm.end():]).strip()
            note_text = re.sub(r"\s+", " ", note_text).strip()
            break

    if category is None:
        return None

    return {
        "duration_hours": round(duration_hours, 2),
        "category": category,
        "note": note_text[:200],
    }
