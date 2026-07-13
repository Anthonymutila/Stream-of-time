import logging
import threading
import time
from datetime import datetime, timezone, timedelta

import requests as http_requests
from flask import Flask, render_template, jsonify, request, url_for

from utils.storage import load_articles, get_article_by_id, save_scrape_run, load_history, reclassify_articles
from scrapers.news_scraper import run_scrape, get_progress, SCRAPERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Background scrape state
_scrape_lock = threading.Lock()
_scrape_status = {"running": False, "last_stats": None, "started_at": None, "finished_at": None}

# Server-side cache for weather and currency
_cache = {"weather": {"data": None, "ts": 0}, "currency": {"data": None, "ts": 0}}
_CACHE_TTL = 1800  # 30 minutes


def _background_scrape():
    start_time = time.time()
    with _scrape_lock:
        _scrape_status["running"] = True
        _scrape_status["started_at"] = start_time
        _scrape_status["last_stats"] = None
    try:
        stats = run_scrape()
        duration = time.time() - start_time
        save_scrape_run(stats.get("scrape_id", "unknown"), stats, duration)
        with _scrape_lock:
            _scrape_status["last_stats"] = stats
    except Exception as e:
        logger.exception("Background scrape failed")
        with _scrape_lock:
            _scrape_status["last_stats"] = {"error": str(e)}
    finally:
        with _scrape_lock:
            _scrape_status["running"] = False
            _scrape_status["finished_at"] = time.time()


def _parse_date(date_str: str):
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _article_date(article):
    return _parse_date(article.get("published_date") or article.get("scraped_date") or "")


def _is_recent(article, cutoff):
    dt = _article_date(article)
    if dt is None:
        return False
    return dt.date() >= cutoff.date()


@app.route("/")
def index():
    all_articles = load_articles()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    articles = [a for a in all_articles if _is_recent(a, cutoff)]
    category = request.args.get("category", "").strip().lower()
    if category:
        articles = [a for a in articles if a.get("category", "").lower() == category]
    articles.sort(key=lambda a: a.get("published_date") or a.get("scraped_date") or "", reverse=True)
    sources = sorted({a["source"] for a in articles} | {name for name, _ in SCRAPERS})
    case_types = sorted({a.get("case_type", "") for a in articles})
    return render_template(
        "index.html",
        articles=articles,
        total=len(articles),
        sources=sources,
        case_types=case_types,
        active_category=category,
    )


@app.route("/archive")
def archive():
    all_articles = load_articles()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    archived = [a for a in all_articles if not _is_recent(a, cutoff)]
    archived.sort(key=lambda a: a.get("published_date") or a.get("scraped_date") or "", reverse=True)

    months = {}
    for a in archived:
        dt = _article_date(a)
        if dt:
            key = (dt.year, dt.month)
        else:
            key = (0, 0)
        months.setdefault(key, []).append(a)
    months = dict(sorted(months.items(), reverse=True))

    return render_template(
        "archive.html",
        months=months,
        total=len(archived),
    )


@app.route("/weather")
def weather():
    now = time.time()
    if _cache["weather"]["data"] and now - _cache["weather"]["ts"] < _CACHE_TTL:
        return jsonify(_cache["weather"]["data"])
    try:
        resp = http_requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": -15.3875,
                "longitude": 28.3228,
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                "timezone": "Africa/Lusaka",
                "forecast_days": 3,
            },
            timeout=5,
        )
        data = resp.json()
        current = data.get("current", {})
        daily = data.get("daily", {})
        weather_codes = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
            55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 80: "Slight rain showers",
            81: "Moderate rain showers", 82: "Violent rain showers", 95: "Thunderstorm",
            96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
        }
        today_code = daily.get("weather_code", [0])[0]
        forecast = []
        for i in range(3):
            forecast.append({
                "date": daily.get("time", [""])[i],
                "max": daily.get("temperature_2m_max", [0])[i],
                "min": daily.get("temperature_2m_min", [0])[i],
                "desc": weather_codes.get(daily.get("weather_code", [0])[i], "Unknown"),
            })
        result = {
            "current": {
                "temp": current.get("temperature_2m"),
                "humidity": current.get("relative_humidity_2m"),
                "wind": current.get("wind_speed_10m"),
                "code": current.get("weather_code", 0),
                "desc": weather_codes.get(current.get("weather_code", 0), "Unknown"),
            },
            "forecast": forecast,
        }
        _cache["weather"] = {"data": result, "ts": now}
        return jsonify(result)
    except Exception as e:
        logger.warning("Weather fetch failed: %s", e)
        if _cache["weather"]["data"]:
            return jsonify(_cache["weather"]["data"])
        return jsonify({"error": "Weather data unavailable"}), 503


@app.route("/currency")
def currency():
    now = time.time()
    if _cache["currency"]["data"] and now - _cache["currency"]["ts"] < _CACHE_TTL:
        return jsonify(_cache["currency"]["data"])
    try:
        resp = http_requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = resp.json()
        if data.get("result") == "success":
            rate = data["rates"].get("ZMW")
            result = {"usd_to_zmw": rate, "date": data.get("time_last_update_utc", "")}
            _cache["currency"] = {"data": result, "ts": now}
            return jsonify(result)
        if _cache["currency"]["data"]:
            return jsonify(_cache["currency"]["data"])
        return jsonify({"error": "Currency data unavailable"}), 503
    except Exception as e:
        logger.warning("Currency fetch failed: %s", e)
        if _cache["currency"]["data"]:
            return jsonify(_cache["currency"]["data"])
        return jsonify({"error": "Currency data unavailable"}), 503


@app.route("/history")
def history():
    runs = load_history()
    runs.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return render_template("history.html", runs=runs, total=len(runs))


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/news")
def news_json():
    articles = load_articles()
    articles.sort(key=lambda a: a.get("scraped_date", ""), reverse=True)
    return jsonify({"total": len(articles), "articles": articles})


@app.route("/scrape", methods=["POST"])
def scrape():
    with _scrape_lock:
        if _scrape_status["running"]:
            return jsonify({"status": "already_running", "message": "Scrape already in progress"})
        _scrape_status["running"] = True
        _scrape_status["started_at"] = time.time()
        _scrape_status["last_stats"] = None

    thread = threading.Thread(target=_background_scrape, daemon=True)
    thread.start()
    return jsonify({"status": "started", "message": "Scraping started in background"})


@app.route("/scrape/status")
def scrape_status():
    with _scrape_lock:
        status = dict(_scrape_status)
    if status["running"] and status["started_at"]:
        status["elapsed"] = round(time.time() - status["started_at"], 1)
    elif status["finished_at"] and status["started_at"]:
        status["elapsed"] = round(status["finished_at"] - status["started_at"], 1)
    status["progress"] = get_progress()
    return jsonify(status)


@app.route("/history/json")
def history_json():
    runs = load_history()
    runs.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return jsonify({"total": len(runs), "runs": runs})


@app.route("/article/<article_id>")
def article_detail(article_id):
    article = get_article_by_id(article_id)
    if not article:
        return render_template("404.html"), 404
    return render_template("article.html", article=article)


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


@app.route("/reclassify", methods=["POST"])
def reclassify():
    changed = reclassify_articles()
    return jsonify({"status": "ok", "reclassified": changed})


if __name__ == "__main__":
    reclassify_articles()
    app.run(debug=True, host="0.0.0.0", port=5000)
