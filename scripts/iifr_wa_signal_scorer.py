#!/usr/bin/env python3.11
"""
Fetch WA signals from crm-messaging.cloud and score leads.

Data:   GET https://app.crm-messaging.cloud/index.php/Api/messageHistory
Auth:   Bearer token from scripts/.env → CRM_MESSAGING_API_KEY

Signal rules (from lead-scoring-v3-canonical.md):
  WA read   → +2  (OUTGOING msg with deliveryStatus == "read")  # reduced per Murali 2026-06-28
  WA reply  → +5  (INCOMING msg from lead)  # approved per Murali 2026-06-28
  WA fail   →  0  (held — no penalty; approved per Murali 2026-06-28)

Matching: last-10-digits (Bigin and crm-messaging mask differently — see docs/phone-matching.md)

Usage:
    python3.11 iifr_wa_signal_scorer.py
"""

import csv, json, logging, os, sys, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

API_BASE = "https://app.crm-messaging.cloud/index.php/Api"
ENV_FILE = Path(__file__).parent / ".env"
OUT_DIR  = Path.home() / "Documents/Mac Mini Sync/Lyra sync/iifr-ecp-wa"

WA_READ_PTS  = 2   # was +3, reduced per Murali 2026-06-28
WA_REPLY_PTS = 5   # INCOMING msg from lead — approved per Murali 2026-06-28
WA_FAIL_PTS  = 0   # held at 0 — pending separate decision

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("wa_scorer")


def _load_token() -> str:
    for line in open(ENV_FILE).read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "CRM_MESSAGING_API_KEY":
            return v.strip()
    log.error("CRM_MESSAGING_API_KEY not found in %s", ENV_FILE)
    return ""


def _headers() -> dict:
    return {"Authorization": f"Bearer {_load_token()}", "Accept": "application/json"}


def _fetch_all_messages(max_pages: int = 50) -> list:
    import requests
    all_msgs, page, per_page = [], 1, 500
    total_estimate = None
    while page <= max_pages:
        try:
            r = requests.get(API_BASE + "/messageHistory",
                             headers=_headers(), params={"page": page, "per_page": per_page},
                             timeout=30)
        except Exception as exc:
            log.error("Network error page %d: %s", page, exc)
            break
        if r.status_code == 401:
            log.error("Auth failed (401) — CRM_MESSAGING_API_KEY expired/invalid")
            sys.exit(1)
        if r.status_code != 200:
            log.error("HTTP %d: %s", r.status_code, r.text[:300])
            break
        d = r.json()
        if d.get("status") != "success":
            log.error("API error: %s", d.get("message", d))
            break
        msgs = d.get("data", {}).get("messages", [])
        total_estimate = d.get("data", {}).get("total", total_estimate)
        if not msgs:
            break
        all_msgs.extend(msgs)
        log.info("  page %d: +%d (total %s)", page, len(msgs), total_estimate or len(all_msgs))
        if len(msgs) < per_page:
            break
        page += 1
        time.sleep(0.3)
    log.info("Fetched %d messages total", len(all_msgs))
    return all_msgs


