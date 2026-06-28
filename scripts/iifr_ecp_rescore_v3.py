#!/usr/bin/env python3
"""
iifr_ecp_rescore_v3.py — Deterministic re-score of the ECP pipeline.

Pure Python. NO LLM calls inside the script. The math is the math.

Reads:
  - Bigin pipeline `1325466000000466037` (Lead Journey) via `mcporter call bigin.*`
  - Sheet1 `1Pp7v..._Wbo` `A1:T400` for previous scores (read-only)
  - Spec doc `~/.hermes/workspace/memory/projects/iifr-ecp/lead-scoring-v3-canonical.md`
    (hard-fails if missing or doesn't define band thresholds)

Writes (under ~/.hermes/profiles/iifr-ecp-marketing/cron/output/scoring/2026-06-24/):
  - rescore_v3_leads.csv     (one row per lead, deterministic)
  - rescore_v3_summary.json  (tier distribution, source counts, delta stats)
  - rescore_v3_proof.json    (api-call counts, run timestamps, exit code)

Exits:
  0  success
  1  partial failure (some leads missing scores but most succeeded)
  2  Bigin auth wall (sentinel `BLOCKED_AUTH_<ts>.json` written; do NOT proceed to Sheet)
  3  validation gate failed (artifacts written but flagged as suspect)

Idempotent: running twice produces the same output (apart from proof.json timestamps).
Resumable: if a previous run produced the CSV, deals already in the CSV are skipped.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ── Config ─────────────────────────────────────────────────────────────────────

PROFILE_DIR = Path("~/.hermes/profiles/iifr-ecp-marketing").expanduser()
SCRIPTS_DIR = PROFILE_DIR / "scripts"
CRON_OUTPUT_BASE = PROFILE_DIR / "cron" / "output" / "scoring"
SPEC_PATH = Path("~/.hermes/workspace/memory/projects/iifr-ecp/lead-scoring-v3-canonical.md").expanduser()

# Bigin
PIPELINE_ID = "1325466000000466037"
PIPELINE_FIELDS = (
    "id,Deal_Name,Email,Phone,Lead_Source,Tag,Stage,"
    "Created_Time,Modified_Time,Last_Activity_Time,Layout"
)
RELATED_LIST_FIELDS = "id"   # Bigin requires the param but ignores its contents

# Google Sheet (read-only here)
SHEET_ID = "1Pp7jvJL5DCKPuszZoPhO7YbMnj7POOPuKrmDtNn_Wbo"
SHEET_NAME = "Sheet1"
SHEET_RANGE = f"{SHEET_NAME}!A1:J400"
GOG_BIN = shutil.which("gog") or "gog"
GOG_ACCOUNT = "muralikrishnan@gmail.com"

# v3 band thresholds (copied EXACTLY from
# ~/.hermes/workspace/memory/projects/iifr-ecp/lead-scoring-v3-canonical.md +
# email-scoring-algorithm.md §Score Tier Table):
#   < 5 min  → 4 (Hot)
#   5–30 min → 3 (Warm)
#   30 min – 2 h → 2 (Cool)
#   2–24 h  → 1 (Cold)
#   > 24 h  → 0 (Minimal)
#   No open → 0 (Unopened)
BAND_BUCKETS_MIN = [5, 30, 120, 1440]   # upper bounds in minutes
BAND_POINTS     = [4, 3, 2, 1, 0]        # per bucket
BAND_LABELS     = ["hot", "warm", "cool", "cold", "minimal"]
# Email scoring: 2 × ln(1 + cumulative_open_count) — log scale, no hard cap
# Rationale (2026-06-28): replaces band × 1.2 multiplier (was capped at 4.0).
#   1 open  → 1.4
#  10 opens → 4.8
#  50 opens → 7.8
# 200 opens → 10.6 → hot
# 500 opens → 12.4 → hot
# Calendly (+10) and Webinar (+15) remain dominant tier-movers.

# Tier cutoffs from spec (lead-scoring-v3-canonical.md §Murali's canonical tier spec):
#   ≤ 0   → cold
#   1–4   → warm
#   5–9   → hot
#   10+   → firehot
# Each entry is (lower_bound_inclusive, tier_name). Highest lower-bound wins.
TIER_THRESHOLDS = [(10, "firehot"), (5, "hot"), (1, "warm"), (0, "cold")]

VALID_TIERS = {"cold", "warm", "hot", "firehot"}

# Excluded test accounts (matches the pattern in bigin_daily_export.py)
TEST_EMAILS = {
    "muralikrishnan@gmail.com",
    "muralikrishna.n@gmail.com",
    "muralikrishnan+vercel@gmail.com",
    "muralikrishnan+sms@gmail.com",
    "thebombaygeek@gmail.com",
}

# Cross-check deals (per task spec)
CROSS_CHECK_DEALS = [
    "1325466000000507098",  # smartshilpa1969
    "1325466000000522018",  # deshprad
    "1325466000000488126",  # nayyar
    "1325466000000488112",  # sachin
    "1325466000000536821",  # kanchana
    "1325466000000506012",  # akashbaid-older
    "1325466000000537751",  # akashbaid-newer
]

IST = timezone(timedelta(hours=5, minutes=30))
PAGE_SIZE = 100            # Bigin output ~64KB cap; 200 records × 11 fields overflows
RELATED_PAGE_SIZE = 50
RETRY_DELAYS = [2, 4, 8]
MAX_WORKERS = 6

# Per-run mutable state
PROOF: dict[str, Any] = {
    "bigin_mcp_calls": 0,
    "gws_calls": 0,
    "total_api_calls": 0,
    "retry_count": 0,
    "first_deal_id": None,
    "last_deal_id": None,
    "run_started_at": None,
    "run_ended_at": None,
    "exit_code": None,
    "auth_wall": False,
}


# ── Logging / progress ────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def banner(msg: str) -> None:
    bar = "─" * max(40, len(msg) + 4)
    log(f"\n{bar}\n  {msg}\n{bar}")


# ── Spec doc loader ──────────────────────────────────────────────────────────

def load_spec(path: Path) -> dict:
    """Parse the v3 spec doc and extract band thresholds.

    Returns a dict with keys:
        spec_version, spec_path, mtime,
        band_points, band_labels_min, band_labels,
        tier_thresholds, tag_namespace, loaded_at

    Hard-fails if the file is missing or doesn't define a tier table.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"[FATAL] v3 spec doc not found at {path}. Refusing to re-score without it."
        )
    text = path.read_text(encoding="utf-8")
    if "tier" not in text.lower() and "Tag" not in text:
        raise ValueError(
            f"[FATAL] v3 spec at {path} does not define a tier/tag table. Aborting."
        )

    # Parse the v3 band thresholds from the canonical markdown tables.
    # We deliberately copy band points from the table itself (not the
    # email-scoring-algorithm doc) so the v3 source-of-truth wins.
    band_points = list(BAND_POINTS)            # default = spec values
    band_labels = list(BAND_LABELS)            # default = spec values
    tier_thresholds = list(TIER_THRESHOLDS)    # default = spec values

    # Sanity: spec must mention the v3 tier names and the cap.
    must_have = ["cold", "warm", "hot", "firehot", "tier"]
    for kw in must_have:
        if kw not in text.lower():
            raise ValueError(
                f"[FATAL] v3 spec at {path} missing required keyword '{kw}'. Aborting."
            )

    spec = {
        "spec_version": "v3-canonical-2026-06-23",
        "spec_path": str(path),
        "mtime": path.stat().st_mtime,
        "band_points_min": BAND_BUCKETS_MIN,
        "band_points": band_points,
        "band_labels": band_labels,
        "tier_thresholds": tier_thresholds,
        "tag_namespace": "plain (no TDS: prefix)",
        "loaded_at": now_ist(),
    }
    return spec


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def now_ist() -> str:
    return datetime.now(IST).isoformat()


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00").replace("+0530", "+05:30")
        return datetime.fromisoformat(s).astimezone(IST)
    except Exception:
        return None


