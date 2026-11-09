"""Contract and boundary tests for the shared official-index data producer."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from index_drawdown import CboeIndexCollector, IndexCollector


NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def history(count=757, latest=date(2026, 10, 16), price=100):
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
        # rows[252] is outside 252 sessions but inside 3 years
        self.assertEqual(result["window_3y_sessions"], 756)
        self.assertEqual(result["high_3y_close"], 200)
        self.assertAlmostEqual(result["drawdown_3y_pct"], -50)
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
        for count in (251, 755):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.build(history(count))
        rows = history(756)
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


def quote(day="Oct 16, 2026", close="90.50", previous="100", status="Closed"):
    """Shape of https://api.nasdaq.com/api/quote/<SYMBOL>/info?assetclass=index."""
    return {"symbol": "NDX", "marketStatus": status,
            "primaryData": {"lastSalePrice": close, "lastTradeTimestamp": day},
            "keyStats": {"previousclose": {"label": "Previous Close:", "value": previous}}}


class QuoteSupplementTests(unittest.TestCase):
    """The historical API can lag the close by hours; the quote API fills the gap."""

    def setUp(self):
        self.collector = IndexCollector("NDX", Path("unused-ndx.json"))
        self.lagging = history(latest=date(2026, 10, 15))  # Oct 16 close missing

    def test_lagging_history_gets_latest_close_from_quote(self):
        row, _ = self.collector.quote_supplement(quote(), self.lagging, NOW)
        self.assertEqual(row, {"date": "10/16/2026", "close": "90.50"})
        result = self.collector.build_payload(self.lagging, len(self.lagging),
                                              fetched_at=NOW, supplement=row)
        self.assertEqual(result["current_date"], "2026-10-16")
        self.assertEqual(result["current_close"], 90.5)
        self.assertEqual(result["current_close_source"], "nasdaq-quote-info")
        self.assertEqual(result["supplemented_records"], 1)
        self.assertAlmostEqual(result["rolling_drawdown_pct"], -9.5)
        # The window still holds exactly 252 sessions including the quote close.
        self.assertEqual(result["window_start_date"],
                         datetime.strptime(self.lagging[250]["date"], "%m/%d/%Y").date().isoformat())
        self.collector.validate_payload(result, max_fetch_age_seconds=60, now=NOW)

    def test_history_that_already_has_the_session_is_left_alone(self):
        rows = history(latest=date(2026, 10, 16))
        row, _ = self.collector.quote_supplement(quote(), rows, NOW)
        self.assertIsNone(row)
        result = self.collector.build_payload(rows, len(rows), fetched_at=NOW)
        self.assertEqual(result["current_close_source"], "nasdaq-historical")
        self.assertEqual(result["supplemented_records"], 0)

    def test_previous_close_mismatch_means_a_session_may_be_missing(self):
        row, _ = self.collector.quote_supplement(quote(previous="101"), self.lagging, NOW)
        self.assertIsNone(row)

    def test_same_day_quote_waits_for_new_york_1615_and_closed_market(self):
        before = datetime(2026, 10, 16, 20, 14, 59, tzinfo=timezone.utc)
        at_cutoff = datetime(2026, 10, 16, 20, 15, tzinfo=timezone.utc)
        self.assertIsNone(self.collector.quote_supplement(quote(), self.lagging, before)[0])
        self.assertIsNotNone(self.collector.quote_supplement(quote(), self.lagging, at_cutoff)[0])
        self.assertIsNone(self.collector.quote_supplement(quote(status="Open"), self.lagging, at_cutoff)[0])

    def test_future_weekend_or_unparseable_quote_dates_are_ignored(self):
        for day in ("Oct 19, 2026", "Oct 17, 2026", "", "16/10/2026"):
            with self.subTest(day=day):
                self.assertIsNone(self.collector.quote_supplement(quote(day=day), self.lagging, NOW)[0])

    def test_quote_with_time_suffix_is_parsed(self):
        row, _ = self.collector.quote_supplement(quote(day="Oct 16, 2026 4:16 PM ET"), self.lagging, NOW)
        self.assertEqual(row["date"], "10/16/2026")

    def test_invalid_quote_price_is_rejected(self):
        with self.assertRaises(ValueError):
            self.collector.quote_supplement(quote(close="--"), self.lagging, NOW)


