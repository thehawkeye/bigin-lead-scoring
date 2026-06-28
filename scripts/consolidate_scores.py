#!/usr/bin/env python3
"""
consolidate_scores.py — Merge all scoring sources into one final score per lead.

Run AFTER (in order):
  1. iifr_ecp_rescore_v3.py
  2. iifr_ecp_rescore_v3_postprocess.py
  3. iifr_wa_signal_scorer.py

Sources merged (each additive):
  ┌─────────────────────┬───────────────┬──────────────────────────────┐
  │ Source              │ Points        │ Where it lives               │
  ├─────────────────────┼───────────────┼──────────────────────────────┤
  │ Bigin base signals  │ score         │ rescore_v3_leads.csv         │
  │ Calendly discovery  │ +10           │ rescore_v3_leads_override.csv│
  │ Webinar attended    │ +15           │ rescore_v3_leads_override.csv│
  │ WhatsApp read       │ +2            │ wa_signals.csv               │
  │ WhatsApp reply      │ +5            │ wa_signals.csv               │
  │ WhatsApp fail       │ 0 (held)      │ wa_signals.csv               │
  └─────────────────────┴───────────────┴──────────────────────────────┘

Usage:
    python3 consolidate_scores.py [--date YYYY-MM-DD]

Inputs (all under cron/output/scoring/{date}/):
    rescore_v3_leads.csv
    rescore_v3_leads_override.csv   ← Calendly/Webinar bonuses
    ../iifr-ecp-wa/wa_signals.csv   ← WA signals

Outputs:
    final_scores.csv
    final_scores_summary.json
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

PROFILE_DIR = Path("~/.hermes/profiles/iifr-ecp-marketing").expanduser()
CRON_OUTPUT = PROFILE_DIR / "cron" / "output" / "scoring"
WA_OUTPUT   = PROFILE_DIR / "scripts"   # WA scorer writes to scripts/cron/.../wa_signals.csv

# WA bonus values (must match iifr_wa_signal_scorer.py)
WA_READ_PTS  = 2
WA_REPLY_PTS = 5

# Calendly/Webinar bonuses (must match iifr_ecp_rescore_v3_postprocess.py)
CAL_BONUS = 10
WEB_BONUS = 15

# Tier thresholds
TIER_THRESHOLDS = [
    (20, "firehot"), (10, "hot"), (5, "hot"), (1, "warm"), (0, "cold")
]


def tier(score: float) -> str:
    for threshold, name in TIER_THRESHOLDS:
        if score >= threshold:
            return name
    return "cold"


def make_parser():
    p = argparse.ArgumentParser(description="Consolidate all scoring sources into final scores")
    p.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date folder (YYYY-MM-DD). Default: today.",
    )
    return p


# ── Load helpers ───────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_wa_scores(date: str) -> dict[str, dict]:
    """
    Load WA signals from the WA scorer's output directory.
    WA scorer writes to scripts/cron/output/scoring/{date}/wa_signals.csv
    but we normalize to ~/Documents/Mac Mini Sync/Lyra sync/iifr-ecp-wa/
    which is always the latest run.

    Returns: {deal_id: {"wa_score": int, "wa_read": bool, "wa_reply": bool}}
    """
    # Try the Mac Mini Sync path (always latest)
    wa_dir = Path.home() / "Documents/Mac Mini Sync/Lyra sync/iifr-ecp-wa"
    wa_csv = wa_dir / "wa_signals.csv"

    # Fallback to scripts cron path
    if not wa_csv.exists():
        wa_csv = PROFILE_DIR / "scripts" / "cron" / "output" / "scoring" / date / "wa_signals.csv"

    if not wa_csv.exists():
        print(f"[WARN] WA signals not found at {wa_csv}", file=sys.stderr)
        return {}

    rows = load_csv(wa_csv)
    # WA CSV fields: phone, score, wa_read, wa_read_count, wa_reply,
    #                wa_reply_count, wa_fail, wa_fail_count,
    #                matched_deals (pipe-separated), deal_ids (pipe-separated)

    deal_scores: dict[str, dict] = defaultdict(
        lambda: {"wa_score": 0, "wa_read": False, "wa_reply": False}
    )

    for row in rows:
        score = int(row.get("score") or 0)
        deal_ids_raw = row.get("deal_ids", "")
        if not deal_ids_raw:
            continue
        for did in deal_ids_raw.split("|"):
            did = did.strip()
            if did:
                deal_scores[did]["wa_score"] += score
                if row.get("wa_read") == "True":
                    deal_scores[did]["wa_read"] = True
                if row.get("wa_reply") == "True":
                    deal_scores[did]["wa_reply"] = True

    return dict(deal_scores)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    args = make_parser().parse_args()
    date = args.date
    run_dir = CRON_OUTPUT / date
    wa_dir   = Path.home() / "Documents/Mac Mini Sync/Lyra sync/iifr-ecp-wa"

    base_csv  = run_dir / "rescore_v3_leads.csv"
    over_csv  = run_dir / "rescore_v3_leads_override.csv"
    wa_csv    = wa_dir   / "wa_signals.csv"

    # Check inputs
    missing = []
    for label, path in [
        ("Base scores",    base_csv),
        ("Overrides",      over_csv),
        ("WA signals",     wa_csv),
    ]:
        if not path.exists():
            missing.append(f"  {label}: {path}")

    if missing:
        print("[ERROR] Missing inputs:\n" + "\n".join(missing), file=sys.stderr)
        print("\nRun in order first:", file=sys.stderr)
        print("  1. python3 iifr_ecp_rescore_v3.py", file=sys.stderr)
        print("  2. python3 iifr_ecp_rescore_v3_postprocess.py", file=sys.stderr)
        print("  3. python3.11 iifr_wa_signal_scorer.py", file=sys.stderr)
        return 1

    # Load
    base_rows = load_csv(base_csv)
    over_rows = load_csv(over_csv)
    wa_scores = load_wa_scores(date)

    print(f"Loaded: {len(base_rows)} base, {len(over_rows)} override, "
          f"{len(wa_scores)} WA-matched deals")

    # Build override lookup: deal_id → {bonus, reason}
    over_map: dict[str, dict] = {}
    for r in over_rows:
        did = r.get("deal_id", "")
        if did:
            over_map[did] = {
                "override_bonus":  int(r.get("override_bonus") or 0),
                "override_reason":  r.get("override_reason", ""),
            }

    # Consolidate
    results = []
    for r in base_rows:
        did = r.get("deal_id", "")

        base_score     = float(r.get("score") or 0)
        over           = over_map.get(did, {})
        over_bonus     = over.get("override_bonus", 0)
        over_reason    = over.get("override_reason", "")

        wa             = wa_scores.get(did, {})
        wa_score       = wa.get("wa_score", 0)

        final_score    = round(base_score + over_bonus + wa_score, 2)
        final_tier     = tier(final_score)

        # Source breakdown
        sources = []
        if base_score > 0:
            sources.append(f"base({base_score})")
        if over_bonus > 0:
            sources.append(f"cal/web({over_bonus})")
        if wa_score > 0:
            sources.append(f"wa({wa_score})")

        row = dict(r)
        row["override_bonus"]  = over_bonus
        row["override_reason"] = over_reason
        row["wa_score"]        = wa_score
        row["wa_read"]         = wa.get("wa_read", False)
        row["wa_reply"]        = wa.get("wa_reply", False)
        row["final_score"]     = final_score
        row["final_tier"]      = final_tier
        row["score_sources"]   = " + ".join(sources) if sources else "none"
        results.append(row)

    # Sort by final_score descending
    results.sort(key=lambda x: -x["final_score"])

    # Write output
    out_csv = run_dir / "final_scores.csv"
    out_json = run_dir / "final_scores_summary.json"

    # Column order
    priority_cols = [
        "deal_id", "deal_name", "email", "phone", "lead_source",
        "score", "override_bonus", "wa_score",
        "final_score", "final_tier",
        "override_reason", "score_sources",
        "wa_read", "wa_reply",
        "tag", "stage",
    ]
    all_cols = priority_cols + [
        c for c in results[0].keys() if c not in priority_cols
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    # Summary
    tier_dist  = Counter(r["final_tier"] for r in results)
    firehot    = [r for r in results if r["final_tier"] == "firehot"]
    hot        = [r for r in results if r["final_tier"] == "hot"]
    warm       = [r for r in results if r["final_tier"] == "warm"]
    cold       = [r for r in results if r["final_tier"] == "cold"]
    wa_matched = [r for r in results if r.get("wa_score", 0) > 0]
    cal_web    = [r for r in results if r.get("override_bonus", 0) > 0]

    summary = {
        "date": date,
        "total_leads": len(results),
        "final_tier_distribution": dict(sorted(tier_dist.items())),
        "leads_with_wa_signal": len(wa_matched),
        "leads_with_cal_or_web": len(cal_web),
        "firehot_leads": [
            {k: r[k] for k in ["deal_id", "deal_name", "email",
                                 "final_score", "final_tier",
                                 "override_reason", "score_sources"]}
            for r in firehot
        ],
        "hot_leads": [
            {k: r[k] for k in ["deal_id", "deal_name", "email",
                                 "final_score", "final_tier",
                                 "override_reason", "score_sources"]}
            for r in hot
        ],
        "top_20_leads": [
            {k: r[k] for k in ["deal_id", "deal_name", "final_score",
                                 "final_tier", "score_sources"]}
            for r in results[:20]
        ],
    }

    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # Print report
    print(f"\n{'='*60}")
    print(f"  Final Scores — {date}")
    print(f"{'='*60}")
    print(f"\nFinal tier distribution:")
    for t in ["firehot", "hot", "warm", "cold"]:
        print(f"  {t:8s}: {tier_dist.get(t, 0):3d}")

    print(f"\n  WA-matched leads: {len(wa_matched)}")
    print(f"  Calendly/Webinar: {len(cal_web)}")
    print(f"  Total leads:      {len(results)}")

    print(f"\nFirehot ({len(firehot)}):")
    for r in firehot:
        print(f"  {r['deal_name'][:40]:40s} | {r['final_score']:5.1f} | {r['score_sources']}")

    print(f"\nTop 20 leads:")
    for r in results[:20]:
        print(f"  {r['final_tier']:8s} {r['final_score']:5.1f} | {r['deal_name'][:40]}")

    print(f"\nOutput CSV: {out_csv}")
    print(f"Output JSON: {out_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