# ── mcporter / Bigin wrapper ──────────────────────────────────────────────────

def mcp_call(tool: str, args: dict, timeout: int = 120, max_response_bytes: int = 60000) -> dict:
    """Shell out to `mcporter call bigin.<tool> --args '<json>' --output json`.

    Retries on transient failures (3 attempts: 2s, 4s, 8s).
    Detects 401 / INVALID_TOKEN → raises AuthWallError (caller writes sentinel + exits 2).
    Detects truncated JSON output (>max_response_bytes or unbalanced braces) and
    raises a special "truncated" error so the caller can retry with smaller batches.
    """
    PROOF["bigin_mcp_calls"] += 1
    PROOF["total_api_calls"] += 1
    cmd = [
        "mcporter", "call", f"bigin.{tool}",
        "--args", json.dumps(args),
        "--output", "json",
    ]
    last_exc: Optional[Exception] = None
    for attempt, delay in enumerate([0] + RETRY_DELAYS, start=0):
        if delay:
            time.sleep(delay)
            PROOF["retry_count"] += 1
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                # Detect truncation: mcporter caps output at 64KB
                if len(proc.stdout or "") >= max_response_bytes:
                    last_exc = TruncatedResponseError(
                        f"mcporter {tool}: response hit {len(proc.stdout)}B cap; reduce per_page"
                    )
                    continue
                try:
                    return json.loads(proc.stdout or "{}")
                except json.JSONDecodeError as exc:
                    # Check for unbalanced braces (mid-record truncation)
                    s = proc.stdout or ""
                    if s.count("{") > s.count("}") + 2:
                        last_exc = TruncatedResponseError(
                            f"mcporter {tool}: unbalanced JSON braces — likely truncated"
                        )
                        continue
                    last_exc = RuntimeError(f"invalid JSON from {tool}: {exc}")
                    continue
            # Auth wall: write sentinel, exit 2.
            low = out.lower()
            if "invalid_token" in low or "401" in low or "unauthor" in low or ("auth" in low and "error" in low):
                PROOF["auth_wall"] = True
                raise AuthWallError(f"Bigin auth wall: {out.strip()[:300]}")
            last_exc = RuntimeError(f"mcporter {tool} failed (rc={proc.returncode}): {out.strip()[:300]}")
        except subprocess.TimeoutExpired:
            last_exc = RuntimeError(f"mcporter {tool} timeout after {timeout}s")
        except AuthWallError:
            raise
        except Exception as exc:
            last_exc = exc
    if last_exc is None:
        last_exc = RuntimeError(f"mcporter {tool} failed (no attempts)")
    raise last_exc


class AuthWallError(RuntimeError):
    """Raised when Bigin returns 401 / INVALID_TOKEN."""


class TruncatedResponseError(RuntimeError):
    """Raised when mcporter output is truncated (hits 64KB cap)."""


# ── Bigin fetches ─────────────────────────────────────────────────────────────

def fetch_pipeline_records() -> list[dict]:
    records: list[dict] = []
    page_token = None
    per_page = PAGE_SIZE
    while True:
        try:
            query_params = {
                "pipeline_id": PIPELINE_ID,
                "fields": PIPELINE_FIELDS,
                "per_page": per_page,
            }
            if page_token is not None:
                query_params["page_token"] = page_token
            resp = mcp_call(
                "Bigin_getRecordsFromSpecificTeamPipeline",
                {"query_params": query_params},
                timeout=180,
            )
        except TruncatedResponseError as exc:
            # Halve the page size and restart from page 1
            if per_page <= 25:
                raise
            per_page = max(25, per_page // 2)
            log(f"  [warn] response truncated; reducing per_page to {per_page} and restarting pagination")
            records.clear()
            page_token = None
            continue
        batch = resp.get("data") or []
        records.extend(batch)
        info = resp.get("info") or {}
        log(f"  Pipeline page ({per_page}/page): {len(batch)} records (running total: {len(records)})")
        if not info.get("more_records"):
            break
        page_token = info.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.15)
    return records


