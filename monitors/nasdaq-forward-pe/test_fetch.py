import unittest
from datetime import date, datetime, timedelta, timezone

from fetch import (
    DEDICATED,
    FETCH_METHOD,
    SOURCE_KIND,
    SEARCH_QUERY,
    build_payload,
    parse_search_index,
    validate_payload,
    validate_transition,
    validate_values,
)


GOOD = '''
<html><body>
1. Nasdaq 100 Forward PE Ratio - trendonify.com
The current rating for Nasdaq 100 Forward PE Ratio is "Undervalued", based on 10-year historical data.
How does the current P/E Ratio compare historically for Nasdaq 100 Forward PE Ratio ?
The current P/E Ratio of 20.7 ranks in the 21.7th percentile over the past decade, placing it in the Undervalued zone by historical standards.
trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio 2026-09-08T00:00:00.0000000
</body></html>
'''

CURRENT_STYLE = '''
<html><body>
NASDAQ-100 Forward PE Ratio
trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio
Last Updated: September 18, 2026
The NASDAQ-100 currently trades at a forward P/E ratio of 20.6 as of September 18, 2026.
Looking at the longer 10-year period, where the median sits at 22.91, the current reading ranks in the 20.8th percentile.
</body></html>
'''



class ParserTests(unittest.TestCase):
    def test_query_tracks_current_dedicated_title(self):
        self.assertEqual(SEARCH_QUERY, 'Trendonify "NASDAQ-100 Forward PE Ratio" percentile')

    def test_no_results_page_is_explicitly_rejected(self):
        raw = '<html><body>No results found for Trendonify "NASDAQ-100 Forward PE Ratio"</body></html>'
        with self.assertRaisesRegex(ValueError, "no results"):
            parse_search_index(raw)

    def test_good_dedicated_result(self):
        self.assertEqual(parse_search_index(GOOD), (20.7, 21.7, "2026-09-08"))

    def test_current_hyphenated_title_and_last_updated_style(self):
        self.assertEqual(parse_search_index(CURRENT_STYLE), (20.6, 20.8, "2026-09-18"))

    def test_result_identity_is_case_insensitive(self):
        raw = GOOD.replace(
            "trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio",
            "TRENDONIFY.COM/united-states/stock-market/nasdaq-100/forward-pe-ratio",
        )
        self.assertEqual(parse_search_index(raw), (20.7, 21.7, "2026-09-08"))

    def test_identical_duplicate_is_accepted(self):
        self.assertEqual(parse_search_index(GOOD + GOOD), (20.7, 21.7, "2026-09-08"))

    def test_conflicting_duplicate_is_rejected(self):
        other = GOOD.replace("20.7", "19.9").replace("21.7", "10.0")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            parse_search_index(GOOD + other)

    def test_bot_challenge_rejected(self):
        raw = "Unfortunately, bots use DuckDuckGo too. Select all squares containing a duck."
        with self.assertRaises(RuntimeError):
            parse_search_index(raw)

    def test_other_provider_rejected(self):
        raw = GOOD.replace(
            "trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio",
            "example.com/united-states/stock-market/nasdaq-100/forward-pe-ratio",
        )
        with self.assertRaises(ValueError):
            parse_search_index(raw)

    def test_wrong_trendonify_url_rejected(self):
        raw = GOOD.replace(
            "trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio",
            "trendonify.com/united-states/stock-market/nasdaq-100/pe-ratio",
        )
        with self.assertRaises(ValueError):
            parse_search_index(raw)

    def test_missing_date_rejected(self):
        raw = GOOD.replace("2026-09-08T00:00:00.0000000", "date unavailable")
        with self.assertRaises(ValueError):
            parse_search_index(raw)


class ValueValidationTests(unittest.TestCase):
    def test_invalid_pe(self):
        with self.assertRaises(ValueError):
            validate_values(0.5, 20.0, date.today().isoformat())

    def test_invalid_percentile(self):
        with self.assertRaises(ValueError):
            validate_values(20.0, 101.0, date.today().isoformat())

    def test_stale_date(self):
        stale = date.today() - timedelta(days=8)
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_values(20.0, 20.0, stale.isoformat())

    def test_future_date(self):
        future = date.today() + timedelta(days=2)
        with self.assertRaisesRegex(ValueError, "future"):
            validate_values(20.0, 20.0, future.isoformat())

    def test_date_rollback(self):
        today = date.today()
        with self.assertRaisesRegex(ValueError, "rollback"):
            validate_values(20.0, 20.0, today.isoformat(), today + timedelta(days=1))

    def test_implausible_same_date_transition(self):
        today = date.today().isoformat()
        previous = {
            "ok": True,
            "forward_pe": 20.0,
            "percentile_10y": 20.0,
            "data_date": today,
        }
        with self.assertRaisesRegex(ValueError, "same-date"):
            validate_transition(30.0, 20.0, today, previous)

    def test_large_but_plausible_same_date_market_move_is_accepted(self):
        today = date.today().isoformat()
        previous = {
            "ok": True,
            "forward_pe": 20.0,
            "percentile_10y": 50.0,
            "data_date": today,
        }
        validate_transition(16.0, 15.0, today, previous)

    def test_reasonable_transition_is_accepted(self):
        today = date.today().isoformat()
        previous = {
            "ok": True,
            "forward_pe": 20.0,
            "percentile_10y": 20.0,
            "data_date": today,
        }
        validate_transition(20.2, 21.0, today, previous)


class PayloadValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.payload = build_payload(
            20.7,
            21.7,
            self.now.date().isoformat(),
            fetched_at=self.now,
        )

    def test_valid_payload(self):
        age = validate_payload(self.payload, max_fetch_age_seconds=4500, now=self.now)
        self.assertEqual(age, 0)
        self.assertEqual(self.payload["source_url"], DEDICATED)
        self.assertEqual(self.payload["source_kind"], SOURCE_KIND)
        self.assertEqual(self.payload["fetch_method"], FETCH_METHOD)

    def test_source_mismatch_rejected(self):
        self.payload["source"] = "Other"
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            validate_payload(self.payload, now=self.now)

    def test_source_url_mismatch_rejected(self):
        self.payload["source_url"] = "https://example.com/"
        with self.assertRaisesRegex(ValueError, "source_url"):
            validate_payload(self.payload, now=self.now)

    def test_schema_mismatch_rejected(self):
        self.payload["schema_version"] = 999
        with self.assertRaisesRegex(ValueError, "schema_version"):
            validate_payload(self.payload, now=self.now)

    def test_stale_fetched_at_rejected(self):
        old = self.now - timedelta(seconds=4501)
        self.payload["fetched_at"] = old.isoformat().replace("+00:00", "Z")
        with self.assertRaisesRegex(ValueError, "fetched_at is stale"):
            validate_payload(self.payload, max_fetch_age_seconds=4500, now=self.now)

    def test_far_future_fetched_at_rejected(self):
        future = self.now + timedelta(seconds=601)
        self.payload["fetched_at"] = future.isoformat().replace("+00:00", "Z")
        with self.assertRaisesRegex(ValueError, "future"):
            validate_payload(self.payload, now=self.now)


if __name__ == "__main__":
    unittest.main()
