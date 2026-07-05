# Zambia Civil Court News Aggregator

A Flask web application that collects and displays **civil court news stories** from Zambian news websites. Uses BeautifulSoup for web scraping, keyword-based filtering to identify civil cases, and JSON file storage.

## Setup

### 1. Create a virtual environment

```bash
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the application

```bash
python app.py
```

The server starts at `http://localhost:5000`.

### 4. Scrape news

- Visit `http://localhost:5000` and click **Refresh News**
- Or send a POST request: `POST http://localhost:5000/scrape`

Scraped articles are saved to `data/court_news.json`.

## Project Structure

```
agregator/
  app.py                     # Flask application
  requirements.txt
  scrapers/
    __init__.py
    news_scraper.py          # BeautifulSoup scraper for 12 Zambian news sites
  utils/
    __init__.py
    filter.py                # Civil case keyword detection + criminal exclusion
    storage.py               # JSON file read/write + deduplication
  templates/
    index.html               # Main news feed
    article.html             # Full article detail
    404.html                 # Not found page
  static/
    style.css                # Responsive UI styles
  data/
    court_news.json          # Scraped article storage
```

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | News feed page |
| GET | `/news` | All articles as JSON |
| POST | `/scrape` | Trigger scraping |
| GET | `/article/<id>` | Full article detail |

## News Sources

Lusaka Times, News Diggers, Zambia Daily Mail, Times of Zambia, ZNBC, Diamond TV, Mwebantu, Zambia Reports, Daily Nation Zambia, Zambian Eye, QFM Zambia, Hot FM Zambia.

## Filtering

Only civil court cases are included: land disputes, contract disputes, labour/employment cases, family law, debt recovery, insurance claims, defamation, and related matters.

Criminal cases, corruption (ACC), political cases, constitutional matters, police investigations, and national security cases are excluded.

## Storage

All data stored in `data/court_news.json`. No database required. Deduplication by URL and title+source hash.
