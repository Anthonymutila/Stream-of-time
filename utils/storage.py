import json
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
NEWS_FILE = os.path.join(DATA_DIR, "court_news.json")
HISTORY_FILE = os.path.join(DATA_DIR, "scrape_history.json")

# In-memory cache
_cache = {"articles": None, "mtime": 0, "id_index": {}}
_history_cache = {"data": None, "mtime": 0}


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def generate_id(title: str, source: str) -> str:
    raw = f"{title.strip().lower()}|{source.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _rebuild_id_index(articles):
    index = {}
    for a in articles:
        aid = a.get("id", "")
        if aid:
            index[aid] = a
    return index


def load_articles() -> list[dict]:
    _ensure_data_dir()
    if not os.path.exists(NEWS_FILE):
        return []
    try:
        mtime = os.path.getmtime(NEWS_FILE)
        if _cache["articles"] is not None and _cache["mtime"] == mtime:
            return _cache["articles"]
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            articles = json.load(f)
        _cache["articles"] = articles
        _cache["mtime"] = mtime
        _cache["id_index"] = _rebuild_id_index(articles)
        return articles
    except (json.JSONDecodeError, IOError):
        return []


def _invalidate_cache():
    _cache["articles"] = None
    _cache["mtime"] = 0
    _cache["id_index"] = {}


def save_articles(articles: list[dict]):
    _ensure_data_dir()
    with open(NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False)
    _invalidate_cache()


def article_exists(url: str, articles: list[dict]) -> bool:
    for a in articles:
        if a.get("url", "").strip() == url.strip():
            return True
    return False


def add_article(article: dict, articles: list[dict]) -> list[dict]:
    articles.append(article)
    return articles


def get_article_by_id(article_id: str) -> Optional[dict]:
    load_articles()
    return _cache["id_index"].get(article_id)


def deduplicate(articles: list[dict]) -> list[dict]:
    seen_ids = set()
    unique = []
    for a in articles:
        aid = a.get("id", "")
        if aid and aid not in seen_ids:
            seen_ids.add(aid)
            unique.append(a)
    return unique


# ---------------------------------------------------------------------------
# Scrape history
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    _ensure_data_dir()
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        mtime = os.path.getmtime(HISTORY_FILE)
        if _history_cache["data"] is not None and _history_cache["mtime"] == mtime:
            return _history_cache["data"]
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _history_cache["data"] = data
        _history_cache["mtime"] = mtime
        return data
    except (json.JSONDecodeError, IOError):
        return []


def save_scrape_run(scrape_id: str, stats: dict, duration: float):
    _ensure_data_dir()
    history = load_history()
    entry = {
        "scrape_id": scrape_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration, 1),
        "stats": stats,
    }
    history.append(entry)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)
    _history_cache["data"] = None


def generate_scrape_id() -> str:
    raw = f"{time.time()}|{os.getpid()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
