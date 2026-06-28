#!/usr/bin/env python3
"""
run_pipeline.py — Run the full ECP lead scoring pipeline in order.

Usage:
    python3 scripts/run_pipeline.py [--date YYYY-MM-DD]

Steps:
  1. iifr_ecp_rescore_v3.py              — base scores from Bigin
  2. iifr_ecp_rescore_v3_postprocess.py  — Calendly/Webinar overrides
  3. iifr_wa_signal_scorer.py             — WhatsApp signals

Outputs land under:
    ~/.hermes/profiles/iifr-ecp-marketing/cron/output/scoring/{date}/
    ~/Documents/Mac Mini Sync/Lyra sync/iifr-ecp-wa/  (WA only)
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROFILE_DIR = Path("~/.hermes/profiles/iifr-ecp-marketing").expanduser()
SCRIPTS_DIR = PROFILE_DIR / "scripts"
PYTHON = "python3"
PYTHON311 = "python3.11"


def run(cmd: list[str], label: str) -> int:
    print(f"\n{'='*60}\nSTEP: {label}\n{'='*60}", flush=True)
    result = subprocess.run(cmd, cwd=SCRIPTS_DIR)
    if result.returncode != 0:
        print(f"[FAIL] {label} exited {result.returncode}", flush=True)
        return result.returncode
    print(f"[OK] {label}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full ECP scoring pipeline")
    parser.add_argument(
        "--date", default=datetime.now().strftime("%Y-%m-%d"),
        help="Output date folder (default: today)"
    )
    args = parser.parse_args()

    print(f"Pipeline date: {args.date}")
    print(f"Scripts dir:  {SCRIPTS_DIR}")

    steps = [
        ([PYTHON, "iifr_ecp_rescore_v3.py"],
         "Step 1: Base scoring (Bigin signals)"),

        ([PYTHON, "iifr_ecp_rescore_v3_postprocess.py"],
         "Step 2: Calendly / Webinar overrides"),

        ([PYTHON311, "iifr_wa_signal_scorer.py"],
         "Step 3: WhatsApp signals"),
    ]

    for cmd, label in steps:
        rc = run(cmd, label)
        if rc != 0:
            print(f"\nPipeline stopped at: {label}")
            return rc

    print(f"\n{'='*60}")
    print("Pipeline complete: {args.date}")
    print(f"{'='*60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