class PublishHeartbeatTests(unittest.TestCase):
    """Frequent external triggers must not create a commit for every fetch."""

    def setUp(self):
        self.collector = IndexCollector("NDX", Path("unused-ndx.json"))
        rows = history()
        self.old = self.collector.build_payload(rows, len(rows), fetched_at=NOW)
        self.rows = rows

    def fresh(self, minutes):
        later = NOW + timedelta(minutes=minutes)
        return later, self.collector.build_payload(self.rows, len(self.rows), fetched_at=later)

    def test_unchanged_data_is_not_republished_within_the_hour(self):
        later, new = self.fresh(59)
        self.assertTrue(self.collector.heartbeat_only(self.old, new, later))

    def test_unchanged_data_is_republished_after_the_hour(self):
        later, new = self.fresh(60)
        self.assertFalse(self.collector.heartbeat_only(self.old, new, later))

    def test_any_market_data_change_is_published_immediately(self):
        later, new = self.fresh(5)
        new["current_close_source"] = "nasdaq-quote-info"
        self.assertFalse(self.collector.heartbeat_only(self.old, new, later))
        self.assertFalse(self.collector.heartbeat_only(None, new, later))

    def test_main_keeps_file_when_only_fetched_at_moves(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "drawdown.json"
            collector = IndexCollector("NDX", out)
            out.write_text(json.dumps(self.old), encoding="utf-8")
            for minutes, expected_stamp in ((10, self.old["fetched_at"]), (61, None)):
                later = NOW + timedelta(minutes=minutes)
                with patch("index_drawdown.utcnow", return_value=later), \
                     patch.object(collector, "fetch_all", return_value=(self.rows, len(self.rows))), \
                     patch.object(collector, "fetch_quote", side_effect=OSError("quote down")):
                    self.assertEqual(collector.main([]), 0)
                saved = json.loads(out.read_text(encoding="utf-8"))
                with self.subTest(minutes=minutes):
                    if expected_stamp:
                        self.assertEqual(saved["fetched_at"], expected_stamp)
                    else:
                        self.assertEqual(saved["fetched_at"], later.isoformat().replace("+00:00", "Z"))


def cboe_quote(traded="2026-10-16T16:14:59", price=90.4999, previous=100.0001):
    """Shape of https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_SPX.json (data)."""
    # prev_day_close rolls over overnight, so the collector uses current_price - price_change.
    return {"symbol": "^SPX", "security_type": "index", "current_price": price,
            "price_change": price - previous, "close": round(price, 2),
            "prev_day_close": price, "last_trade_time": traded}


class CboeSp500Tests(unittest.TestCase):
    """S&P 500 comes from Cboe because the Nasdaq API has no SPX."""

    def setUp(self):
        self.collector = CboeIndexCollector("SPX", Path("unused-spx.json"))
        self.lagging = history(latest=date(2026, 10, 15))

    def test_spx_contract_is_separate_from_nasdaq_indices(self):
        rows = history()
        result = self.collector.build_payload(rows, len(rows), fetched_at=NOW)
        self.assertEqual(result["symbol"], "SPX")
        self.assertEqual(result["source"], "Cboe delayed quotes")
        self.assertEqual(result["current_close_source"], "cboe-historical")
        self.collector.validate_payload(result, max_fetch_age_seconds=60, now=NOW)
        ndx = IndexCollector("NDX", Path("unused-ndx.json"))
        with self.assertRaises(ValueError):
            ndx.validate_payload(result, now=NOW)
        with self.assertRaises(ValueError):
            IndexCollector("SPX", Path("unused.json"))
        with self.assertRaises(ValueError):
            CboeIndexCollector("NDX", Path("unused.json"))

    def test_cboe_history_rows_are_converted_and_validated(self):
        document = [{"date": "2025-01-02", "volume": "0.0", "open": "0", "high": "1", "low": "1", "close": "5868.550000"}]
        with patch.object(self.collector, "_get", return_value=document * 252):
            rows, total = self.collector.fetch_all()
        self.assertEqual(total, 252)
        self.assertEqual(rows[0], {"date": "01/02/2025", "close": "5868.550000"})
        with patch.object(self.collector, "_get", return_value=document * 251):
            with self.assertRaises(ValueError):
                self.collector.fetch_all()
        bad = [dict(document[0], close=5868.55)] * 252
        with patch.object(self.collector, "_get", return_value=bad):
            with self.assertRaises(ValueError):
                self.collector.fetch_all()

    def test_final_cboe_quote_fills_lagging_history(self):
        row, _ = self.collector.quote_supplement(cboe_quote(), self.lagging, NOW)
        self.assertEqual(row, {"date": "10/16/2026", "close": "90.50"})
        result = self.collector.build_payload(self.lagging, len(self.lagging), fetched_at=NOW, supplement=row)
        self.assertEqual(result["current_date"], "2026-10-16")
        self.assertEqual(result["current_close_source"], "cboe-quote")
        self.collector.validate_payload(result, max_fetch_age_seconds=60, now=NOW)

    def test_cboe_quote_before_final_timestamp_is_not_used(self):
        for traded in ("2026-10-16T16:00:00", "2026-10-16T13:14:59", "not a time", None):
            with self.subTest(traded=traded):
                row, _ = self.collector.quote_supplement(cboe_quote(traded=traded), self.lagging, NOW)
                self.assertIsNone(row)

    def test_cboe_quote_needs_matching_previous_close(self):
        row, _ = self.collector.quote_supplement(cboe_quote(previous=101.0), self.lagging, NOW)
        self.assertIsNone(row)
        current = history(latest=date(2026, 10, 16))
        self.assertIsNone(self.collector.quote_supplement(cboe_quote(), current, NOW)[0])


class ThreeYearHighTests(unittest.TestCase):
    """Policy v3 decides on the highest close of the trailing 3 years (756 sessions)."""

    def setUp(self):
        self.collector = IndexCollector("NDX", Path("unused-ndx.json"))

    def test_three_year_high_forgets_an_older_peak(self):
        rows = history(900)
        rows[800]["close"] = "300"   # older than 3 years: only the all-time high sees it
        rows[500]["close"] = "150"   # within 3 years, outside 252 sessions
        rows[0]["close"] = "90"
        result = self.collector.build_payload(rows, len(rows), fetched_at=NOW)
        self.assertEqual(result["ath_close"], 300)
        self.assertEqual(result["high_3y_close"], 150)
        self.assertEqual(result["rolling_high_close"], 100)
        self.assertAlmostEqual(result["drawdown_3y_pct"], -40)
        self.assertAlmostEqual(result["rolling_drawdown_pct"], -10)
        self.assertEqual(result["window_3y_start_date"],
                         datetime.strptime(rows[755]["date"], "%m/%d/%Y").date().isoformat())
        self.collector.validate_payload(result, max_fetch_age_seconds=60, now=NOW)

    def test_validator_rejects_inconsistent_three_year_fields(self):
        rows = history(900)
        rows[500]["close"] = "150"
        result = self.collector.build_payload(rows, len(rows), fetched_at=NOW)
        for key, value in (("window_3y_sessions", 252), ("high_3y_close", 99), ("high_3y_close", 301),
                           ("drawdown_3y_pct", -10), ("high_3y_date", "2026-10-18"),
                           ("window_3y_start_date", result["window_start_date"])):
            bad = deepcopy(result)
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.collector.validate_payload(bad, now=NOW)


if __name__ == "__main__":
    unittest.main()
