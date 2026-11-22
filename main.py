import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
import psycopg2
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ev-scanner")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

AUTO_SCAN_ENABLED = os.environ.get("AUTO_SCAN_ENABLED", "true").strip().lower() in ("1", "true", "yes")
AUTO_SCAN_INTERVAL_SECONDS = max(60, int(os.environ.get("AUTO_SCAN_INTERVAL_SECONDS", "300")))
PRE_KICKOFF_MINUTES = max(5.0, float(os.environ.get("PRE_KICKOFF_MINUTES", "90")))
MAX_LEAGUES_PER_SCAN = max(1, min(15, int(os.environ.get("MAX_LEAGUES_PER_SCAN", "6"))))
REGIONS = os.environ.get("ODDS_REGIONS", "uk,eu")
MARKETS = "h2h,totals,btts"

_scan_lock = threading.Lock()
_last_scan_result = None


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=12,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to send Telegram alert")


def parse_iso_time(value: str):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_selection_label(market_key: str, outcome_name: str, point) -> str:
    if market_key == "totals" and point is not None:
        return f"{outcome_name} {point}"
    if market_key == "btts":
        return f"BTTS {outcome_name}"
    return outcome_name


def get_active_soccer_leagues() -> list:
    headers = {"Accept": "application/json"}
    resp = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": ODDS_API_KEY},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    sports = resp.json()
    if not isinstance(sports, list):
        return []

    priority_prefixes = (
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_germany_bundesliga",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
    )

    active_soccer = []
    for s in sports:
        key = str(s.get("key", ""))
        if s.get("active") and key.startswith("soccer_"):
            active_soccer.append(
                {
                    "key": key,
                    "title": s.get("title") or key,
                    "group": s.get("group") or "Soccer",
                }
            )

    active_soccer.sort(
        key=lambda item: (
            0 if any(item["key"].startswith(p) for p in priority_prefixes) else 1,
            item["key"],
        )
    )
    return active_soccer[:MAX_LEAGUES_PER_SCAN]


def fetch_soccer_events() -> tuple:
    headers = {"Accept": "application/json"}
    leagues = get_active_soccer_leagues()
    league_keys = [lg["key"] for lg in leagues]

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": "decimal",
    }

    all_events = []
    for key in league_keys:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                params=params,
                headers=headers,
                timeout=20,
            )
            if r.status_code == 200 and isinstance(r.json(), list):
                for ev in r.json():
                    ev.setdefault("sport_key", key)
                    all_events.append(ev)
            else:
                logger.warning("League %s returned status %s", key, r.status_code)
        except Exception:
            logger.exception("Failed fetching odds for league %s", key)

    return all_events, league_keys


def collect_ev_from_events(events: list, start_time: datetime, end_time: datetime):
    matched_events = []
    ev_picks = []
    now_utc = datetime.now(timezone.utc)

    for event in events:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        league = event.get("sport_title") or event.get("sport_key") or "Soccer"
        label = f"{home} vs {away}"
        commence_str = event.get("commence_time", "")
        commence = parse_iso_time(commence_str)

        if commence and not (start_time <= commence <= end_time):
            continue

        minutes_to_kickoff = (
            round((commence - now_utc).total_seconds() / 60.0, 1)
            if commence
            else None
        )
        matched_events.append(
            {
                "league": league,
                "match": label,
                "commence_time": commence_str,
                "minutes_to_kickoff": minutes_to_kickoff,
            }
        )

        line_groups = {}
        for bm in event.get("bookmakers", []):
            bm_title = bm.get("title") or bm.get("key") or "Bookmaker"
            for market in bm.get("markets", []):
                m_key = market.get("key")
                if m_key not in ("h2h", "totals", "btts"):
                    continue
                for out in market.get("outcomes", []):
                    name = out.get("name")
                    price = out.get("price")
                    point = out.get("point")
                    if not name or not isinstance(price, (int, float)) or price <= 1.0:
                        continue

                    sel_label = format_selection_label(m_key, str(name), point)
                    group_key = (m_key, point if m_key == "totals" else None)
                    line_groups.setdefault(group_key, {}).setdefault(sel_label, []).append(
                        (bm_title, float(price))
                    )

        for (m_key, point), outcome_odds in line_groups.items():
            if len(outcome_odds) < 2:
                continue

            avg_implied = {}
            for sel_label, quotes in outcome_odds.items():
                avg_implied[sel_label] = sum(1.0 / p for _, p in quotes) / len(quotes)

            overround = sum(avg_implied.values())
            if overround <= 0:
                continue

            fair_probs = {sel_label: imp / overround for sel_label, imp in avg_implied.items()}

            for sel_label, quotes in outcome_odds.items():
                fair_p = fair_probs[sel_label]
                fair_decimal = 1.0 / fair_p
                for bm_title, price in quotes:
                    ev_pct = ((price * fair_p) - 1.0) * 100.0
                    if ev_pct > 0:
                        pick = {
                            "league": league,
                            "match": label,
                            "commence_time": commence_str,
                            "minutes_to_kickoff": minutes_to_kickoff,
                            "market": m_key,
                            "selection": sel_label,
                            "bookmaker": bm_title,
                            "odds": round(price, 2),
                            "fair_odds": round(fair_decimal, 2),
                            "ev_percent": round(ev_pct, 2),
                        }
                        if point is not None:
                            pick["line"] = point
                        ev_picks.append(pick)

    matched_events.sort(
        key=lambda x: x["minutes_to_kickoff"] if x["minutes_to_kickoff"] is not None else 999999
    )
    ev_picks.sort(key=lambda x: x["ev_percent"], reverse=True)
    return matched_events, ev_picks


