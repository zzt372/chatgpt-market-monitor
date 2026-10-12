import unittest
from datetime import datetime, timezone, timedelta
from fetch import build_payload, SOURCE_KIND, FETCH_METHOD
from publish import good_remote


class ConcurrentPublishTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.candidate=build_payload(21.41,36.7,self.now.date().isoformat(),fetched_at=self.now)

    def test_fresher_remote_wins(self):
        remote=build_payload(21.42,37.0,self.now.date().isoformat(),fetched_at=self.now+timedelta(minutes=1))
        self.assertTrue(good_remote(remote,self.candidate))

    def test_identical_recent_remote_skips_commit(self):
        remote=build_payload(21.41,36.7,self.now.date().isoformat(),fetched_at=self.now-timedelta(minutes=10))
        self.assertTrue(good_remote(remote,self.candidate))

    def test_old_remote_is_replaced(self):
        remote=build_payload(21.41,36.7,self.now.date().isoformat(),fetched_at=self.now-timedelta(minutes=30))
        self.assertFalse(good_remote(remote,self.candidate))

    def test_different_values_recent_remote_not_silently_accepted(self):
        remote=build_payload(21.1,32.0,self.now.date().isoformat(),fetched_at=self.now-timedelta(minutes=10))
        self.assertFalse(good_remote(remote,self.candidate))

    def test_remote_future_date_wins(self):
        remote=build_payload(21.42,37.0,(self.now+timedelta(days=1)).date().isoformat(),fetched_at=self.now)
        self.assertTrue(good_remote(remote,self.candidate))

    def test_bad_method_pair_does_not_win(self):
        remote=build_payload(21.42,37.0,self.now.date().isoformat(),fetched_at=self.now+timedelta(minutes=1),
                             source_kind=SOURCE_KIND,fetch_method="trendonify-direct")
        self.assertFalse(good_remote(remote,self.candidate))

    def test_corrupted_remote_does_not_win(self):
        remote=dict(self.candidate,source="unrelated")
        self.assertFalse(good_remote(remote,self.candidate))


if __name__=="__main__":
    unittest.main()
