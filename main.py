import json
import logging
import os
from datetime import datetime, timezone
import psycopg2
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ev-scanner")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)


def run_scan() -> dict:
    if not ODDS_API_KEY:
        return {"status": "error", "detail": "Missing ODDS_API_KEY"}

    resp = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": ODDS_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    sports = resp.json()

    if DATABASE_URL:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scanner_runs (
                        id SERIAL PRIMARY KEY,
                        checked_at TIMESTAMPTZ NOT NULL,
                        sports_count INT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO scanner_runs (checked_at, sports_count) VALUES (%s, %s)",
                    (datetime.now(timezone.utc), len(sports)),
                )

    summary = f"EV Scanner ran at {datetime.now(timezone.utc).isoformat()} — {len(sports)} sports active."
    send_telegram(summary)
    return {"status": "ok", "sports_count": len(sports)}


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    try:
        if path == "/scan":
            result = run_scan()
            body = json.dumps(result).encode("utf-8")
            start_response("200 OK", [("Content-Type", "application/json")])
            return [body]

        body = json.dumps({"status": "alive", "service": "ev-scanner-public"}).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json")])
        return [body]
    except Exception as exc:
        logger.exception("Scan error")
        body = json.dumps({"status": "error", "detail": str(exc)}).encode("utf-8")
        start_response("500 Internal Server Error", [("Content-Type", "application/json")])
        return [body]