def log_to_supabase(checked_at: datetime, picks_count: int) -> None:
    if not DATABASE_URL:
        return
    conn_str = DATABASE_URL
    if "sslmode=" not in conn_str:
        sep = "&" if "?" in conn_str else "?"
        conn_str = f"{conn_str}{sep}sslmode=require"

    with psycopg2.connect(conn_str, connect_timeout=10) as conn:
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
                (checked_at, picks_count),
            )


def run_pre_kickoff_ev_scan(window_minutes: float = None, notify: bool = True) -> dict:
    global _last_scan_result
    if not ODDS_API_KEY:
        return {"status": "error", "detail": "Missing ODDS_API_KEY"}

    minutes_limit = PRE_KICKOFF_MINUTES if window_minutes is None else max(5.0, float(window_minutes))

    with _scan_lock:
        events, leagues_scanned = fetch_soccer_events()
        now = datetime.now(timezone.utc)
        window_start = now
        primary_cutoff = now + timedelta(minutes=minutes_limit)

        matched_events, ev_picks = collect_ev_from_events(events, window_start, primary_cutoff)
        used_window = f"next {int(minutes_limit)} mins before kickoff"

        if not matched_events:
            fallback_cutoff = now + timedelta(hours=24.0)
            matched_events, ev_picks = collect_ev_from_events(events, window_start, fallback_cutoff)
            used_window = "next 24h (fallback — none in pre-kickoff window)"

        log_to_supabase(now, len(ev_picks))

        if notify:
            if ev_picks:
                top = ev_picks[0]
                msg = (
                    f"+EV Pick ({top['league']} | {top['market'].upper()} | {used_window}): "
                    f"{top['selection']} in {top['match']} @ {top['odds']} on {top['bookmaker']} "
                    f"(+{top['ev_percent']}% EV, fair ~{top['fair_odds']})"
                )
            else:
                msg = (
                    f"Pre-kickoff scan ({used_window}): {len(matched_events)} matches checked across "
                    f"{len(leagues_scanned)} leagues (1X2, Totals & BTTS) — no +EV picks right now."
                )
            send_telegram(msg)

        result = {
            "status": "ok",
            "checked_at": now.isoformat(),
            "window_used": used_window,
            "leagues_scanned": leagues_scanned,
            "markets": ["1x2 (h2h)", "over/under (totals)", "btts"],
            "matched_events_count": len(matched_events),
            "ev_selections_count": len(ev_picks),
            "matched_events": matched_events[:30],
            "ev_selections": ev_picks[:30],
        }
        _last_scan_result = result
        return result


def _background_scanner_loop() -> None:
    time.sleep(10)
    while AUTO_SCAN_ENABLED:
        try:
            logger.info("Running automatic pre-kickoff +EV scan...")
            run_pre_kickoff_ev_scan(notify=True)
        except Exception:
            logger.exception("Automatic pre-kickoff scan failed")
        time.sleep(AUTO_SCAN_INTERVAL_SECONDS)


if AUTO_SCAN_ENABLED and ODDS_API_KEY:
    worker_thread = threading.Thread(target=_background_scanner_loop, daemon=True)
    worker_thread.start()


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        if path == "/scan":
            mins_raw = qs.get("minutes", [None])[0]
            mins_val = float(mins_raw) if mins_raw is not None else None
            result = run_pre_kickoff_ev_scan(window_minutes=mins_val, notify=True)
            body = json.dumps(result, indent=2).encode("utf-8")
            start_response("200 OK", [("Content-Type", "application/json")])
            return [body]

        body = json.dumps(
            {
                "status": "alive",
                "mode": "automatic pre-kickoff +EV scanner",
                "auto_scan_enabled": AUTO_SCAN_ENABLED,
                "auto_scan_interval_seconds": AUTO_SCAN_INTERVAL_SECONDS,
                "pre_kickoff_minutes": PRE_KICKOFF_MINUTES,
                "max_leagues_per_scan": MAX_LEAGUES_PER_SCAN,
                "markets": ["1x2 (h2h)", "over/under (totals)", "btts"],
                "manual_scan_url": "/scan",
                "last_scan_summary": (
                    {
                        "checked_at": _last_scan_result.get("checked_at"),
                        "window_used": _last_scan_result.get("window_used"),
                        "leagues_scanned": _last_scan_result.get("leagues_scanned"),
                        "matched_events_count": _last_scan_result.get("matched_events_count"),
                        "ev_selections_count": _last_scan_result.get("ev_selections_count"),
                    }
                    if _last_scan_result
                    else None
                ),
            },
            indent=2,
        ).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json")])
        return [body]
    except Exception as exc:
        logger.exception("Scan error")
        body = json.dumps({"status": "error", "detail": str(exc)}).encode("utf-8")
        start_response("500 Internal Server Error", [("Content-Type", "application/json")])
        return [body]