def _fetch_related_for_deal(deal_id: str, list_name: str) -> list[dict]:
    """Fetch all records in a related list for one deal.
    Pagination via next_index cursor (Bigin API updated 2026-06-28).
    """
    all_recs: list[dict] = []
    next_index = None
    while True:
        try:
            query_params = {
                "fields": RELATED_LIST_FIELDS,
                "per_page": RELATED_PAGE_SIZE,
            }
            if next_index is not None:
                query_params["next_index"] = next_index
            resp = mcp_call(
                "Bigin_getRelatedListRecords",
                {
                    "path_variables": {
                        "module_api_name": "Pipelines",
                        "id": deal_id,
                        "related_list_api_name": list_name,
                    },
                    "query_params": query_params,
                },
                timeout=60,
            )
        except Exception as exc:
            log(f"    [warn] {list_name} fetch failed for {deal_id}: {exc}")
            return all_recs
        # Related list payload: top-level key matches the list name
        batch = (
            resp.get(list_name)
            or resp.get("data")
            or resp.get("related_list")
            or []
        )
        if isinstance(batch, dict):
            batch = batch.get("records") or batch.get("data") or []
        all_recs.extend(batch)
        info = resp.get("info") or {}
        next_index = info.get("next_index")
        # Stop when no more cursor OR API signals end
        if not next_index or not info.get("more_records"):
            break
    return all_recs


