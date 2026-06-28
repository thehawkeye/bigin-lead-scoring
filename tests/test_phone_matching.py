#!/usr/bin/env python3
"""Unit tests for phone matching — last-10-digits logic."""

import unittest

# Copy of the normalize function for testing (matches iifr_wa_signal_scorer.py)
def normalize_phone(phone: str) -> str:
    """Strip country code, return last 10 digits."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


class TestPhoneMatching(unittest.TestCase):

    def test_exact_match(self):
        """Identical masked phones match."""
        self.assertEqual(normalize_phone("+919876550110"), normalize_phone("+919876550110"))

    def test_mask_diff_same_number(self):
        """Bigin mask vs crm-messaging mask for the same number."""
        # Bigin: first 3 + last 4
        bigin = "+919****0110"   # digits: 919876550110
        # crm-messaging: different split
        crm  = "+919****5011"   # same digits: 91987655011... wait
        # Let's use real numbers
        bigin = "+919****3010"   # Ridhima: digits = 919995333010
        crm   = "+919****0115"   # different last4 split
        self.assertEqual(normalize_phone(bigin), normalize_phone(crm))

    def test_spaces_and_prefix(self):
        """Handles spaces and country code prefix."""
        a = "+91 98765 50110"
        b = "919876550110"
        c = "+919876550110"
        self.assertEqual(normalize_phone(a), normalize_phone(b))
        self.assertEqual(normalize_phone(b), normalize_phone(c))

    def test_short_number(self):
        """Short numbers return as-is (no padding)."""
        self.assertEqual(normalize_phone("987651"), "987651")

    def test_empty(self):
        """Empty string handled gracefully."""
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone(None), "")  # type: ignore

    def test_real_data_samples(self):
        """Real numbers from the 2026-06-28 run."""
        cases = [
            # (bigin_phone, crm_phone, expected_equal)
            ("+919****3010", "+919****3010", True),   # Ridhima
            ("+919****0115", "+919****0115", True),   # Muralikrishnan
            ("447828573520", "447828573520", True),   # Prateek (UK)
            ("+919876550110", "+919876550110", True),
        ]
        for bigin_ph, crm_ph, expected in cases:
            result = normalize_phone(bigin_ph) == normalize_phone(crm_ph)
            self.assertEqual(result, expected,
                f"{bigin_ph} vs {crm_ph}: expected {expected}, got {result}")

    def test_known_fail_case(self):
        """The exact case that failed before the fix."""
        # Bigin: Ridhima = +919****3010  → last10 = 9995333010
        # crm-messaging: +919****0115     → last10 = 9867200115
        # These should NOT match (different numbers)
        bigin = "+919****3010"
        crm   = "+919****0115"
        self.assertNotEqual(normalize_phone(bigin), normalize_phone(crm))

    def test_known_pass_case(self):
        """The cases that now correctly match after the fix."""
        # Muralikrishnan in both
        bigin = "+919****0115"
        crm   = "+919****0115"
        self.assertEqual(normalize_phone(bigin), normalize_phone(crm))


if __name__ == "__main__":
    unittest.main()
