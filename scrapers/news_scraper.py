import gzip
import zlib
import time
import random
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, quote_plus
import threading

import requests
from bs4 import BeautifulSoup

from utils.filter import classify_article, detect_case_type, _normalize
from utils.storage import (
    generate_id,
    load_articles,
    save_articles,
    deduplicate,
    generate_scrape_id,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Thread-safe session for connection reuse
_session = requests.Session()
_session.headers.update(BROWSER_HEADERS)

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_adapter = HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20,
    max_retries=Retry(total=1, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

_lock = threading.Lock()

# Shared progress state
_progress = {"phase": "", "percent": 0, "detail": ""}
_progress_lock = threading.Lock()


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _set_progress(phase="", percent=0, detail=""):
    with _progress_lock:
        _progress["phase"] = phase
        _progress["percent"] = percent
        _progress["detail"] = detail


# Concurrency limits
SOURCE_WORKERS = 12
ARTICLE_WORKERS = 14
GOOGLE_WORKERS = 6
GOOGLE_BATCH = 5


def _decode_response(resp: requests.Response) -> str:
    raw = resp.content
    if raw[:2] == b'\x1f\x8b':
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif raw[:2] in (b'\x78\x01', b'\x78\x9c', b'\x78\xda'):
        try:
            raw = zlib.decompress(raw)
        except Exception:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                pass
    content_type = resp.headers.get("Content-Type", "")
    if "charset" in content_type:
        encoding = content_type.split("charset=")[-1].split(";")[0].strip()
    else:
        encoding = resp.encoding or "utf-8"
    return raw.decode(encoding, errors="replace")


def _get_page(url: str, retries: int = 1) -> Optional[BeautifulSoup]:
    for attempt in range(retries):
        try:
            resp = _session.get(url, timeout=5)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "html" not in ct and "xml" not in ct and "text" not in ct:
                logger.debug("Skipping non-HTML response (%s) from %s", ct, url)
                return None
            html = _decode_response(resp)
            if html[:3] == "ID3" or not html.strip():
                return None
            return BeautifulSoup(html, "lxml")
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(0.3)
    return None


def _extract_article_content(url: str) -> Optional[dict]:
    soup = _get_page(url)
    if not soup:
        return None

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og:
            title = og.get("content", "")
    if not title:
        return None

    paragraphs = []
    article_el = soup.find("article")
    if not article_el:
        article_el = soup.find("div", class_=lambda c: c and any(
            k in c.lower().split() for k in ("entry-content", "post-content", "article-content", "single-content", "post-body", "entry-body")
        ))
    container = article_el or soup
    for p in container.find_all("p"):
        text = p.get_text(strip=True)
        if len(text) > 30 and not text.startswith("Share") and "cookie" not in text.lower():
            paragraphs.append(text)
    content = "\n\n".join(paragraphs)
    summary = paragraphs[0] if paragraphs else ""
    if len(summary) > 300:
        summary = summary[:297] + "..."

    image_url = ""
    meta = soup.find("meta", property="og:image")
    if meta and meta.get("content"):
        image_url = meta["content"]

    published_date = ""
    meta_time = soup.find("meta", property="article:published_time")
    if meta_time and meta_time.get("content"):
        published_date = meta_time["content"]
    else:
        time_tag = soup.find("time")
        if time_tag:
            published_date = time_tag.get("datetime", time_tag.get_text(strip=True))
        else:
            for el in soup.find_all(class_=lambda c: c and "date" in c.lower()):
                t = el.get_text(strip=True)
                if t:
                    published_date = t
                    break

    return {
        "title": title,
        "content": content,
        "summary": summary,
        "published_date": published_date,
        "image_url": image_url,
    }


def _collect_links_from_page(soup: BeautifulSoup, base_url: str, source_name: str) -> list[dict]:
    articles = []
    domain = base_url.split("//")[1].split("/")[0]
    bare_domain = domain.lstrip("www.")
    for a_tag in soup.select("a[href]"):
        href = a_tag.get("href", "").strip()
        title = a_tag.get_text(strip=True)
        if len(title) < 20:
            continue
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(base_url, href)
        if domain in full or bare_domain in full:
            articles.append({"url": full, "title": title, "source": source_name})
    return articles


# ---------------------------------------------------------------------------
# Source-specific scrapers — each returns a list of link dicts
# ---------------------------------------------------------------------------

def _scrape_source(source_name: str, base_url: str, path_variants: list[str]) -> list[dict]:
    urls_to_try = [f"{base_url}{p}" for p in path_variants]
    urls_to_try.append(base_url)
    all_links = []
    seen = set()
    court_paths = [p.lower() for p in path_variants if "court" in p.lower()]
    for listing_url in urls_to_try:
        soup = _get_page(listing_url)
        if not soup:
            continue
        links = _collect_links_from_page(soup, base_url, source_name)
        is_court_page = any(cp in listing_url.lower() for cp in court_paths)
        for link in links:
            if link["url"] not in seen:
                seen.add(link["url"])
                if is_court_page:
                    link["force_category"] = "court"
                all_links.append(link)
    return all_links[:40]


def scrape_lusaka_times() -> list[dict]:
    return _scrape_source("Lusaka Times", "https://www.lusakatimes.com", [
        "/headlines/", "/other-news/", "/economy/",
        "/health/", "/ruralnews/",
    ])

def scrape_zambia_daily_mail() -> list[dict]:
    return []

def scrape_times_of_zambia() -> list[dict]:
    return _scrape_source("Times of Zambia", "https://www.times.co.zm", [
        "/news/", "/court-news/", "/business/",
        "/features/", "/sports/",
    ])

def scrape_mwebantu() -> list[dict]:
    return _scrape_source("Mwebantu", "https://www.mwebantu.com", [
        "/category/police/", "/category/politics/", "/category/business/",
    ])

def scrape_zambia_reports() -> list[dict]:
    return []

def scrape_daily_nation() -> list[dict]:
    return []

def scrape_zambian_eye() -> list[dict]:
    return _scrape_source("Zambian Eye", "https://www.zambianeye.com", [
        "/latest/", "/politics/", "/business/",
    ])

def scrape_kalemba_news() -> list[dict]:
    return _scrape_source("Kalemba News", "https://kalemba.news", [
        "/category/local/", "/category/politics/", "/category/business/",
        "/category/court/",
    ])

def scrape_mast_media() -> list[dict]:
    return _scrape_source("Mast Media", "https://mastmediazm.com", [
        "/category/courts-crime/", "/category/news/",
    ])

def scrape_zambian_observer() -> list[dict]:
    return _scrape_source("Zambian Observer", "https://zambianobserver.com", [
        "/category/politics/", "/category/court/", "/category/world/africa/",
        "/category/world/", "/category/business/", "/category/health/",
    ])


SCRAPERS = [
    ("Lusaka Times", scrape_lusaka_times),
    ("Zambia Daily Mail", scrape_zambia_daily_mail),
    ("Times of Zambia", scrape_times_of_zambia),
    ("Mwebantu", scrape_mwebantu),
    ("Zambia Reports", scrape_zambia_reports),
    ("Daily Nation Zambia", scrape_daily_nation),
    ("Zambian Eye", scrape_zambian_eye),
    ("Kalemba News", scrape_kalemba_news),
    ("Mast Media", scrape_mast_media),
    ("Zambian Observer", scrape_zambian_observer),
]

# ---------------------------------------------------------------------------
# Google search (parallel batches)
# ---------------------------------------------------------------------------

GOOGLE_QUERIES = [
    "site:lusakatimes.com civil court ruling Zambia",
    "site:zambiadailymail.com civil court ruling Zambia",
    "site:times.co.zm civil court ruling Zambia",
    "site:mwebantu.com civil court ruling Zambia",
    "site:zambiareports.com civil court ruling Zambia",
    "site:dailynationzambia.com civil court ruling Zambia",
    "site:zambianeye.com civil court ruling Zambia",
    "site:kalemba.news civil court ruling Zambia",
    "site:mastmediazm.com civil court ruling Zambia",
    "site:zambianobserver.com civil court ruling Zambia",
    "Zambia civil lawsuit filed damages",
    "Zambia high court civil judgment",
    "Zambia industrial relations court labour ruling",
    "Zambia family court divorce maintenance order",
    "Zambia land dispute court ruling",
    "Zambia breach of contract court",
]

GOOGLE_SOURCE_MAP = {
    "lusakatimes.com": "Lusaka Times",
    "zambiadailymail.com": "Zambia Daily Mail",
    "times.co.zm": "Times of Zambia",
    "mwebantu.com": "Mwebantu",
    "zambiareports.com": "Zambia Reports",
    "dailynationzambia.com": "Daily Nation Zambia",
    "zambianeye.com": "Zambian Eye",
    "kalemba.news": "Kalemba News",
    "mastmediazm.com": "Mast Media",
    "zambianobserver.com": "Zambian Observer",
}


def _google_search_one(query: str) -> list[dict]:
    url = f"https://www.google.com/search?q={quote_plus(query)}&num=8"
    soup = _get_page(url, retries=1)
    if not soup:
        return []
    results = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/url?q=" in href:
            actual = href.split("/url?q=")[1].split("&")[0]
            title = a.get_text(strip=True)
            if len(title) > 15:
                source_name = "Web"
                for domain, name in GOOGLE_SOURCE_MAP.items():
                    if domain in actual.lower():
                        source_name = name
                        break
                results.append({"url": actual, "title": title, "source": source_name})
    return results


def scrape_google_court_news() -> list[dict]:
    all_results = []
    with ThreadPoolExecutor(max_workers=GOOGLE_WORKERS) as executor:
        futures = {executor.submit(_google_search_one, q): q for q in GOOGLE_QUERIES}
        for future in as_completed(futures):
            try:
                all_results.extend(future.result())
            except Exception as e:
                logger.warning("Google query failed: %s", e)
    return all_results


# ---------------------------------------------------------------------------
# Parallel article detail fetcher
# ---------------------------------------------------------------------------

def _fetch_and_filter(link: dict, existing_urls: set, scrape_id: str = "") -> Optional[dict]:
    url = link["url"]
    if url in existing_urls:
        return None

    detail = _extract_article_content(url)
    if not detail or not detail.get("content"):
        return None

    title = detail["title"] or link.get("title", "")
    if not title:
        return None

    force_cat = link.get("force_category", "")
    if force_cat:
        category = force_cat
    else:
        category = classify_article(title, detail["content"])
    case_type = detect_case_type(_normalize(f"{title} {detail['content']}")) if category == "court" else "general"

    article_id = generate_id(title, link["source"])
    return {
        "id": article_id,
        "title": title,
        "summary": detail["summary"],
        "content": detail["content"],
        "source": link["source"],
        "url": url,
        "published_date": detail["published_date"],
        "scraped_date": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "case_type": case_type,
        "image_url": detail["image_url"],
        "scrape_id": scrape_id,
    }


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run_scrape() -> dict:
    scrape_id = generate_scrape_id()
    existing = load_articles()
    existing_urls = {a["url"] for a in existing}
    stats = {
        "scrape_id": scrape_id,
        "scraped": 0,
        "civil_found": 0,
        "duplicates_skipped": 0,
        "errors": 0,
        "sources": {},
        "google_results": 0,
        "total_existing": len(existing),
    }

    all_links = []

    # Phase 1: scrape all sources in parallel
    _set_progress("Scanning news sources", 0, f"0/{len(SCRAPERS)} sources")
    logger.info("Phase 1: Scraping %d sources in parallel...", len(SCRAPERS))
    sources_done = 0
    with ThreadPoolExecutor(max_workers=SOURCE_WORKERS) as executor:
        futures = {}
        for source_name, scraper_fn in SCRAPERS:
            futures[executor.submit(scraper_fn)] = source_name
        for future in as_completed(futures):
            source_name = futures[future]
            try:
                links = future.result()
                stats["sources"][source_name] = len(links)
                all_links.extend(links)
                logger.info("  %s: %d links", source_name, len(links))
            except Exception as e:
                logger.error("  %s failed: %s", source_name, e)
                stats["errors"] += 1
            sources_done += 1
            _set_progress("Scanning news sources", round(sources_done / len(SCRAPERS) * 100), f"{sources_done}/{len(SCRAPERS)} sources done")

    # Phase 2: Google search in parallel
    _set_progress("Scanning Google", 0, "Running queries...")
    logger.info("Phase 2: Google search discovery...")
    try:
        google_links = scrape_google_court_news()
        stats["google_results"] = len(google_links)
        all_links.extend(google_links)
        _set_progress("Scanning Google", 100, f"{len(google_links)} results")
        logger.info("  Google: %d links", len(google_links))
    except Exception as e:
        logger.error("Google search error: %s", e)
        _set_progress("Scanning Google", 100, "Search failed")

    # Deduplicate links
    seen_urls = set()
    unique_links = []
    for link in all_links:
        if link["url"] not in seen_urls:
            seen_urls.add(link["url"])
            unique_links.append(link)

    # Filter out already-stored URLs
    new_links = [l for l in unique_links if l["url"] not in existing_urls]
    stats["duplicates_skipped"] = len(unique_links) - len(new_links)
    stats["total_links_found"] = len(unique_links)
    logger.info("Phase 3: Fetching %d new article pages with %d workers...", len(new_links), ARTICLE_WORKERS)

    # Phase 3: fetch articles in parallel
    _set_progress("Scanning articles", 0, f"0/{len(new_links)} articles")
    new_articles = []
    articles_done = 0
    with ThreadPoolExecutor(max_workers=ARTICLE_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_and_filter, link, existing_urls, scrape_id): link
            for link in new_links
        }
        for future in as_completed(futures):
            link = futures[future]
            try:
                article = future.result()
                if article:
                    new_articles.append(article)
                    stats["civil_found"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                logger.debug("Article fetch failed: %s - %s", link.get("url", "?"), e)
                stats["errors"] += 1
            articles_done += 1
            if new_links:
                _set_progress("Scanning articles", round(articles_done / len(new_links) * 100), f"{articles_done}/{len(new_links)} articles")

    # Save
    existing.extend(new_articles)
    existing = deduplicate(existing)
    save_articles(existing)
    stats["scraped"] = len(new_articles) + stats["errors"]
    stats["total_after"] = len(existing)

    logger.info(
        "Done: %d civil articles added, %d errors, %d duplicates skipped",
        stats["civil_found"], stats["errors"], stats["duplicates_skipped"],
    )
    return stats
