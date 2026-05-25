from __future__ import annotations

import json
import logging
import re

from app import config
from app.categories import ALL_CATEGORIES, detect_category, is_impulse

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a personal finance transaction parser. Extract transaction details from natural language.

Return ONLY a JSON object with these exact fields:
- amount: number (required, always positive)
- type: "income", "expense", or "transfer"
- category: one of the valid categories
- note: short description string
- is_impulse: boolean

Valid categories:
  Expense: Food, Transport, Entertainment, Clothing, Education, Health, \
Personal Care, Subscriptions, Housing, Travel, Website Business, RA Work, GRE Prep
  Income: Income
  Transfer: To Investments, From Investments

Rules:
- Message starts with + or contains income/salary words → income, Income
- "to investments" or similar → transfer, "To Investments"
- "from investments" or similar → transfer, "From Investments"
- "impulse" keyword → is_impulse=true
- Default to expense if unclear
- Website Business: anything related to websites, domains, hosting, SaaS tools, business insurance, software, servers
- Housing: rent, utilities, electricity, water, phone bills, home insurance, repairs
- RA Work: anything related to research assistant work, lab supplies, academic research, experiments — category must be exactly "RA Work"
- GRE Prep: anything related to GRE test prep, GRE books, GRE courses, test registration fees — category must be exactly "GRE Prep"
- note must be SHORT and clean — strip any leading filler words like "spent on", "paid for", "bought", "got"

Examples:
  "14 kebab"                                   → {"amount":14,"type":"expense","category":"Food","note":"kebab","is_impulse":false}
  "spent 40 groceries"                         → {"amount":40,"type":"expense","category":"Food","note":"groceries","is_impulse":false}
  "+2400 salary"                               → {"amount":2400,"type":"income","category":"Income","note":"salary","is_impulse":false}
  "100 to investments"                         → {"amount":100,"type":"transfer","category":"To Investments","note":"to investments","is_impulse":false}
  "40 clothes impulse"                         → {"amount":40,"type":"expense","category":"Clothing","note":"clothes","is_impulse":true}
  "34.60 spent on website business insurance"  → {"amount":34.60,"type":"expense","category":"Website Business","note":"website business insurance","is_impulse":false}
  "20 domain renewal"                          → {"amount":20,"type":"expense","category":"Website Business","note":"domain renewal","is_impulse":false}
  "120 home insurance"                         → {"amount":120,"type":"expense","category":"Housing","note":"home insurance","is_impulse":false}
  "15 RA work"                                 → {"amount":15,"type":"expense","category":"RA Work","note":"RA work","is_impulse":false}
  "30 GRE prep book"                           → {"amount":30,"type":"expense","category":"GRE Prep","note":"GRE prep book","is_impulse":false}
  "50 magoosh subscription"                    → {"amount":50,"type":"expense","category":"GRE Prep","note":"magoosh subscription","is_impulse":false}

Return only valid JSON, no other text."""


async def parse_transaction(text: str) -> dict | None:
    if config.ANTHROPIC_API_KEY:
        result = await _parse_with_claude(text)
        if result:
            return result
    return _parse_with_regex(text)


async def _parse_with_claude(text: str) -> dict | None:
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = msg.content[0].text.strip()
        data = json.loads(raw)
        return _validate(data)
    except Exception as exc:
        logger.warning("Claude parse failed: %s", exc)
        return None


def _parse_with_regex(text: str) -> dict | None:
    m = re.search(r"\+?(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    amount = float(m.group(1).replace(",", "."))
    type_, category = detect_category(text)
    note = re.sub(r"\+?\d+(?:[.,]\d+)?", "", text).strip()
    note = re.sub(r"\s+", " ", note).strip() or text[:60]
    return _validate({
        "amount": amount,
        "type": type_,
        "category": category,
        "note": note,
        "is_impulse": is_impulse(text),
    })


def _validate(data: dict) -> dict | None:
    try:
        amount = abs(float(data["amount"]))
    except (KeyError, TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    type_ = data.get("type", "expense")
    if type_ not in ("income", "expense", "transfer"):
        type_ = "expense"
    category = data.get("category", "Food")
    if category not in ALL_CATEGORIES:
        category = "Food"
    note = str(data.get("note") or "")
    note = re.sub(r"^(?:spent\s+on|paid\s+for|bought|spent|got|purchased)\s+", "", note, flags=re.IGNORECASE).strip()
    return {
        "amount": round(amount, 2),
        "type": type_,
        "category": category,
        "note": note[:200],
        "is_impulse": bool(data.get("is_impulse", False)),
    }