def _normalize_phone(phone: str) -> str:
    """Strip +91 / country code prefix, return last 10 digits."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _fetch_bigin_deals() -> dict:
    """Fetch all Bigin pipeline deals → {last10: [deal, ...]}."""
    import json as j, subprocess
    all_deals, page_token = [], None
    per_page = 200

    while True:
        args = {
            "query_params": {
                "pipeline_id": "1325466000000466037",
                "fields": "id,Deal_Name,Phone,Email",
                "per_page": per_page,
            }
        }
        if page_token:
            args["query_params"]["page_token"] = page_token
        cmd = [
            "/opt/homebrew/bin/mcporter", "call",
            "bigin.Bigin_getRecordsFromSpecificTeamPipeline",
            "--args", j.dumps(args), "--output", "json"
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            log.warning("Bigin fetch failed: %s", proc.stderr.strip()[:200])
            break
        try:
            resp = j.loads(proc.stdout)
        except Exception:
            log.warning("Bigin parse error")
            break
        batch = resp.get("data", [])
        all_deals.extend(batch)
        info = resp.get("info", {})
        if not info.get("more_records"):
            break
        page_token = info.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.3)

    log.info("Fetched %d Bigin deals", len(all_deals))
    # Index by last10 digits (handles both masked and unmasked numbers)
    by_last10 = defaultdict(list)
    for d in all_deals:
        phone = (d.get("Phone") or "").strip()
        if phone:
            key = _normalize_phone(phone)
            if key:
                by_last10[key].append(d)
    return by_last10


def score_wa_signals(messages: list, by_phone: dict) -> list:
    """
    WA read:  OUTGOING + deliveryStatus=="read"  → +2
    WA reply: INCOMING                         → +5 (approved per Murali 2026-06-28)
    WA fail:  OUTGOING + deliveryStatus=="failed" →  0 (held — no penalty)
    """
    wa_data = defaultdict(lambda: {
        "wa_read": False, "wa_reply": False, "wa_fail": False,
        "read_count": 0, "fail_count": 0, "reply_count": 0,
    })

    for m in messages:
        to_num = (m.get("to") or "").strip()
        if not to_num:
            continue
        direction = m.get("direction") or ""
        status    = m.get("deliveryStatus") or ""

        if direction == "OUTGOING" and status == "read":
            wa_data[to_num]["wa_read"] = True
            wa_data[to_num]["read_count"] += 1
        elif direction == "OUTGOING" and status == "failed":
            wa_data[to_num]["wa_fail"] = True
            wa_data[to_num]["fail_count"] += 1
        elif direction == "INCOMING":
            wa_data[to_num]["wa_reply"] = True
            wa_data[to_num]["reply_count"] += 1

    results = []
    for phone, sig in sorted(wa_data.items()):
        score = 0
        if sig["wa_read"]:   score += WA_READ_PTS
        if sig["wa_reply"]:  score += WA_REPLY_PTS
        if sig["wa_fail"]:   score += WA_FAIL_PTS   # currently 0
        # Match by last10 digits (handles mask differences between Bigin and crm-messaging)
        deals = by_phone.get(_normalize_phone(phone), [])
        results.append({
            "phone":           phone,
            "wa_read":         sig["wa_read"],
            "wa_read_count":   sig["read_count"],
            "wa_reply":        sig["wa_reply"],
            "wa_reply_count":  sig["reply_count"],
            "wa_fail":         sig["wa_fail"],
            "wa_fail_count":   sig["fail_count"],
            "score":           score,
            "matched_deals":   [d["Deal_Name"] for d in deals],
            "deal_ids":        [d["id"] for d in deals],
        })

    return results


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    messages = _fetch_all_messages()
    by_phone = _fetch_bigin_deals()
    results  = score_wa_signals(messages, by_phone)

    matched         = [r for r in results if r["matched_deals"]]
    read_ct         = sum(1 for r in results if r["wa_read"])
    reply_ct        = sum(1 for r in results if r["wa_reply"])
    fail_ct         = sum(1 for r in results if r["wa_fail"])
    matched_read_ct = sum(1 for r in matched if r["wa_read"])
    matched_fail_ct = sum(1 for r in matched if r["wa_fail"])

    print(f"\n{'='*60}")
    print(f"  WA signal summary")
    print(f"  Messages processed:  {len(messages):>6}")
    print(f"  Unique recipients:  {len(results):>6}")
    print(f"  WA read (all):     {read_ct:>6}")
    print(f"  WA reply (all):    {reply_ct:>6}")
    print(f"  WA fail (all):     {fail_ct:>6}")
    print(f"  Matched to Bigin: {len(matched):>6}")
    print(f"  WA read → Bigin:  {matched_read_ct:>6}")
    print(f"  WA fail → Bigin:  {matched_fail_ct:>6}")
    print(f"{'='*60}\n")

    if matched:
        print(f"Leads with WA signals ({len(matched)}):")
        for r in sorted(matched, key=lambda x: -x["score"]):
            tags = []
            if r["wa_read"]:   tags.append(f"read({r['wa_read_count']})")
            if r["wa_reply"]:  tags.append(f"reply({r['wa_reply_count']})")
            if r["wa_fail"]:   tags.append(f"fail({r['wa_fail_count']})")
            deals = ", ".join(r["matched_deals"])
            print(f"  [{r['score']:+d}] {r['phone']} | {deals}")
            print(f"         tags: {', '.join(tags)}")

    csv_path = OUT_DIR / "wa_signals.csv"
    json_path = OUT_DIR / "wa_signals.json"

    with open(csv_path, "w") as f:
        fields = ["phone","score","wa_read","wa_read_count","wa_reply",
                  "wa_reply_count","wa_fail","wa_fail_count","matched_deals","deal_ids"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = dict(r)
            row["matched_deals"] = "|".join(r["matched_deals"])
            row["deal_ids"]      = "|".join(r["deal_ids"])
            w.writerow(row)

    with open(json_path, "w") as f:
        json.dump({
            "fetched_at":      datetime.now().isoformat(),
            "total_messages":   len(messages),
            "total_recipients": len(results),
            "signals":         results,
        }, f, indent=2, default=str)

    print(f"\nwrote CSV: {csv_path}")
    print(f"wrote JSON: {json_path}")


if __name__ == "__main__":
    main()
