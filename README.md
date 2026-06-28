# Bigin Lead Scoring

Deterministic lead scoring for the IIFR ECP pipeline. Combines signals from:
- **Bigin CRM** (email, clicks, page views, stage, tags)
- **crm-messaging.cloud** (WhatsApp read receipts and replies)
- **Calendly** (discovery call bookings — out-of-band overrides)
- **Webinar attendance** (out-of-band overrides)

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env with your Bigin refresh token and crm-messaging API key

# 3. Run the pipeline
python3 scripts/iifr_ecp_rescore_v3.py
python3 scripts/iifr_ecp_rescore_v3_postprocess.py
python3.11 scripts/iifr_wa_signal_scorer.py
```

## Architecture

```
scripts/
├── iifr_ecp_rescore_v3.py              # Core scoring (Bigin signals only)
├── iifr_ecp_rescore_v3_postprocess.py  # Calendly/Webinar overrides
├── iifr_wa_signal_scorer.py             # WhatsApp signals (crm-messaging.cloud)
├── iifr_crm_messaging_backfill.py      # WA historical backfill
└── iifr_gate_check.py                  # Pre-launch QA gate

cron/output/scoring/YYYY-MM-DD/
├── rescore_v3_leads.csv                # Scored leads (v3 base score)
├── rescore_v3_leads_override.csv       # + Calendly/Webinar bonuses
└── wa_signals.csv                      # WhatsApp signal matches
```

## Scoring formula (v3)

```
composite_score = email_score + click_bonus + stage_bonus + tag_bonus
email_score     = 2 × ln(1 + cumulative_open_count)     [no cap]
click_bonus     = +1 if any click else 0
stage_bonus     = Deal Stage → warm (1–4) or hot (5+)
tag_bonus       = ECP interest tags → +1 to +3

Overrides (post-process, additive):
  Calendly discovery call → +10
  Webinar attendance      → +15
  WA read receipt         → +2
  WA reply from lead      → +5
  WA fail delivery        → 0 (held, no penalty)
```

## Tier thresholds

| Tier     | Score    |
|----------|----------|
| firehot  | ≥ 20     |
| hot      | 10–19.9  |
| warm     | 5–9.9    |
| cold     | ≤ 4.9    |

## Phone matching — IMPORTANT

Bigin and crm-messaging.cloud mask phone numbers differently:
- **Bigin:** `+91X****YYYY` (first 3 + last 4 digits)
- **crm-messaging:** `+91X****ZZZZ` (same format, different split)

**Exact string comparison fails. Always use last-10-digits matching.**

See: [docs/phone-matching.md](docs/phone-matching.md)

## Spec document

`docs/lead-scoring-v3-canonical.md` — authoritative specification for all scoring rules.
Update this before changing any scoring logic.
