#!/usr/bin/env python3
"""
Human gate check — used by orchestrator scripts BEFORE launching any
creative brief, campaign, nurture sequence, or budget change.

Loads approvals.jsonl and returns:
  0  → approved (approved_by field is set and not empty)
  1  → pending (no approved_by — block and request approval)
  2  → rejected
  3  → not found

Usage:
    gate_check.py creative ECP-B-20260618-01
    gate_check.py campaign Q3-launch-01
    gate_check.py budget campaign-2026-Q3
"""
import json, os, sys
from pathlib import Path

APPROVALS = Path.home() / ".hermes/workspace/memory/projects/iifr-ecp/approvals.jsonl"

DECISION_TYPES = {"creative", "campaign", "budget", "outreach", "stage",
                  "claim", "landing_page", "kill_switch", "system"}

def read_entries():
    if not APPROVALS.exists():
        return []
    entries = []
    with open(APPROVALS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries

def check_approval(decision_type: str, item_id: str):
    """Check if item has explicit human approval in log."""
    entries = read_entries()
    # Find the latest entry for this item
    candidates = [e for e in entries
                  if e.get("item_id") == item_id
                  and e.get("decision_type") == decision_type]
    if not candidates:
        return None  # not found
    latest = candidates[-1]
    return latest.get("status"), latest.get("approved_by"), latest

def main():
    if len(sys.argv) < 3:
        print("Usage: gate_check.py <decision_type> <item_id>", file=sys.stderr)
        sys.exit(3)

    decision_type = sys.argv[1].lower()
    item_id = sys.argv[2]

    if decision_type not in DECISION_TYPES:
        print(f"[warn] unknown decision_type {decision_type!r}, proceeding", file=sys.stderr)

    status, approved_by, entry = check_approval(decision_type, item_id)

    if status is None:
        print(f"NOT_FOUND: {decision_type}/{item_id} not in approval log")
        sys.exit(3)

    print(f"STATUS={status} APPROVED_BY={approved_by} ITEM={item_id}")

    if status == "approved" and approved_by:
        print("GATE_OPEN")
        sys.exit(0)
    elif status == "approved" and not approved_by:
        # backwards compat: agent-self-approved, flag it
        print(f"WARN: approved without human approved_by — {item_id}")
        sys.exit(0)
    elif status == "pending":
        print(f"GATE_BLOCKED: {decision_type}/{item_id} pending human approval")
        sys.exit(1)
    elif status == "rejected":
        print(f"GATE_REJECTED: {decision_type}/{item_id} rejected")
        sys.exit(2)
    elif status == "escalated":
        print(f"GATE_ESCALATED: {decision_type}/{item_id} escalated")
        sys.exit(1)
    else:
        print(f"UNKNOWN_STATUS: {status} for {item_id}")
        sys.exit(1)

if __name__ == "__main__":
    main()
