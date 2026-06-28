# Phone Matching Tests

> **IMPORTANT:** Bigin and crm-messaging.cloud use different phone masking.
> All tests must use last-10-digits matching. Direct string comparison will fail silently.

## Test Cases

| Bigin Phone        | crm-messaging `to` | Expected last10 match |
|---|---|---|
| `+919****0110`    | `+919****0110`     | ✅ YES                |
| `+919****0110`    | `+919****0110`     | ✅ YES (different mask) |
| `+91 98765 5011`  | `+919****5011`     | ✅ YES (spaces)       |
| `91987555011`     | `+919****5011`     | ✅ YES (no prefix)    |
| `+91X****YYYY`    | `+91X****ZZZZ`     | ❌ NO (different last4) |

## How to Run

```bash
python3 -m pytest tests/test_phone_matching.py -v
```
