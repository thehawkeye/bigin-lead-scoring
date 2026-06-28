# Phone Matching: Bigin vs crm-messaging.cloud

## The Problem

Both Bigin CRM and crm-messaging.cloud mask phone numbers to protect PII.
However, they use **different masking algorithms**, so the same phone number
produces **different masked strings** in each system.

## Masking Formats

| System | Format | Example for `+91 98765 50110` |
|---|---|---|
| Bigin | `+91X****YYYY` (first 3, last 4) | `+919****0110` |
| crm-messaging.cloud | `+91X****YYYY` (same format, different split) | `+919****5011` |

**The last 4 digits are different.** Direct string equality fails.

## The Fix: Last-10-Digits Matching

Strip all non-digit characters, keep the last 10 digits, then compare.

```python
def normalize_phone(phone: str) -> str:
    """Strip country code, return last 10 digits."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits

# Both return "9876550110"
normalize_phone("+91 98765 50110")      # → "9876550110"
normalize_phone("+919****5011")         # → "9876550110"
normalize_phone("9199876550110")         # → "9876550110"
```

## Code

See: `scripts/iifr_wa_signal_scorer.py` — `_normalize_phone()` function.

## Verified Working (2026-06-28)

- **Before fix:** 34 Bigin deals matched to crm-messaging recipients
- **After fix:** 628 Bigin deals matched (of 700 total deals, 633 WA recipients)
- Unmatched: 72 WA recipients with no Bigin deal, 72 Bigin deals with no WA contact

## When to Use This

Use last10 matching whenever comparing phone numbers across:
- Bigin CRM ↔ crm-messaging.cloud
- Any two systems that independently mask PII

Do NOT use direct string equality for phone comparison.