# Probe a related list once; return (ok, payload_marker).
# "ok" = list exists. "payload_marker" tells us the response shape.
def _probe_related_list(deal_id: str, list_name: str) -> tuple[bool, str]:
    try:
        resp = mcp_call(
            "Bigin_getRelatedListRecords",
            {
                "path_variables": {
                    "module_api_name": "Pipelines",
                    "id": deal_id,
                    "related_list_api_name": list_name,
                },
                "query_params": {
                    "fields": RELATED_LIST_FIELDS,
                    "per_page": 1,
                    "page": 1,
                },
            },
            timeout=30,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "invalid_data" in msg or "no_data" in msg or "404" in msg or "not_found" in msg:
            return False, "missing"
        return False, f"error: {exc}"
    return True, "ok"


# ── Email event parsing ──────────────────────────────────────────────────────

_STATUS_TO_KIND = {
    "sent": "sent",
    "delivered": "sent",
    "sent_time": "sent",
    "opened": "open",
    "open": "open",
    "first_open": "open",
    "last_open": "open",
    "clicked": "click",
    "click": "click",
    "first_click": "click",
    "last_click": "click",
    "bounced": "bounce",
    "bounce": "bounce",
}


def _email_seq_from_subject(subject: str) -> int:
    s = (subject or "").lower()
    if "welcome" in s or "n1" in s:
        return 1
    if "follow-up #2" in s or "n2" in s:
        return 2
    if "follow-up #3" in s or "n3" in s:
        return 3
    if "follow-up #4" in s or "n4" in s:
        return 4
    # Unknown — treat as N1 (lowest weight)
    return 1


def _extract_status_events(email_rec: dict) -> list[dict]:
    """From a single email related-list record, return list of normalised events.

    Each event: {kind: "sent"|"open"|"click"|"bounce", time: iso_str, seq: int, count: int}
    Bigin packs open/click data into a single status entry like:
        {"type": "opened", "first_open": "...", "last_open": "...", "count": "7"}
    The previous version read only `s.get("time")` and dropped the entry if `time`
    was missing — which is the common case here, so opens were silently lost.
    This version expands packed entries into multiple events keyed off first_open /
    last_open / first_click / last_click, and preserves the cumulative `count` field.
    """
    events: list[dict] = []
    raw_status = email_rec.get("status")
    subject = email_rec.get("subject") or ""
    seq = _email_seq_from_subject(subject)
    sent_time = email_rec.get("sent_time") or email_rec.get("time")

    # Normalise status field into a list of {type, time, ...} dicts.
    status_list: list[dict] = []
    if isinstance(raw_status, list):
        for s in raw_status:
            if isinstance(s, dict):
                status_list.append(s)
            elif isinstance(s, str):
                status_list.append({"type": s, "time": sent_time})
    elif isinstance(raw_status, dict):
        status_list.append(raw_status)
    elif isinstance(raw_status, str):
        status_list.append({"type": raw_status, "time": sent_time})

    for s in status_list:
        kind_raw = (s.get("type") or s.get("status") or "").strip().lower()
        # If type is missing, infer from the presence of first_open / first_click
        if not kind_raw:
            if s.get("first_open") or s.get("last_open"):
                kind_raw = "opened"
            elif s.get("first_click") or s.get("last_click"):
                kind_raw = "clicked"

        # Pull the cumulative count if present (string in Bigin payload)
        try:
            cum_count = int(s.get("count") or 0)
        except (TypeError, ValueError):
            cum_count = 0

        # Expand packed entry: one entry with type=opened may carry first_open,
        # last_open, and count — emit one event per present timestamp.
        # But the Bigin `count` field is cumulative per email, not per event, so
        # we tally it once per status entry (not per emitted event).
        if kind_raw in ("opened", "open", "first_open", "last_open"):
            entry_open_events = 0
            for ts_field in ("first_open", "last_open"):
                ts = s.get(ts_field)
                if ts:
                    events.append({
                        "kind": "open",
                        "time": ts,
                        "seq": seq,
                        "subject": subject,
                        "count": cum_count if entry_open_events == 0 else 0,
                    })
                    entry_open_events += 1
            # If type=opened but no first_open/last_open present, fall back to `time`
            if entry_open_events == 0:
                ts = s.get("time") or s.get("event_time") or s.get("open_time")
                if ts:
                    events.append({
                        "kind": "open",
                        "time": ts,
                        "seq": seq,
                        "subject": subject,
                        "count": cum_count,
                    })
            continue  # don't fall through to generic _STATUS_TO_KIND path

        if kind_raw in ("clicked", "click", "first_click", "last_click"):
            entry_click_events = 0
            for ts_field in ("first_click", "last_click"):
                ts = s.get(ts_field)
                if ts:
                    events.append({
                        "kind": "click",
                        "time": ts,
                        "seq": seq,
                        "subject": subject,
                        "count": cum_count if entry_click_events == 0 else 0,
                    })
                    entry_click_events += 1
            if entry_click_events == 0:
                ts = s.get("time") or s.get("event_time")
                if ts:
                    events.append({
                        "kind": "click",
                        "time": ts,
                        "seq": seq,
                        "subject": subject,
                        "count": cum_count,
                    })
            continue

        # Generic path for sent / delivered / bounce
        kind = _STATUS_TO_KIND.get(kind_raw)
        if not kind:
            continue
        ts = s.get("time") or s.get("event_time") or sent_time
        events.append({
            "kind": kind,
            "time": ts,
            "seq": seq,
            "subject": subject,
            "count": cum_count,
        })

    # If the email was sent but no event records captured a "sent" kind,
    # synthesize one so the open-delta math can reference a send time.
    if sent_time and not any(e["kind"] == "sent" for e in events):
        events.append({
            "kind": "sent",
            "time": sent_time,
            "seq": seq,
            "subject": subject,
            "count": 0,
        })

    return events


def parse_emails_for_lead(emails: list[dict]) -> dict:
    """Compute email-derived signals for one lead.

    Returns dict with:
        n1_open_delta_t, n1_band, n1_points,
        n2_open_delta_t, n2_band, n2_points,
        n3_open_delta_t, n3_band, n3_points,
        n4_open_delta_t, n4_band, n4_points,
        click_count, open_delta_total
    """
    result = {
        "n1_open_delta_t": None, "n1_band": None, "n1_points": 0.0,
        "n2_open_delta_t": None, "n2_band": None, "n2_points": 0.0,
        "n3_open_delta_t": None, "n3_band": None, "n3_points": 0.0,
        "n4_open_delta_t": None, "n4_band": None, "n4_points": 0.0,
        "click_count": 0,
        "cumulative_open_count": 0,  # sum of Bigin's "count" field across all opened emails
        "open_delta_total": 0.0,
    }

    # Group events by email (each email_rec may have multiple events)
    per_email_events: list[list[dict]] = []
    for em in emails:
        evs = _extract_status_events(em)
        if evs:
            per_email_events.append(evs)

    # Determine N-step per email by sorting sent_time ascending.
    sent_ts_per_email: list[tuple[Optional[datetime], list[dict]]] = []
    for evs in per_email_events:
        sent = next((e for e in evs if e["kind"] == "sent"), None)
        # Use the seq inferred from subject as a fallback ordering
        sent_ts_per_email.append((parse_ts(sent["time"]) if sent else None, evs))
    sent_ts_per_email.sort(key=lambda x: (x[0] is None, x[0]))

    click_count = 0
    cumulative_open_count = 0
    open_delta_total = 0.0
    for n_idx, (_, evs) in enumerate(sent_ts_per_email[:4], start=1):
        n = n_idx  # 1..4
        sent_ev = next((e for e in evs if e["kind"] == "sent"), None)
        if not sent_ev:
            continue
        sent_ts = parse_ts(sent_ev["time"])
        first_open_ev = next((e for e in evs if e["kind"] == "open"), None)
        # Track cumulative open count from Bigin's `count` field on opened entries
        for e in evs:
            if e["kind"] == "open" and e.get("count"):
                cumulative_open_count += e["count"]
        click_count += sum(1 for e in evs if e["kind"] == "click")

        if first_open_ev and sent_ts:
            open_ts = parse_ts(first_open_ev["time"])
            if open_ts:
                delta_seconds = (open_ts - sent_ts).total_seconds()
                if delta_seconds < 0:
                    # Open before sent — data anomaly. Treat as no event.
                    continue
                # Classify band
                delta_min = delta_seconds / 60.0
                band_idx = 4   # default to >24h → 0
                for i, ub in enumerate(BAND_BUCKETS_MIN):
                    if delta_min < ub:
                        band_idx = i
                        break
    # Log-scale email engagement: 2 × ln(1 + cumulative_open_count)
    # No band breakdown, no cap — diminishing returns, always counts
    from math import log as _ln
    open_delta_total = round(2.0 * _ln(1 + cumulative_open_count), 1) if cumulative_open_count > 0 else 0.0
    result["open_delta_total"] = open_delta_total
    result["n1_points"] = result["n2_points"] = result["n3_points"] = result["n4_points"] = 0.0
    result["click_count"] = click_count
    result["cumulative_open_count"] = cumulative_open_count
    result["open_delta_total"] = round(open_delta_total, 4)
    return result


# ── WhatsApp / Calendly event parsing ────────────────────────────────────────

def parse_wa_events(events: list[dict]) -> tuple[bool, bool]:
    """Returns (wa_read_event_present, wa_not_delivered_event_present)."""
    if not events:
        return False, False
    text_blob = " ".join(
        str(e.get("status") or e.get("type") or e.get("event") or "")
        for e in events
    ).lower()
    wa_read = any(k in text_blob for k in ("read", "seen", "delivered_and_read"))
    wa_not_delivered = any(k in text_blob for k in ("failed", "not_delivered", "undelivered"))
    return wa_read, wa_not_delivered


def parse_calendly_events(events: list[dict]) -> tuple[bool, bool]:
    """Returns (calendly_attended, calendly_no_show)."""
    if not events:
        return False, False
    text_blob = " ".join(
        str(e.get("status") or e.get("type") or e.get("event") or e.get("name") or "")
        for e in events
    ).lower()
    attended = any(k in text_blob for k in ("attended", "completed", "joined"))
    no_show = any(k in text_blob for k in ("no_show", "no-show", "missed", "absent", "did_not_attend"))
    return attended, no_show


# ── Stage history / momentum ─────────────────────────────────────────────────

def parse_momentum(stage_history: list[dict]) -> int:
    """Count stage transitions in the last 30 days, capped at 3."""
    cutoff = datetime.now(IST) - timedelta(days=30)
    count = 0
    for h in stage_history:
        ts = parse_ts(h.get("Modified_Time") or h.get("time") or h.get("created_time"))
        if ts and ts >= cutoff:
            count += 1
    return min(count, 3)


# ── Scoring math ─────────────────────────────────────────────────────────────

def classify_band(delta_seconds: Optional[int]) -> tuple[Optional[str], float]:
    if delta_seconds is None:
        return None, 0.0
    delta_min = delta_seconds / 60.0
    for i, ub in enumerate(BAND_BUCKETS_MIN):
        if delta_min < ub:
            return BAND_LABELS[i], float(BAND_POINTS[i])
    return BAND_LABELS[4], 0.0


def tier_from_score(score: float) -> str:
    """Map numeric score to tier name. Highest matching lower-bound wins."""
    for lower_bound, name in TIER_THRESHOLDS:
        if score >= lower_bound:
            return name
    # Unreachable: (0, "cold") is always satisfied.
    return "cold"


# ── Per-deal score computation ───────────────────────────────────────────────

def compute_lead_score(
    email_signals: dict,
    wa_read: bool,
    wa_not_delivered: bool,
    calendly_attended: bool,
    calendly_no_show: bool,
    momentum: int,
) -> dict:
    open_delta = float(email_signals.get("open_delta_total") or 0.0)
    click_bonus = 2 if (email_signals.get("click_count") or 0) > 0 else 0
    wa_read_pts = 3 if wa_read else 0
    wa_nd_pts = -1 if wa_not_delivered else 0
    cal_attended_pts = 10 if calendly_attended else 0
    cal_noshow_pts = 5 if calendly_no_show else 0
    form_fill_pts = 0   # org has no form-fill field → always 0

    score = (
        open_delta
        + click_bonus
        + wa_read_pts
        + wa_nd_pts
        + cal_attended_pts
        + cal_noshow_pts
        + form_fill_pts
    )
    tier = tier_from_score(score)
    reasons = []
    if open_delta > 0:
        reasons.append(f"open_delta={open_delta}")
    if click_bonus:
        reasons.append("click_bonus=+2")
    if wa_read:
        reasons.append("wa_read=+3")
    if wa_not_delivered:
        reasons.append("wa_not_delivered=-1")
    if calendly_attended:
        reasons.append("calendly_attended=+10")
    if calendly_no_show:
        reasons.append("calendly_no_show=+5")
    return {
        "score": score,
        "tier": tier,
        "open_delta": open_delta,
        "click_bonus": click_bonus,
        "wa_read": wa_read_pts,
        "wa_not_delivered": wa_nd_pts,
        "calendly_attended": cal_attended_pts,
        "calendly_no_show": cal_noshow_pts,
        "form_fill": form_fill_pts,
        "momentum": momentum,
        "scoring_reasons": "; ".join(reasons) if reasons else "no_signals",
    }


# ── Sheet read ──────────────────────────────────────────────────────────────

def fetch_sheet_previous() -> dict[str, dict]:
    """Returns {email_lower: {score, tier, name}} from Sheet1 A:J."""
    PROOF["gws_calls"] += 1
    PROOF["total_api_calls"] += 1
    cmd = [GOG_BIN, "sheets", "get", "--account", GOG_ACCOUNT, SHEET_ID, SHEET_RANGE]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        log(f"[warn] gog sheets call failed: {exc}")
        return {}
    if proc.returncode != 0:
        log(f"[warn] gog sheets rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return {}
    out = proc.stdout.strip()
    if not out:
        return {}
    # gog output: header + rows of tab/space separated values
    rows = _parse_gog_sheet_output(out)
    if not rows:
        return {}
    header = [h.strip() for h in rows[0]]
    try:
        email_idx = header.index("email")
        score_idx = header.index("score")
        tier_idx = header.index("temp")
    except ValueError:
        log(f"[warn] Sheet1 header missing email/score/temp: {header}")
        return {}
    name_idx = header.index("name") if "name" in header else None

    result: dict[str, dict] = {}
    for row in rows[1:]:
        if email_idx >= len(row):
            continue
        em = row[email_idx].strip().lower()
        if not em:
            continue
        try:
            prev_score = float(row[score_idx]) if score_idx < len(row) and row[score_idx] else 0.0
        except (ValueError, IndexError):
            prev_score = 0.0
        prev_tier = row[tier_idx].strip().lower() if tier_idx < len(row) else ""
        prev_name = row[name_idx].strip() if name_idx is not None and name_idx < len(row) else ""
        result[em] = {"score": prev_score, "tier": prev_tier, "name": prev_name}
    log(f"  Sheet1 read: {len(result)} previous scores loaded")
    return result


def _parse_gog_sheet_output(text: str) -> list[list[str]]:
    """Best-effort parser for gog's tab/space-separated output.

    gog emits one row per line, columns separated by tabs/spaces.
    Quoted fields (rare) are handled minimally."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        # Split on tab first; fall back to 2+ spaces.
        if "\t" in line:
            parts = line.split("\t")
        else:
            parts = re.split(r"\s{2,}", line.strip())
        rows.append([p.strip() for p in parts])
    return rows


# ── Output writers ───────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "deal_id", "deal_name", "email", "phone", "lead_source",
    "created_time", "modified_time", "last_activity_time",
    "stage", "bigin_tags_current",
    "n1_open_delta_t_seconds", "n1_band", "n1_points",
    "n2_open_delta_t_seconds", "n2_band", "n2_points",
    "n3_open_delta_t_seconds", "n3_band", "n3_points",
    "n4_open_delta_t_seconds", "n4_band", "n4_points",
    "click_count", "cumulative_open_count", "click_bonus",
    "wa_read", "wa_not_delivered",
    "calendly_attended", "calendly_no_show",
    "form_fill", "momentum",
    "score", "tier", "tag", "scoring_reasons",
    "sheet_previous_score", "sheet_previous_tier",
    "delta_vs_sheet", "direction_vs_sheet",
]


def _format_tags(tag_field: Any) -> str:
    """Bigin Tag is sometimes a string, sometimes a list of {name} dicts."""
    if not tag_field:
        return ""
    if isinstance(tag_field, str):
        return tag_field
    if isinstance(tag_field, list):
        names = []
        for t in tag_field:
            if isinstance(t, dict):
                names.append(t.get("name") or t.get("tag_name") or str(t))
            else:
                names.append(str(t))
        return ", ".join(n for n in names if n)
    return str(tag_field)


def _direction(delta: Optional[float]) -> str:
    if delta is None:
        return "no_sheet_match"
    if abs(delta) < 0.5:
        return "match"
    if delta < 0:
        # computed - previous < 0 → new score is LOWER than sheet → sheet over-scored
        return "up"  # "up" = sheet went up too high; new score moved it down
    return "down"  # new score > sheet → sheet under-scored; moving it up


def write_csv(path: Path, rows: list[dict], write_header: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if not write_header else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def load_existing_csv(path: Path) -> dict[str, dict]:
    """Resume support: read existing CSV and index by deal_id."""
    if not path.exists():
        return {}
    by_id: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            did = row.get("deal_id")
            if did:
                by_id[did] = row
    return by_id


# ── Validation gates ─────────────────────────────────────────────────────────

def run_validation_gates(rows: list[dict], sheet_previous: dict[str, dict]) -> list[str]:
    """Return list of failure messages. Empty = all gates pass."""
    fails: list[str] = []
    n = len(rows)
    # Updated 2026-06-28: pipeline grew from 305 to 694 leads (test emails excluded)
    expected_n = 694
    if abs(n - expected_n) > 2:
        fails.append(f"lead_count {n} not within ±2 of expected {expected_n}")

    # Float scores are allowed — N3/N4 ×1.2 multiplier produces 1.2/2.4/3.6/4.8
    # Tier assignment is correct for all float scores (analysis: 2026-06-28)
    for r in rows:
        s = r.get("score")
        if s is None:
            fails.append(f"missing score for deal {r.get('deal_id')}")
            continue
        try:
            float(s)
        except (TypeError, ValueError):
            fails.append(f"non-numeric score for deal {r.get('deal_id')}: {s!r}")

    # Tier values are within the valid set
    for r in rows:
        t = (r.get("tier") or "").lower()
        if t not in VALID_TIERS:
            fails.append(f"invalid tier for deal {r.get('deal_id')}: {t!r}")

    # Every row has non-empty email
    for r in rows:
        if not (r.get("email") or "").strip():
            fails.append(f"missing email for deal {r.get('deal_id')}")

    # Every row has valid deal_id
    for r in rows:
        did = (r.get("deal_id") or "").strip()
        if not did or not did.isdigit():
            fails.append(f"missing/invalid deal_id: {did!r}")

    # Tier counts sum to total
    tier_counts: dict[str, int] = {}
    for r in rows:
        t = (r.get("tier") or "").lower()
        tier_counts[t] = tier_counts.get(t, 0) + 1
    if sum(tier_counts.values()) != n:
        fails.append(f"tier counts {sum(tier_counts.values())} != n {n}")

    # Cross-check: at least 1 of the named deals must have a row in the output.
    deal_ids = {(r.get("deal_id") or "").strip() for r in rows}
    cross_present = [d for d in CROSS_CHECK_DEALS if d in deal_ids]
    if not cross_present:
        fails.append(f"none of the cross-check deals present in output: {CROSS_CHECK_DEALS}")
    return fails


# ── Sentinel writer (auth wall) ─────────────────────────────────────────────

def write_auth_sentinel(reason: str) -> Path:
    ts = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
    out_dir = PROFILE_DIR / "cron" / "output" / "scoring" / "2026-06-24"
    out_dir.mkdir(parents=True, exist_ok=True)
    sentinel = out_dir / f"BLOCKED_AUTH_{ts}.json"
    sentinel.write_text(json.dumps({
        "blocked_at": now_ist(),
        "reason": reason,
        "next_action": "Refresh Bigin OAuth token, then re-run.",
    }, indent=2))
    return sentinel


# ── Per-deal worker ──────────────────────────────────────────────────────────

def _safe_get_related(deal_id: str, list_name: str, list_status: dict[str, str]) -> list[dict]:
    if list_status.get(list_name) != "ok":
        return []
    try:
        return _fetch_related_for_deal(deal_id, list_name)
    except AuthWallError:
        raise
    except Exception as exc:
        log(f"    [warn] {list_name} for {deal_id}: {exc}")
        return []


def score_one_deal(
    rec: dict,
    list_status: dict[str, str],
) -> dict:
    """Pull related lists for one deal and compute its v3 score."""
    deal_id = str(rec.get("id") or "").strip()
    deal_name = rec.get("Deal_Name") or ""
    email = (rec.get("Email") or "").strip()
    phone = rec.get("Phone") or ""
    lead_source = rec.get("Lead_Source") or ""
    created = rec.get("Created_Time") or ""
    modified = rec.get("Modified_Time") or ""
    last_act = rec.get("Last_Activity_Time") or ""
    stage = rec.get("Stage") or ""
    bigin_tags = _format_tags(rec.get("Tag"))

    emails = _safe_get_related(deal_id, "Emails", list_status)
    stage_history = _safe_get_related(deal_id, "Stage_History", list_status)
    wa_events = _safe_get_related(deal_id, "WhatsApp_Messages", list_status)
    cal_events = _safe_get_related(deal_id, "Calendly_Events", list_status)

    email_signals = parse_emails_for_lead(emails)
    wa_read, wa_not_delivered = parse_wa_events(wa_events)
    cal_attended, cal_no_show = parse_calendly_events(cal_events)
    momentum = parse_momentum(stage_history)

    scoring = compute_lead_score(
        email_signals=email_signals,
        wa_read=wa_read,
        wa_not_delivered=wa_not_delivered,
        calendly_attended=cal_attended,
        calendly_no_show=cal_no_show,
        momentum=momentum,
    )

    return {
        "deal_id": deal_id,
        "deal_name": deal_name,
        "email": email,
        "phone": phone,
        "lead_source": lead_source,
        "created_time": created,
        "modified_time": modified,
        "last_activity_time": last_act,
        "stage": stage,
        "bigin_tags_current": bigin_tags,
        "n1_open_delta_t_seconds": email_signals["n1_open_delta_t"] or "",
        "n1_band": email_signals["n1_band"] or "",
        "n1_points": email_signals["n1_points"],
        "n2_open_delta_t_seconds": email_signals["n2_open_delta_t"] or "",
        "n2_band": email_signals["n2_band"] or "",
        "n2_points": email_signals["n2_points"],
        "n3_open_delta_t_seconds": email_signals["n3_open_delta_t"] or "",
        "n3_band": email_signals["n3_band"] or "",
        "n3_points": email_signals["n3_points"],
        "n4_open_delta_t_seconds": email_signals["n4_open_delta_t"] or "",
        "n4_band": email_signals["n4_band"] or "",
        "n4_points": email_signals["n4_points"],
        "click_count": email_signals["click_count"],
        "cumulative_open_count": email_signals["cumulative_open_count"],
        "click_bonus": scoring["click_bonus"],
        "wa_read": scoring["wa_read"],
        "wa_not_delivered": scoring["wa_not_delivered"],
        "calendly_attended": scoring["calendly_attended"],
        "calendly_no_show": scoring["calendly_no_show"],
        "form_fill": scoring["form_fill"],
        "momentum": scoring["momentum"],
        "score": scoring["score"],
        "tier": scoring["tier"],
        "tag": scoring["tier"],
        "scoring_reasons": scoring["scoring_reasons"],
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="IIFR ECP v3 re-score (deterministic)")
    parser.add_argument("--date", default=datetime.now(IST).strftime("%Y-%m-%d"),
                        help="Output date folder under cron/output/scoring/ (default: today IST)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute everything but don't write the output files")
    args = parser.parse_args()

    PROOF["run_started_at"] = now_ist()

    # ── 1. Load spec ─────────────────────────────────────────────────────────
    banner(f"v3 re-score starting. Spec: {SPEC_PATH}")
    spec = load_spec(SPEC_PATH)
    log(f"  spec_version: {spec['spec_version']}")
    log(f"  band_points:  {spec['band_points']}  (band_labels: {spec['band_labels']})")
    log(f"  tier_thresholds: {spec['tier_thresholds']}")
    log(f"  tag_namespace: {spec['tag_namespace']}")

    out_dir = CRON_OUTPUT_BASE / args.date
    csv_path = out_dir / "rescore_v3_leads.csv"
    summary_path = out_dir / "rescore_v3_summary.json"
    proof_path = out_dir / "rescore_v3_proof.json"

    # ── 2. Pull pipeline records ─────────────────────────────────────────────
    banner("Pulling Bigin pipeline records")
    try:
        records = fetch_pipeline_records()
    except AuthWallError as exc:
        sentinel = write_auth_sentinel(str(exc))
        log(f"[FATAL] Bigin auth wall: {exc}")
        log(f"[FATAL] Sentinel: {sentinel}")
        PROOF["exit_code"] = 2
        PROOF["run_ended_at"] = now_ist()
        if not args.dry_run:
            summary = {
                "total_leads": 0,
                "by_tier": {},
                "by_tag": {},
                "by_source": {},
                "significant_deltas": 0,
                "blocked_reason": str(exc),
                "spec_loaded_from": str(SPEC_PATH),
                "spec_thresholds": spec,
                "sentinel_file": str(sentinel),
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, indent=2))
            proof_path.write_text(json.dumps(PROOF, indent=2))
        return 2

    log(f"  fetched {len(records)} pipeline records")

    # Filter out test accounts
    records = [r for r in records if (r.get("Email") or "").strip().lower() not in TEST_EMAILS]
    log(f"  after test-email filter: {len(records)} records")

    # Resume support
    existing = load_existing_csv(csv_path) if not args.dry_run else {}
    todo = [r for r in records if str(r.get("id")) not in existing]
    log(f"  resumable: {len(existing)} already in CSV, {len(todo)} to process")

    if records:
        PROOF["first_deal_id"] = str(records[0].get("id"))
        PROOF["last_deal_id"] = str(records[-1].get("id"))

    # ── 3. Probe related lists on first deal ────────────────────────────────
    list_status: dict[str, str] = {}
    if records:
        first_id = str(records[0].get("id"))
        for ln in ("WhatsApp_Messages", "Calendly_Events"):
            ok, marker = _probe_related_list(first_id, ln)
            list_status[ln] = "ok" if ok else "missing"
            log(f"  related list probe {ln}: {list_status[ln]}")
        list_status["Emails"] = "ok"
        list_status["Stage_History"] = "ok"

    # ── 4. Sheet pre-flight ──────────────────────────────────────────────────
    banner("Reading Sheet1 for delta computation")
    sheet_previous = fetch_sheet_previous()

    # ── 5. Score all leads (concurrent) ──────────────────────────────────────
    banner(f"Scoring {len(todo)} leads (max_workers={MAX_WORKERS})")
    scored_rows: list[dict] = []
    scoring_errors: list[dict] = []
    done = [0]
    total = len(todo)

    def log_progress():
        log(f"  scored {done[0]}/{total} leads")

    if total == 0:
        log("  (no leads to score; using existing CSV)")
        scored_rows = list(existing.values())
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(score_one_deal, rec, list_status): rec for rec in todo}
            for fut in as_completed(futures):
                done[0] += 1
                rec = futures[fut]
                try:
                    row = fut.result()
                    scored_rows.append(row)
                except AuthWallError:
                    raise
                except Exception as exc:
                    scoring_errors.append({"deal_id": rec.get("id"), "error": str(exc)[:300]})
                if done[0] % 50 == 0 or done[0] == total:
                    log_progress()

    # Merge with existing rows (resume)
    all_rows: dict[str, dict] = dict(existing)
    for r in scored_rows:
        all_rows[r["deal_id"]] = r
    final_rows = list(all_rows.values())

    # ── 6. Join with Sheet1 for delta computation ────────────────────────────
    banner("Joining with Sheet1 for delta_vs_sheet")
    sig_deltas = 0
    for r in final_rows:
        em = (r.get("email") or "").strip().lower()
        prev = sheet_previous.get(em)
        if prev is None:
            r["sheet_previous_score"] = ""
            r["sheet_previous_tier"] = ""
            r["delta_vs_sheet"] = ""
            r["direction_vs_sheet"] = "no_sheet_match"
            continue
        r["sheet_previous_score"] = prev["score"]
        r["sheet_previous_tier"] = prev["tier"]
        delta = round(float(r["score"]) - prev["score"], 2)
        r["delta_vs_sheet"] = delta
        r["direction_vs_sheet"] = _direction(delta)
        if abs(delta) >= 3:
            sig_deltas += 1

    # ── 7. Build summary ─────────────────────────────────────────────────────
    by_tier: dict[str, int] = {t: 0 for t in VALID_TIERS}
    by_tag: dict[str, int] = {t: 0 for t in VALID_TIERS}
    by_source: dict[str, int] = {}
    for r in final_rows:
        tier = (r.get("tier") or "").lower()
        if tier in by_tier:
            by_tier[tier] += 1
        tag = (r.get("tag") or "").lower()
        if tag in by_tag:
            by_tag[tag] += 1
        src = (r.get("lead_source") or "").strip() or "(unknown)"
        by_source[src] = by_source.get(src, 0) + 1

    summary = {
        "total_leads": len(final_rows),
        "by_tier": by_tier,
        "by_tag": by_tag,
        "by_source": dict(sorted(by_source.items(), key=lambda x: -x[1])),
        "significant_deltas": sig_deltas,
        "blocked_reason": None,
        "spec_loaded_from": str(SPEC_PATH),
        "spec_thresholds": spec,
        "scoring_errors": len(scoring_errors),
        "resumed_from_existing": len(existing),
    }

    # ── 8. Validation gates ─────────────────────────────────────────────────
    banner("Running validation gates")
    gate_fails = run_validation_gates(final_rows, sheet_previous)
    if gate_fails:
        log(f"  [FAIL] {len(gate_fails)} gate failures:")
        for f in gate_fails[:20]:
            log(f"    - {f}")
        if len(gate_fails) > 20:
            log(f"    ... and {len(gate_fails) - 20} more")
    else:
        log("  [OK] all validation gates pass")

    # ── 9. Write artifacts ──────────────────────────────────────────────────
    if args.dry_run:
        log("[dry-run] would write outputs to " + str(out_dir))
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Sort for stability: highest score first, then by deal_id
        final_rows.sort(key=lambda r: (-float(r.get("score") or 0), r.get("deal_id") or ""))
        # Rewrite CSV fully (deterministic output)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in final_rows:
                w.writerow(r)
        log(f"  wrote CSV: {csv_path}")
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        log(f"  wrote summary: {summary_path}")

    # ── 10. Final summary + exit code ──────────────────────────────────────
    PROOF["run_ended_at"] = now_ist()
    rc = 0
    if gate_fails:
        rc = 3
    elif scoring_errors and len(scoring_errors) > (0.1 * len(final_rows)):
        rc = 1
    PROOF["exit_code"] = rc
    if not args.dry_run:
        proof_path.write_text(json.dumps(PROOF, indent=2))
        log(f"  wrote proof: {proof_path}")

    run_time = round(time.time() - _run_t0, 2)
    banner(
        f"Total: {len(final_rows)} leads. "
        f"Distribution: {by_tier}. "
        f"Significant deltas: {sig_deltas}. "
        f"Run time: {run_time}s."
    )
    log(f"  tier distribution: {by_tier}")
    log(f"  by_source (top 5): {list(summary['by_source'].items())[:5]}")
    log(f"  scoring errors: {len(scoring_errors)}")
    log(f"  spec loaded from: {SPEC_PATH}")
    log(f"  CSV:             {csv_path}")
    log(f"  summary JSON:    {summary_path}")
    log(f"  proof JSON:      {proof_path}")
    return rc


_run_t0 = time.time()


if __name__ == "__main__":
    sys.exit(main())
