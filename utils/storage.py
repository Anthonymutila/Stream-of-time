import json
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool


DATABASE_URL = os.environ.get("DATABASE_URL")

_connection_pool: pool.ThreadedConnectionPool | None = None


def _get_pool() -> pool.ThreadedConnectionPool:
    global _connection_pool
    if _connection_pool is None:
        _connection_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            sslmode="require",
        )
    return _connection_pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    _get_pool().putconn(conn)


def init_db():
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                content TEXT,
                source TEXT,
                url TEXT UNIQUE,
                published_date TEXT,
                scraped_date TEXT,
                category TEXT,
                case_type TEXT,
                image_url TEXT,
                scrape_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
            CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
            CREATE INDEX IF NOT EXISTS idx_articles_scraped_date ON articles(scraped_date);

            CREATE TABLE IF NOT EXISTS scrape_history (
                id SERIAL PRIMARY KEY,
                scrape_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                duration_seconds REAL,
                stats TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_timestamp ON scrape_history(timestamp);
        """)
        conn.commit()
        cur.close()
    finally:
        _put_conn(conn)


init_db()


def generate_id(title: str, source: str) -> str:
    raw = f"{title.strip().lower()}|{source.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def generate_scrape_id() -> str:
    raw = f"{time.time()}|{os.getpid()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def load_articles() -> list[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM articles ORDER BY scraped_date DESC")
        rows = cur.fetchall()
        cur.close()
        return [dict(row) for row in rows]
    finally:
        _put_conn(conn)


def save_articles(articles: list[dict]):
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM articles")
        for a in articles:
            cur.execute(
                """INSERT INTO articles
                   (id, title, summary, content, source, url, published_date,
                    scraped_date, category, case_type, image_url, scrape_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                    title=EXCLUDED.title, summary=EXCLUDED.summary,
                    content=EXCLUDED.content, source=EXCLUDED.source,
                    url=EXCLUDED.url, published_date=EXCLUDED.published_date,
                    scraped_date=EXCLUDED.scraped_date, category=EXCLUDED.category,
                    case_type=EXCLUDED.case_type, image_url=EXCLUDED.image_url,
                    scrape_id=EXCLUDED.scrape_id""",
                (
                    a.get("id", ""),
                    a.get("title", ""),
                    a.get("summary", ""),
                    a.get("content", ""),
                    a.get("source", ""),
                    a.get("url", ""),
                    a.get("published_date", ""),
                    a.get("scraped_date", ""),
                    a.get("category", ""),
                    a.get("case_type", ""),
                    a.get("image_url", ""),
                    a.get("scrape_id", ""),
                ),
            )
        conn.commit()
        cur.close()
    finally:
        _put_conn(conn)


def article_exists(url: str, articles: list[dict]) -> bool:
    for a in articles:
        if a.get("url", "").strip() == url.strip():
            return True
    return False


def add_article(article: dict, articles: list[dict]) -> list[dict]:
    articles.append(article)
    return articles


def get_article_by_id(article_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM articles WHERE id = %s", (article_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    finally:
        _put_conn(conn)


def article_url_exists(url: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM articles WHERE url = %s", (url.strip(),))
        row = cur.fetchone()
        cur.close()
        return row is not None
    finally:
        _put_conn(conn)


def deduplicate(articles: list[dict]) -> list[dict]:
    seen_ids = set()
    unique = []
    for a in articles:
        aid = a.get("id", "")
        if aid and aid not in seen_ids:
            seen_ids.add(aid)
            unique.append(a)
    return unique


def load_history() -> list[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT scrape_id, timestamp, duration_seconds, stats FROM scrape_history ORDER BY timestamp DESC"
        )
        rows = cur.fetchall()
        cur.close()
        result = []
        for row in rows:
            entry = dict(row)
            if entry.get("stats"):
                try:
                    entry["stats"] = json.loads(entry["stats"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(entry)
        return result
    finally:
        _put_conn(conn)


def save_scrape_run(scrape_id: str, stats: dict, duration: float):
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scrape_history (scrape_id, timestamp, duration_seconds, stats) VALUES (%s, %s, %s, %s)",
            (
                scrape_id,
                datetime.now(timezone.utc).isoformat(),
                round(duration, 1),
                json.dumps(stats, ensure_ascii=False),
            ),
        )
        conn.commit()
        cur.close()
    finally:
        _put_conn(conn)


def reclassify_articles():
    from utils.filter import classify_article, detect_case_type, _normalize
    articles = load_articles()
    changed = 0
    for a in articles:
        old_cat = a.get("category", "")
        new_cat = classify_article(a.get("title", ""), a.get("content", ""))
        if new_cat != old_cat:
            a["category"] = new_cat
            if new_cat == "court":
                a["case_type"] = detect_case_type(_normalize(f"{a.get('title','')} {a.get('content','')}"))
            changed += 1
    if changed:
        save_articles(articles)
    return changed
