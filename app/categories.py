EXPENSE_CATEGORIES = [
    "Food", "Transport", "Entertainment", "Clothing", "Education",
    "Health", "Personal Care", "Subscriptions", "Housing", "Travel",
    "Website Business", "RA Work", "GRE Prep", "Gifts",
]
INCOME_CATEGORIES = ["Income"]
TRANSFER_CATEGORIES = ["To Investments", "From Investments"]
ALL_CATEGORIES = EXPENSE_CATEGORIES + INCOME_CATEGORIES + TRANSFER_CATEGORIES

_KEYWORDS: dict[str, list[str]] = {
    "Food": [
        "food", "eat", "lunch", "dinner", "breakfast", "restaurant", "cafe", "coffee",
        "groceries", "grocery", "supermarket", "snack", "pizza", "burger", "kebab",
        "sushi", "takeaway", "takeout", "fastfood", "bakery", "market",
    ],
    "Transport": [
        "transport", "uber", "taxi", "bus", "metro", "train", "subway", "car",
        "fuel", "gas", "petrol", "parking", "bike", "ride", "lyft", "bolt",
    ],
    "Entertainment": [
        "entertainment", "movie", "cinema", "game", "games", "concert", "theater",
        "theatre", "netflix", "spotify", "streaming", "music", "bar", "pub",
        "drinks", "nightclub", "event", "festival",
    ],
    "Clothing": [
        "clothing", "clothes", "shirt", "pants", "shoes", "dress", "jacket",
        "fashion", "zara", "h&m", "hm", "adidas", "nike", "outfit", "jeans",
    ],
    "Education": [
        "education", "book", "books", "course", "university", "school",
        "tutorial", "class", "learning", "udemy", "coursera", "study",
    ],
    "Health": [
        "health", "doctor", "medicine", "pharmacy", "gym", "fitness",
        "hospital", "dentist", "vitamin", "supplement", "workout",
    ],
    "Personal Care": [
        "personal", "haircut", "barber", "salon", "beauty", "cosmetics",
        "toiletries", "shampoo", "soap", "hygiene",
    ],
    "Subscriptions": [
        "subscription", "monthly", "annual", "membership", "icloud",
        "prime", "youtube", "apple", "google", "microsoft", "plan",
    ],
    "Housing": [
        "housing", "rent", "utilities", "electricity", "water", "internet",
        "phone", "bill", "bills", "mortgage", "insurance", "repair", "maintenance",
    ],
    "Website Business": [
        "website", "domain", "hosting", "business insurance", "saas",
        "server", "vps", "ssl", "cloudflare", "aws", "vercel", "render",
        "stripe", "software license", "web hosting",
    ],
    "Travel": [
        "travel", "flight", "hotel", "airbnb", "vacation", "holiday",
        "trip", "booking", "hostel", "airport",
    ],
    "RA Work": [
        "ra work", "research assistant", "research", "lab supplies", "lab",
        "academic work", "research materials", "experiment", "reagent",
    ],
    "GRE Prep": [
        "gre", "gre prep", "magoosh", "manhattan prep", "ets", "gre book",
        "gre course", "gre test", "gre fee", "gre registration", "practice test",
    ],
    "Gifts": [
        "gift", "gifts", "present", "presents", "chocolate", "flowers",
        "birthday", "anniversary", "christmas", "xmas", "wrapped", "surprise",
    ],
    "Income": [
        "salary", "wage", "income", "paycheck", "payment", "freelance",
        "dividend", "bonus", "earnings", "revenue", "received", "got paid",
    ],
    "To Investments": [
        "invest", "investing", "etf", "stock", "stocks", "crypto",
        "bitcoin", "fund", "portfolio",
    ],
    "From Investments": [
        "withdraw", "withdrawal", "redeem", "from invest", "from savings",
        "sell stocks", "sell etf",
    ],
}


def detect_category(text: str) -> tuple[str, str]:
    lower = text.lower()

    if any(p in lower for p in ["to invest", "to savings", "invest"]):
        return "transfer", "To Investments"
    if any(p in lower for p in ["from invest", "from saving", "withdraw"]):
        return "transfer", "From Investments"

    if text.lstrip().startswith("+") or any(k in lower for k in _KEYWORDS["Income"]):
        return "income", "Income"

    best_cat = "Food"
    best_score = 0
    for cat, keywords in _KEYWORDS.items():
        if cat in ("Income", "To Investments", "From Investments"):
            continue
        score = sum(1 for k in keywords if k in lower)
        if score > best_score:
            best_score = score
            best_cat = cat

    return "expense", best_cat


def is_impulse(text: str) -> bool:
    return any(w in text.lower() for w in ["impulse", "impulsive", "unnecessary", "splurge"])
