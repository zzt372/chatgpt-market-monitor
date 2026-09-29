import unittest
from datetime import datetime, timezone

from drawdown import build_payload, validate_payload


class DrawdownTests(unittest.TestCase):
    # Producer regression coverage for official Nasdaq close-history drawdown.
    def test_build_payload(self):
        rows = [
            {"date": "09/29/2026", "close": "90.00"},
            {"date": "09/28/2026", "close": "100.00"},
            {"date": "09/27/2026", "close": "95.00"},
        ]
        now = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        p = build_payload(rows, 7805, fetched_at=now)
        self.assertEqual(p["current_close"], 90.0)
        self.assertEqual(p["ath_close"], 100.0)
        self.assertEqual(p["ath_date"], "2026-09-28")
        self.assertEqual(p["drawdown_pct"], -10.0)
        validate_payload(p, now=now)

    def test_duplicate_ath_uses_latest_date(self):
        rows = [
            {"date": "09/29/2026", "close": "100.00"},
            {"date": "09/28/2026", "close": "100.00"},
        ]
        p = build_payload(rows, 7805, fetched_at=datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(p["ath_date"], "2026-09-29")
        self.assertEqual(p["drawdown_pct"], 0.0)

    def test_reject_bad_drawdown(self):
        rows = [
            {"date": "09/29/2026", "close": "90.00"},
            {"date": "09/28/2026", "close": "100.00"},
        ]
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        p = build_payload(rows, 7805, fetched_at=now)
        p["drawdown_pct"] = -9.0
        with self.assertRaisesRegex(ValueError, "drawdown mismatch"):
            validate_payload(p, now=now)

    def test_reject_short_history(self):
        rows = [{"date": "09/29/2026", "close": "100.00"}]
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        p = build_payload(rows, 1, fetched_at=now)
        with self.assertRaisesRegex(ValueError, "history too short"):
            validate_payload(p, now=now)


if __name__ == "__main__":
    unittest.main()
