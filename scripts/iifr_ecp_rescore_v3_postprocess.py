#!/usr/bin/env python3
"""
iifr_ecp_rescore_v3_postprocess.py
Apply Murali's out-of-band signal overrides to the v3 re-score output.

Signals applied (additive on top of v3 composite score):
  Calendly discovery call booked: +10
  Webinar attended:               +15

Run AFTER iifr_ecp_rescore_v3.py

Usage:
    python3 iifr_ecp_rescore_v3_postprocess.py [--date YYYY-MM-DD]

Inputs:  cron/output/scoring/{date}/rescore_v3_leads.csv
Outputs: cron/output/scoring/{date}/rescore_v3_leads_override.csv
         cron/output/scoring/{date}/rescore_v3_summary_override.json
"""

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

# ── Overrides ──────────────────────────────────────────────────────────────────
# Calendly discovery calls booked (+10 additive)
CAL_OVERRIDES = {
    "1325466000000488112": "Calendly: Dr Sachin Gulati (12 Jun)",
    "1325466000000507098": "Calendly: Shilpa Smart (13 Jun)",
    "1325466000000521334": "Calendly: Rachana Dedhia (15 Jun)",
    "1325466000000508027": "Calendly: Amit Singh (16 Jun)",
    "1325466000000506626": "Calendly: Ashok Patil (17 Jun)",
    "1325466000000488126": "Calendly: Ridhima Gupta (17 Jun)",
    "1325466000000530207": "Calendly: syeda zehra (20 Jun)",
    "1325466000000529424": "Calendly: Dr.Amol Neve (23 Jun)",
    "1325466000000553575": "Calendly: Rahul Kumar Behera (24 Jun)",
    "1325466000000548114": "Calendly: Abhishek Somani (26 Jun)",
    "1325466000000548097": "Calendly: Kasturi Pomal (27 Jun)",
}

# Webinar attendees (+15 additive)
WEB_OVERRIDES = {
    "1325466000000520995": "Webinar: Niti Pathak",
    "1325466000000564399": "Webinar: Dr.A.Ramamoorthy Mathematics",
    "1325466000000563313": "Webinar: Ashwin S",
    "1325466000000533708": "Webinar: G. Ashwini",
    "1325466000000548114": "Webinar: Abhishek Somani",
    "1325466000000527039": "Webinar: Dr. Gyanendra Rawat",
    "1325466000000563508": "Webinar: Dr.Manjoo Rani",
    "1325466000000563538": "Webinar: Abhishek Kaushik",
    "1325466000000565477": "Webinar: Jeyachandran Subramanian",
    "1325466000000565561": "Webinar: Prakash Pandey",
    "1325466000000567822": "Webinar: Shalini Sahay",
    "1325466000000568795": "Webinar: Sony Singh",
    "1325466000000568807": "Webinar: Shalini Sahay (duplicate record)",
    "1325466000000568864": "Webinar: Divya Sharma",
    "1325466000000570355": "Webinar: Dr Veereesh Rampur",
    "1325466000000488126": "Webinar: Ridhima Gupta",
}

# Not found in Bigin (flagged): Kamini Veeresh, IK Singh, Pradeep Deshpande

CAL_BONUS = 10
WEB_BONUS = 15

# Tier thresholds (firehot ≥ 20; updated 2026-06-28)
TIER_THRESHOLDS = [
    (20, "firehot"), (10, "hot"), (5, "hot"), (1, "warm"), (0, "cold")
]


def tier_from_score(score: float) -> str:
    for lower_bound, name in TIER_THRESHOLDS:
        if score >= lower_bound:
            return name
    return "cold"


# ── Paths ───────────────────────────────────────────────────────────────────────

PROFILE_DIR = Path("~/.hermes/profiles/iifr-ecp-marketing").expanduser()
CRON_OUTPUT = PROFILE_DIR / "cron" / "output" / "scoring"


def make_parser():
    p = argparse.ArgumentParser(description="Apply Calendly/Webinar overrides to v3 scores")
    p.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date folder (YYYY-MM-DD). Default: today.",
    )
    return p


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    args = make_parser().parse_args()
    date = args.date

    run_dir = CRON_OUTPUT / date
    input_csv = run_dir / "rescore_v3_leads.csv"
    if not input_csv.exists():
        print(f"[ERROR] {input_csv} not found — run iifr_ecp_rescore_v3.py first", file=sys.stderr)
        return 1

    rows = []
    with open(input_csv) as f:
        for row in csv.DictReader(f):
            rows.append(row)

    print(f"Loaded {len(rows)} leads from {input_csv.name}")

    # Apply overrides
    changed = 0
    for row in rows:
        did = row.get("deal_id", "")
        base_score = float(row.get("score") or 0)
        bonus = 0
        reasons = []

        if did in CAL_OVERRIDES:
            bonus += CAL_BONUS
            reasons.append(CAL_OVERRIDES[did] + " (+10)")
        if did in WEB_OVERRIDES:
            bonus += WEB_BONUS
            reasons.append(WEB_OVERRIDES[did] + " (+15)")

        row["override_bonus"] = bonus
        row["override_reason"] = " | ".join(reasons) if reasons else ""
        row["new_score"] = round(base_score + bonus, 2)
        row["new_tier"] = tier_from_score(row["new_score"])

        if bonus > 0:
            changed += 1

    # Summary
    orig_dist  = Counter(tier_from_score(float(r.get("score") or 0)) for r in rows)
    new_dist   = Counter(r["new_tier"] for r in rows)
    by_source  = Counter(
        r.get("lead_source", "").strip() or "(unknown)" for r in rows
    )
    firehot_leads = [r for r in rows if r["new_tier"] == "firehot"]
    hot_leads     = [r for r in rows if r["new_tier"] == "hot"]

    # Write CSV
    out_csv = run_dir / "rescore_v3_leads_override.csv"
    cols = list(rows[0].keys())
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    summary = {
        "date": date,
        "total_leads": len(rows),
        "overrides_applied": changed,
        "orig_tier_distribution": dict(orig_dist),
        "new_tier_distribution": dict(new_dist),
        "firehot_leads": [
            {"deal_id": r["deal_id"], "deal_name": r["deal_name"],
             "email": r["email"], "new_score": r["new_score"],
             "new_tier": r["new_tier"], "override_reason": r["override_reason"]}
            for r in firehot_leads
        ],
        "hot_leads": [
            {"deal_id": r["deal_id"], "deal_name": r["deal_name"],
             "email": r["email"], "new_score": r["new_score"],
             "new_tier": r["new_tier"], "override_reason": r["override_reason"]}
            for r in hot_leads if r["override_bonus"] > 0
        ],
        "by_source": dict(sorted(by_source.items(), key=lambda x: -x[1])),
        "not_found_in_bigin": ["Kamini Veeresh", "IK Singh", "Pradeep Deshpande"],
    }

    out_json = run_dir / "rescore_v3_summary_override.json"
    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"\nOverrides applied: {changed}")
    print(f"Output CSV: {out_csv}")
    print(f"Output JSON: {out_json}")
    print(f"\nOrig distribution: {dict(orig_dist)}")
    print(f"New distribution:  {dict(new_dist)}")
    print(f"\nFirehot ({len(firehot_leads)}):")
    for r in firehot_leads:
        print(f"  {r['deal_name']} | score={r['new_score']} | {r['override_reason']}")
    print(f"\nHot with override ({len([r for r in hot_leads if r['override_bonus']>0])}):")
    for r in hot_leads:
        if r["override_bonus"] > 0:
            print(f"  {r['deal_name']} | score={r['new_score']} | {r['override_reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
