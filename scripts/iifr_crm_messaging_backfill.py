#!/usr/bin/env python3.11
"""
CRM Messaging WhatsApp backfill / incremental fetch.

API docs: https://crm-messaging.cloud/docs/message-history-api/
Endpoint: GET https://app.crm-messaging.cloud/index.php/Api/messageHistory
Auth:    Bearer token (CRM_MESSAGING_API_KEY in .env)
Params:  page, per_page (max 500).  No status/start_date/end_date filters.

Status is in the message body as deliveryStatus; direction is OUTGOING/INCOMING.
Filtering by status/direction/date is done in Python after fetching.

Usage:
    python3.11 iifr_crm_messaging_backfill.py --mode backfill
    python3.11 iifr_crm_messaging_backfill.py --mode incremental [--days 1]
    python3.11 iifr_crm_messaging_backfill.py --mode probe

Exit codes: 0 = success, 1 = failure (auth, network, etc.)
"""

import json, logging, os, sys, time
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
ENV_FILE   = SCRIPT_DIR / ".env"
TOKEN_KEY  = "CRM_MESSAGING_API_KEY"
API_BASE   = "https://app.crm-messaging.cloud/index.php/Api"

# Output
BASE_DIR   = Path.home() / "Documents/Mac Mini Sync/IIFR-ECP-Data/crm/whatsapp"
RAW_DIR    = BASE_DIR / "raw"
NORM_DIR   = BASE_DIR / "normalized"
INCR_DIR   = BASE_DIR / "incremental"
MANIFEST   = BASE_DIR / "manifests/whatsapp_manifest.json"

for _d in [RAW_DIR, NORM_DIR, INCR_DIR, MANIFEST.parent]:
    _d.mkdir(parents=True, exist_ok=True)

LOG_FILE   = RAW_DIR / f"crm_messaging_{datetime.now():%Y%m%d_%H%M%S}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("crm_messaging")


# ── Lazy token / header access ───────────────────────────────────────────────
def _load_env() -> str:
    """Return the CRM_MESSAGING_API_KEY from the script-local .env file.

    Hermes gateway / shell env injection has precedence bugs for this script,
    so the file on disk is treated as the source of truth.
    """
    if not ENV_FILE.exists():
        log.error(".env not found at %s", ENV_FILE)
        return ""

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == TOKEN_KEY:
            return v.strip()

    log.error("%s not found in %s", TOKEN_KEY, ENV_FILE)
    return ""


def _headers() -> dict:
    token = _load_env()
    if not token:
        log.error("CRM_MESSAGING_API_KEY not found in environment or %s", ENV_FILE)
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _token() -> str:
    return _load_env()


# ── API client ───────────────────────────────────────────────────────────────
def fetch_all_messages(pages_limit: int = 100) -> list[dict]:
    """Fetch every page from /messageHistory. Returns list of message dicts."""
    import requests
    all_msgs, page, per_page = [], 1, 500

    while page <= pages_limit:
        params = {"page": page, "per_page": per_page}
        log.info("Fetching page %d ...", page)
        try:
            r = requests.get(API_BASE + "/messageHistory",
                             headers=_headers(), params=params, timeout=30)
        except Exception as exc:
            log.error("Network error page %d: %s", page, exc)
            break

        if r.status_code == 401:
            log.error("Auth failed (401) — CRM_MESSAGING_API_KEY may be expired/invalid")
            sys.exit(1)
        if r.status_code != 200:
            log.error("HTTP %d: %s", r.status_code, r.text[:300])
            break

        d = r.json()
        if d.get("status") != "success":
            log.error("API error: %s", d.get("message", d))
            break

        data     = d.get("data", {})
        messages = data.get("messages", [])
        if not messages:
            log.info("No more messages at page %d", page)
            break

        all_msgs.extend(messages)
        total = data.get("total", "?")
        log.info("  page %d: +%d messages  (cumulative %d, total %s)",
                 page, len(messages), len(all_msgs), total)
        if len(messages) < per_page:
            break
        page += 1
        time.sleep(0.5)

    log.info("Fetched %d raw messages", len(all_msgs))
    return all_msgs


# ── Dedupe ───────────────────────────────────────────────────────────────────
def dedupe(messages: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for m in messages:
        mid = m.get("msgId") or m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)
    log.info("After dedupe (msgId): %d unique", len(unique))
    return unique


