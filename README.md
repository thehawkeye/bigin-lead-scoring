# Bigin Lead Scoring

Deterministic lead scoring for the IIFR ECP pipeline. Combines signals from
Bigin CRM, crm-messaging.cloud (WhatsApp), and out-of-band Calendly/Webinar data.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials

python3 scripts/run_pipeline.py [--date YYYY-MM-DD]
```

## Pipeline (run in order)

```
scripts/
├── iifr_ecp_rescore_v3.py              # Step 1: base scores (Bigin signals)
├── iifr_ecp_rescore_v3_postprocess.py   # Step 2: Calendly (+10) / Webinar (+15)
├── iifr_wa_signal_scorer.py             # Step 3: WhatsApp signals
└── consolidate_scores.py                 # Step 4: merge all → final_scores.csv
```

## Scoring formula (v3, finalized 2026-06-28)

```
final_score = base_score + calendly_bonus + webinar_bonus + wa_score

base_score  = email_score + click_bonus + stage_bonus + tag_bonus
email_score = 2 × ln(1 + cumulative_open_count)   [no cap]
click_bonus = +1 if any click else 0

calendly_bonus = +10 (Calendly discovery call booked)
webinar_bonus  = +15 (Webinar attended)
wa_score       = +2 (WA read) +5 (WA reply from lead) +0 (WA fail, held)
```

## Tier thresholds

| Tier     | Score    |
|----------|----------|
| firehot  | ≥ 20     |
| hot      | 10–19.9  |
| warm     | 5–9.9    |
| cold     | ≤ 4.9    |

## Phone matching — Critical

Bigin and crm-messaging.cloud mask phone numbers **differently**.
Exact string comparison fails. Always use **last-10-digits matching**.

See: [docs/phone-matching.md](docs/phone-matching.md)

## Outputs

| File | Contents |
|---|---|
| `final_scores.csv` | All leads, all sources merged, ranked by final_score |
| `final_scores_summary.json` | Tier distribution, firehot list, top 20 |
| `rescore_v3_leads_override.csv` | Base + Calendly/Webinar |
| `wa_signals.csv` | WA signals (in Mac Mini Sync) |

## Spec document

`docs/lead-scoring-v3-canonical.md` — authoritative specification.
Update this before changing any scoring logic.

## Changelog

| Date | Change |
|---|---|
| 2026-06-28 | v3 finalized: email log scale, WA last10 matching, firehot ≥20 |
| 2026-06-28 | WA fail held at 0; WA read +2, WA reply +5 |
| 2026-06-28 | New `consolidate_scores.py` merges all 3 sources |
| 2026-06-28 | postprocess gains `--date` arg; hardcoded path removed |
