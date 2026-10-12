import unittest
from datetime import date, datetime
from direct_source import OfficialPageError, parse_official_page
from fetch import (
    DIRECT_FETCH_METHOD, DIRECT_SOURCE_KIND, build_payload,
    validate_payload, SOURCE_KIND, FETCH_METHOD
)

def sample(pe="21.41", pct="36.7", updated="October 09, 2026", asof=None):
    asof = updated if asof is None else asof
    return f"""<html><head><title>NASDAQ-100 Forward PE Ratio</title>
    <script>var ignored = "the current reading ranks in the 1st percentile";</script></head>
    <body><h1>NASDAQ-100 Forward PE Ratio</h1><b>{pe}</b>
    <div>Last Updated:{updated}</div>
    <p>The NASDAQ-100 currently trades at a forward P/E ratio of {pe} as of {asof}.</p>
    <p>Over the past five years, current reading ranks in the 20th percentile.</p>
    <p>Looking at the longer 10-year period, where the median sits at 22.91,
       the current reading ranks in the {pct}th percentile - historically Undervalued.</p>
    <p>The 20-year median ranks in the 62.1th percentile.</p>
    </body></html>"""


class DirectPageTests(unittest.TestCase):
    def test_good_official_page(self):
        self.assertEqual(parse_official_page(sample()), (21.41, 36.7, "2026-10-09"))

    def test_script_injection_ignored(self):
        h = sample().replace("</body>", "<script>Last Updated: October 10, 2026</script></body>")
        self.assertEqual(parse_official_page(h), (21.41, 36.7, "2026-10-09"))

    def test_wrong_page_rejected(self):
        with self.assertRaises(OfficialPageError):
            parse_official_page(sample().replace("NASDAQ-100 Forward PE Ratio", "S&P500 valuation"))

    def test_missing_date_rejected(self):
        with self.assertRaisesRegex(OfficialPageError, "date absent"):
            parse_official_page(sample().replace("Last Updated:October 09, 2026", ""))

    def test_missing_10year_rejected(self):
        with self.assertRaises(OfficialPageError):
            parse_official_page(sample().replace("Looking at the longer 10-year period", "Historical review"))

    def test_mismatched_dates_rejected(self):
        with self.assertRaisesRegex(OfficialPageError, "disagree"):
            parse_official_page(sample(asof="October 08, 2026"))

    def test_old_and_direct_payload_pairs(self):
        now=datetime.now().astimezone()
        today=date.today().isoformat()
        for kind, method in [(SOURCE_KIND, FETCH_METHOD), (DIRECT_SOURCE_KIND, DIRECT_FETCH_METHOD)]:
            with self.subTest(kind=kind):
                payload=build_payload(21.41,36.7,today,fetched_at=now,source_kind=kind,fetch_method=method)
                validate_payload(payload,max_fetch_age_seconds=3600,now=now)

    def test_crossed_method_kind_rejected(self):
        now=datetime.now().astimezone()
        payload=build_payload(21.41,36.7,date.today().isoformat(),fetched_at=now,
                              source_kind=DIRECT_SOURCE_KIND,fetch_method=FETCH_METHOD)
        with self.assertRaisesRegex(ValueError,"source_kind/fetch_method"):
            validate_payload(payload, now=now)

if __name__=="__main__":
    unittest.main()
