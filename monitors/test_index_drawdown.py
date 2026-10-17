"""Contract and boundary tests for the shared official-index data producer."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from index_drawdown import IndexCollector


NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def history(count=253, latest=date(2026, 10, 16), price=100):
    """Provide distinct weekday rows, newest first, like the official API."""
    rows = []
    day = latest
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append({"date": day.strftime("%m/%d/%Y"), "close": str(price)})
        day -= timedelta(days=1)
    return rows


class IndexProducerTests(unittest.TestCase):
    def setUp(self):
        self.collector = IndexCollector("NDX", Path("unused-ndx.json"))

    def build(self, rows, stamp=NOW):
        return self.collector.build_payload(rows, len(rows), fetched_at=stamp)

    def test_rolling_252_high_is_separate_from_older_ath(self):
        rows = history()
        rows[40]["close"] = "125"
        rows[252]["close"] = "200"
        result = self.build(rows)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["window_sessions"], 252)
        self.assertEqual(result["current_date"], "2026-10-16")
        self.assertEqual(result["rolling_high_close"], 125)
        self.assertEqual(result["rolling_high_date"],
                         datetime.strptime(rows[40]["date"], "%m/%d/%Y").date().isoformat())
        self.assertAlmostEqual(result["rolling_drawdown_pct"], -20)
        self.assertEqual(result["ath_close"], 200)
        self.assertEqual(result["drawdown_pct"], -50)
        self.collector.validate_payload(result, max_fetch_age_seconds=60, now=NOW)

    def test_rolling_drawdown_retains_threshold_precision(self):
        rows = history(price=10000)
        rows[0]["close"] = "11,137.29"
        rows[10]["close"] = "14,634.72"
        result = self.build(rows)
        expected = (11137.29 / 14634.72 - 1) * 100
        self.assertAlmostEqual(result["rolling_drawdown_pct"], expected, places=12)
        self.assertNotEqual(result["rolling_drawdown_pct"], round(expected, 4))
        self.assertEqual(result["drawdown_pct"], round(expected, 4))

    def test_ndx_and_sox_contracts_cannot_be_mixed(self):
        rows = history()
        for symbol in ("NDX", "SOX"):
            with self.subTest(symbol=symbol):
                collector = IndexCollector(symbol, Path(f"unused-{symbol}.json"))
                result = collector.build_payload(rows, len(rows), fetched_at=NOW)
                self.assertEqual(result["symbol"], symbol)
                collector.validate_payload(result, now=NOW)
                wrong = deepcopy(result)
                wrong["symbol"] = "SOX" if symbol == "NDX" else "NDX"
                with self.assertRaises(ValueError):
                    collector.validate_payload(wrong, now=NOW)

    def test_duplicate_dates_are_rejected_instead_of_counted_as_sessions(self):
        rows = history()
        rows[-1] = deepcopy(rows[-2])
        with self.assertRaises(ValueError):
            self.build(rows)

    def test_short_or_incomplete_history_is_rejected(self):
        short = history(251)
        with self.assertRaises(ValueError):
            self.build(short)
        rows = history(252)
        with self.assertRaises(ValueError):
            self.collector.build_payload(rows, len(rows) + 1, fetched_at=NOW)

    def test_summer_today_row_is_used_only_after_new_york_1615(self):
        rows = history()
        rows[0]["close"] = "200"
        before = datetime(2026, 10, 16, 20, 14, 59, tzinfo=timezone.utc)
        at_cutoff = datetime(2026, 10, 16, 20, 15, tzinfo=timezone.utc)
        early = self.build(rows, before)
        self.assertEqual(early["current_date"], "2026-10-15")
        self.assertEqual(early["current_close"], 100)
        self.assertEqual(early["ath_close"], 100)
        settled = self.build(rows, at_cutoff)
        self.assertEqual(settled["current_date"], "2026-10-16")
        self.assertEqual(settled["current_close"], 200)

    def test_winter_cutoff_follows_new_york_timezone(self):
        rows = history(latest=date(2026, 12, 15))
        before = datetime(2026, 12, 15, 21, 14, 59, tzinfo=timezone.utc)
        at_cutoff = datetime(2026, 12, 15, 21, 15, tzinfo=timezone.utc)
        self.assertEqual(self.build(rows, before)["current_date"], "2026-12-14")
        self.assertEqual(self.build(rows, at_cutoff)["current_date"], "2026-12-15")

    def test_future_history_rows_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(history(latest=date(2026, 10, 19)))

    def test_validator_rejects_inconsistent_rolling_contract(self):
        result = self.build(history())
        for key, value in (("rolling_drawdown_pct", -1), ("window_sessions", 251),
                           ("rolling_high_close", 99), ("schema_version", 1),
                           ("rolling_high_date", "2026-10-18")):
            bad = deepcopy(result)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.collector.validate_payload(bad, now=NOW)

    def test_validator_rejects_stale_fetch_and_stale_price(self):
        result = self.build(history())
        with self.assertRaises(ValueError):
            self.collector.validate_payload(result, max_fetch_age_seconds=60,
                                            now=NOW + timedelta(seconds=61))
        with self.assertRaises(ValueError):
            self.collector.validate_payload(result, now=NOW + timedelta(days=10))

    def test_pagination_early_empty_page_is_rejected(self):
        first = history(2)
        with patch.object(self.collector, "fetch_page", side_effect=[(first, 3), ([], 3)]) as mocked:
            with self.assertRaises(ValueError):
                self.collector.fetch_all()
            self.assertEqual(mocked.call_args_list[1].args, (2,))

    def test_pagination_total_change_is_rejected(self):
        first = history(2)
        remaining = [history(3)[-1]]
        with patch.object(self.collector, "fetch_page", side_effect=[(first, 3), (remaining, 4)]):
            with self.assertRaises(ValueError):
                self.collector.fetch_all()


if __name__ == "__main__":
    unittest.main()
