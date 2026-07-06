import re
from typing import Optional


# ---------------------------------------------------------------------------
# Category detection keywords
# ---------------------------------------------------------------------------

COURT_KEYWORDS = [
    "civil suit", "civil case", "civil matter",
    "sue", "sues", "sued", "suing", "filed a suit", "filed suit", "filed a case",
    "lawsuit", "damages awarded", "damages of", "injunction",
    "high court", "industrial relations court", "labor court", "labour court",
    "contract dispute", "land dispute", "property dispute", "boundary dispute",
    "debt recovery", "debt collection", "eviction order", "unlawful eviction",
    "divorce petition", "divorce case", "custody battle", "custody dispute",
    "maintenance order", "maintenance claim", "child support", "family law",
    "business dispute", "commercial dispute", "insurance claim",
    "personal injury", "compensation claim", "negligence claim",
    "defamation", "libel", "breach of contract", "specific performance",
    "receivership", "liquidation", "bankruptcy",
    "wrongful dismissal", "unfair termination",
    "employment dispute", "labour dispute", "trade dispute",
    "arbitration award", "plaintiff", "defendant", "appellant", "respondent",
    "petitioner", "claimant", "court order", "out of court",
    "sued for", "claiming damages", "award of damages",
    "ordered to pay", "ruled that", "judgment of the court", "court judgment",
    "court dismisses", "court rules", "court case", "court hearing",
    "ruling", "verdict", "settlement",
    "constitutional court", "magistrate court", "magistrates court",
    "supreme court", "court of appeal", "subordinate court",
    "judge", "judges", "justice", "bench",
    "sentence", "sentenced", "acquitted", "convicted",
    "bail", "remand", "detention",
    "applicant", "application",
    "hearings", "trial", "proceedings",
    "legal", "litigation", "advocate", "barrister", "lawyer",
    "attorney", "solicitor",
]

POLITICS_KEYWORDS = [
    "president", "minister", "parliament", "national assembly",
    "election", "polling", "candidate", "campaign", "vote", "voting",
    "upnd", "patriotic front", "pf ", "opposition", "government",
    "political party", "bill", "constitution", "speaker",
    "hh ", "hichilema", "lungu", "kabesha",
]

BUSINESS_KEYWORDS = [
    "economy", "gdp", "inflation", "interest rate", "stock", "share",
    "bond", "mortgage rate", "bank", "loan", "borrowing", "budget", "tax",
    "mining", "copper", "konkola", "kcm", "vedanta",
    "business", "company", "corporate", "trade", "export", "import",
    "investment", "market", "price", "currency", "kwacha",
]

CRIME_KEYWORDS = [
    "murder", "killed", "homicide", "manslaughter", "assault",
    "robbery", "armed robbery", "heist", "theft", "stealing", "burglary",
    "rape", "sexual assault", "defilement", "carnal knowledge",
    "corruption", "bribery", "embezzlement", "money laundering",
    "fraud", "forgery", "drug trafficking", "drug possession", "narcotics",
    "kidnapping", "abduction", "trafficking", "human trafficking", "smuggling",
    "arson", "attempted murder", "grievous bodily harm",
    "acc ", "anti-corruption commission",
    "criminal case", "criminal charges", "criminal trial", "criminal court",
    "police investigation", "police arrest", "detained", "remand",
    "prosecution", "prosecuted", "convicted", "acquitted",
    "bail", "jail", "prison", "sentence", "sentenced", "indictment",
    "treason", "sedition", "espionage", "terrorism",
    "jailed", "prison sentence",
]

SOCIAL_KEYWORDS = [
    "football", "soccer", "match", "tournament", "player", "coach",
    "music", "artist", "album", "concert", "entertainment",
    "wedding", "marriage", "divorce", "family",
    "accident", "fire", "flood", "disaster",
    "weather", "rain", "drought",
    "health", "hospital", "disease", "outbreak",
    "education", "school", "university", "student",
    "transport", "fuel", "road", "traffic",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def classify_article(title: str, content: str = "") -> str:
    """Classify article into a category. Returns category name."""
    title_norm = _normalize(title)
    content_norm = _normalize(content)
    combined = f"{title_norm} {content_norm}"

    # Check for strong political endorsement/support articles first
    political_endorsement_patterns = [
        "endorse", "endorsed", "endorsing", "endorsement",
        "support for", "supports", "backing",
        "second term", "re-election", "reelection",
        "vote for", "campaign for",
    ]
    is_political_endorsement = any(p in title_norm for p in political_endorsement_patterns)

    # Score each category
    scores = {
        "court": 0,
        "politics": 0,
        "business": 0,
        "crime": 0,
        "social": 0,
    }

    for kw in COURT_KEYWORDS:
        if kw in title_norm:
            scores["court"] += 3
        elif kw in combined:
            scores["court"] += 1

    for kw in POLITICS_KEYWORDS:
        if kw in title_norm:
            scores["politics"] += 3
        elif kw in combined:
            scores["politics"] += 1

    for kw in BUSINESS_KEYWORDS:
        if kw in title_norm:
            scores["business"] += 3
        elif kw in combined:
            scores["business"] += 1

    for kw in CRIME_KEYWORDS:
        if kw in title_norm:
            scores["crime"] += 3
        elif kw in combined:
            scores["crime"] += 1

    for kw in SOCIAL_KEYWORDS:
        if kw in title_norm:
            scores["social"] += 2
        elif kw in combined:
            scores["social"] += 1

    # If article is a political endorsement, override court classification
    if is_political_endorsement and scores["politics"] > 0:
        return "politics"

    # Get the highest scoring category
    best_category = max(scores, key=scores.get)
    best_score = scores[best_category]

    # If no strong match, default to general
    if best_score == 0:
        return "general"

    # If court and crime are close, prefer court (court cases about crime are still court news)
    if scores["court"] > 0 and scores["crime"] > 0 and scores["court"] >= scores["crime"]:
        return "court"

    return best_category


def detect_case_type(text: str) -> str:
    text_lower = _normalize(text)
    case_type_map = {
        "land": ["land dispute", "property dispute", "boundary dispute", "eviction order", "unlawful eviction"],
        "contract": ["contract dispute", "breach of contract", "specific performance", "business dispute", "commercial dispute"],
        "labour": ["industrial relations court", "labour court", "labor court", "employment dispute", "labour dispute", "trade dispute", "wrongful dismissal", "unfair termination"],
        "family": ["divorce petition", "divorce case", "custody battle", "custody dispute", "maintenance order", "maintenance claim", "child support", "family law"],
        "debt": ["debt recovery", "debt collection", "bankruptcy", "receivership", "liquidation"],
        "defamation": ["defamation", "libel"],
        "tort": ["negligence claim", "personal injury"],
    }
    for case_type, keywords in case_type_map.items():
        for kw in keywords:
            if kw in text_lower:
                return case_type
    return "general"


def is_civil_article(title: str, content: str) -> tuple[bool, Optional[str]]:
    """Check if article is a civil court case. Now also returns category."""
    category = classify_article(title, content)
    if category == "court":
        case_type = detect_case_type(_normalize(f"{title} {content}"))
        return True, case_type
    return False, category
