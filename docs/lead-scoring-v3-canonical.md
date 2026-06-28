# IIFR ECP Lead Scoring — Canonical Specification

> **Source of truth.** All scoring logic changes must be reflected here first.
> Document the change, run the numbers against current pipeline, then update the scripts.

---

## Overview

The ECP lead scoring pipeline assigns each Bigin pipeline deal a **composite score** (v3, finalized 2026-06-28) from multiple signal sources. Scores are computed deterministically with no LLM calls.

**Tier thresholds:**

| Tier     | Composite score |
|----------|----------------|
| firehot  | ≥ 20           |
| hot      | 10 – 19.9      |
| warm     | 5 – 9.9        |
| cold     | ≤ 4.9          |

---

## Source 1: Email Engagement (Bigin CDP)

Formula: `2 × ln(1 + cumulative_open_count)` — log scale, no hard cap (updated 2026-06-28; was band × 1.2 capped at 4.0)

| Opens | Score | Tier contribution |
|---|---|---|
| 0     | 0     | cold              |
| 1     | 1.4   | cold              |
| 6     | 3.8   | warm              |
| 10    | 4.8   | warm              |
| 20    | 6.2   | hot               |
| 50    | 7.8   | hot               |
| 100   | 9.4   | hot               |
| 200   | 10.6  | hot (breaks 10)   |
| 500   | 12.4  | hot               |

> Rationale: log scale provides diminishing returns. Email alone can reach hot (10+) but never firehot (20+). Calendly and Webinar remain dominant qualifiers.

**Clicks:** `+1` if any click event exists (binary, no count weighting).

---

## Source 2: WhatsApp (crm-messaging.cloud)

Source: `https://app.crm-messaging.cloud/index.php/Api/messageHistory` — separate from Bigin.
Auth: Bearer token from `scripts/.env → CRM_MESSAGING_API_KEY`.

### Phone Matching — Critical Bug Fix (2026-06-28)

Bigin masks phone as `+91X****XXXX` (first 3 + last 4 digits).
crm-messaging masks differently — e.g., `+91X****0115` vs Bigin's `+91X****4155` for the same number.
**Exact string match fails. Use last-10-digits matching.**

| Signal | Score | Condition |
|---|---|---|
| WA read | +2 | OUTGOING msg + deliveryStatus == "read" |
| WA reply | +5 | INCOMING msg from lead |
| WA fail | 0 | OUTGOING msg + deliveryStatus == "failed" (held — no penalty) |

Current data (2,912 messages total):
- 327 unique WA recipients with read receipts → **323 matched to Bigin** (via last10)
- 479 WA fail instances → matched to Bigin (score = 0)
- 26 fail-only leads → score 0 (no WA read)
- 1 INCOMING reply (from 3 numbers asking "Tell me more about ECP") — not matched to Bigin yet

### 3-Day WhatsApp Gap Rule

Do NOT send another automated WhatsApp to the same lead within 3 days of the last outbound WA message.

### 24-Hour Manual WA Ban

After an automated WhatsApp is sent, wait 24 hours before any manual WA to the same lead.

---

## Source 3: Calendly Discovery Calls

**Out-of-band signal — applied as overrides in post-processing.**

Calendly discovery call booked → **+10** additive on top of composite score.

Matching: strict Bigin `Deal_Name` string match against Calendly event attendee names.
Known deal IDs and names are hard-coded in `iifr_ecp_rescore_v3_postprocess.py`.

---

## Source 4: Webinar Attendance

**Out-of-band signal — applied as overrides in post-processing.**

Webinar attended → **+15** additive.

Matching: strict Bigin `Deal_Name` string match.
Known deal IDs and names are hard-coded in `iifr_ecp_rescore_v3_postprocess.py`.

> Note: Webinar bonus is additive on top of Calendly bonus. A lead with both Calendly (+10) and Webinar (+15) plus base score reaches 25+ → firehot.

---

## Stage Bonus

Bigin pipeline stage → warm or hot tier nudge.

## Tag Bonus

ECP interest tags → +1 to +3 based on tag type.

---

## Override: Landing Page Firehot

If a lead has:
- `Lead_Source == "IIFR ECP Landing Page"`
- No WA negative signals
- Created after 2026-06-25

→ Score = 10 (guaranteed hot minimum)

---

## Excluded Test Contacts

The following emails are excluded from all scoring calculations:

- muralikrishnan@gmail.com
- muralikrishna.n@gmail.com
- muralikrishnan+vercel@gmail.com
- muralikrishnan+sms@gmail.com
- thebombaygeek@gmail.com

---

## Non-Negotiable Rules

1. **Single-writer rule for Bigin.** Live Bigin writes are owned by `latticed-bd` only.
2. **Claims must be source-verified.** Never copy competitor ad claims. Never invent proof points.
3. **Every campaign has segment, angle, creative ID, source tracking.**
4. **Decisions live in the approval log** — `~/.hermes/workspace/memory/projects/iifr-ecp/approvals.jsonl`
5. **SMS is frozen** — never recommend or use SMS outreach.

---

## Changelog

| Date | Change |
|---|---|
| 2026-06-28 | v3 finalized: email log scale, WA last10 matching, firehot threshold 20+ |
| 2026-06-28 | WA fail held at 0 (was -1, blocked) |
| 2026-06-28 | WA read reduced to +2 (was +3) |
| 2026-06-28 | Webinar bonus +15 additive (was replacing Calendly) |
| 2026-06-28 | Calendly bonus +10 additive |
| 2026-06-28 | Email formula: `2×ln(1+n)` no cap (was band×1.2 capped at 4.0) |