# ── Date parsing ─────────────────────────────────────────────────────────────
def parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00").replace(" +", "+").replace(" ", "T")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.fromisoformat(raw if "%z" in fmt else raw[:19])
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw[:10])
    except ValueError:
        return None


# ── Filtering (in Python — API doesn't support these) ────────────────────────
def filter_messages(
    messages: list[dict],
    start_date: str | None = None,
    end_date: str | None = None,
    direction: str | None = None,
    channel: str = "whatsapp",
    delivery_status: str | None = None,
) -> list[dict]:
    result = []
    for m in messages:
        if m.get("channel", "").lower() != channel.lower():
            continue
        if direction and m.get("direction", "").upper() != direction.upper():
            continue
        if delivery_status and m.get("deliveryStatus", "").upper() != delivery_status.upper():
            continue
        ts = parse_date(m.get("date", ""))
        if ts is None:
            continue
        date_str = ts.strftime("%Y-%m-%d")
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        result.append(m)
    log.info("After filters [ch=%s, dir=%s, status=%s, start=%s, end=%s]: %d",
             channel, direction, delivery_status, start_date, end_date, len(result))
    return result


# ── Manifest ─────────────────────────────────────────────────────────────────
def read_manifest() -> dict:
    if MANIFEST.exists():
        with open(MANIFEST) as f:
            return json.load(f)
    return {}


def write_manifest(m: dict) -> None:
    with open(MANIFEST, "w") as f:
        json.dump(m, f, indent=2)
    log.info("Manifest written: %s", MANIFEST)


# ── Write outputs ─────────────────────────────────────────────────────────────
def write_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info("Written %d messages → %s  (%d KB)",
             len(data), path, path.stat().st_size // 1024)


# ── Modes ─────────────────────────────────────────────────────────────────────
def mode_probe() -> None:
    import requests
    r = requests.get(API_BASE + "/messageHistory",
                      headers=_headers(), params={"page": 1, "per_page": 5}, timeout=15)
    d = r.json()
    total  = d.get("data", {}).get("total", "?")
    status = d.get("status", "error")
    log.info("PROBE: HTTP %d | status=%s | total=%s", r.status_code, status, total)
    if r.status_code == 200 and status == "success":
        log.info("AUTH OK")
        sys.exit(0)
    else:
        log.error("AUTH FAIL — message: %s", d.get("message"))
        sys.exit(1)


def mode_backfill() -> None:
    messages = fetch_all_messages()
    if not messages:
        sys.exit(1)
    messages = dedupe(messages)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RAW_DIR / f"whatsapp_backfill_{ts}.json"
    write_json(path, messages)

    m = read_manifest()
    m["backfill"] = {"run_ts": datetime.now().isoformat(), "file": str(path),
                     "unique_count": len(messages)}
    write_manifest(m)
    log.info("BACKFILL DONE — %d messages", len(messages))


def mode_incremental(days: int = 1) -> None:
    end_str   = datetime.now().strftime("%Y-%m-%d")
    start_dt  = datetime.now() - timedelta(days=days)
    start_str = start_dt.strftime("%Y-%m-%d")

    messages = fetch_all_messages()
    if not messages:
        sys.exit(1)
    messages = dedupe(messages)
    messages = filter_messages(messages, start_date=start_str, end_date=end_str)

    path = INCR_DIR / f"whatsapp_incremental_{start_str}_{end_str}.json"
    write_json(path, messages)

    m = read_manifest()
    entry = {"run_ts": datetime.now().isoformat(), "file": str(path),
             "unique_count": len(messages), "days": days,
             "start_date": start_str, "end_date": end_str}
    m.setdefault("incremental_runs", []).append(entry)
    m["last_incremental"] = entry
    write_manifest(m)
    log.info("INCREMENTAL DONE — %d messages for %s to %s", len(messages), start_str, end_str)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["probe", "backfill", "incremental"],
                        default="incremental")
    parser.add_argument("--days", type=int, default=1)
    args = parser.parse_args()

    token = _load_env()
    if not token:
        log.error("CRM_MESSAGING_API_KEY not found in environment or %s", ENV_FILE)
        sys.exit(1)

    if args.mode == "probe":
        mode_probe()
    elif args.mode == "backfill":
        mode_backfill()
    elif args.mode == "incremental":
        mode_incremental(days=args.days)


if __name__ == "__main__":
    main()
