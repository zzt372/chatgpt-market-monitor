from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest

import alerts
from alerts import evaluate_observation


NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)


def observation(drawdown, day="2026-10-16", symbol="NDX", *, high=100, ath=None):
    """Producer payload; the decision uses the trailing 3-year high (policy v3)."""
    current = float(Decimal(str(high)) * (1 + Decimal(str(drawdown)) / 100))
    ath = max(high, 100) if ath is None else ath
    return {
        "schema_version": 2,
        "symbol": symbol,
        "ok": True,
        "current_date": day,
        "current_close": current,
        "window_sessions": 252,
        "rolling_high_close": high,
        "rolling_high_date": "2026-09-01",
        "rolling_drawdown_pct": drawdown,
        "window_3y_sessions": 756,
        "high_3y_close": high,
        "high_3y_date": "2026-09-01",
        "drawdown_3y_pct": drawdown,
        "ath_close": ath,
        "ath_date": "2026-01-02",
        "drawdown_pct": round((current / ath - 1) * 100, 4),
        "fetched_at": NOW.isoformat(),
    }


def legacy_state(version, symbol="NDX", notified=(-10,)):
    """A state saved under policy v1/v2 (252-session or all-time-high levels)."""
    state = {
        "schema_version": version,
        "policy_version": version,
        "symbol": symbol,
        "episode_id": 1,
        "episode_active": True,
        "notified_thresholds_pct": list(notified),
        "last_observation_date": "2026-10-15",
        "last_drawdown_pct": -14.7,
    }
    if version == 2:
        state.update(last_rolling_high_close=100, recovery_target_close=None)
    return state


class AlertTests(unittest.TestCase):
    def evaluate(self, data, state=None):
        return evaluate_observation(data, state, now=NOW)

    def test_baseline_suppresses_preexisting_stages(self):
        result = self.evaluate(observation(-44))
        self.assertEqual(result["status"], "baseline")
        self.assertIsNone(result["notification"])
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-20, -30, -40])
        later = self.evaluate(observation(-52, "2026-10-17"), result["state"])
        self.assertEqual(later["notification"]["threshold_pct"], -50)

    def test_first_new_stage_after_baseline(self):
        baseline = self.evaluate(observation(-18))["state"]
        result = self.evaluate(observation(-20, "2026-10-17"), baseline)
        self.assertEqual(result["notification"]["threshold_pct"], -20)
        self.assertEqual(result["state"]["episode_id"], 1)

    def test_binary_float_price_ratio_does_not_miss_exact_threshold(self):
        baseline = self.evaluate(observation(-18))["state"]
        data = observation(-20, "2026-10-17")
        data["drawdown_3y_pct"] = (80 / 100 - 1) * 100
        result = self.evaluate(data, baseline)
        self.assertEqual(result["notification"]["threshold_pct"], -20)

    def test_partial_recovery_does_not_rearm(self):
        initial = self.evaluate(observation(-32, "2026-10-14"))["state"]
        recovered = self.evaluate(observation(-16, "2026-10-15"), initial)["state"]
        recross = self.evaluate(observation(-32, "2026-10-16"), recovered)
        self.assertIsNone(recross["notification"])
        self.assertEqual(recross["state"]["episode_id"], initial["episode_id"])

    def test_reset_is_inclusive_and_next_episode_can_alert(self):
        initial = self.evaluate(observation(-32, "2026-10-14"))["state"]
        reset = self.evaluate(observation(-5, "2026-10-15"), initial)
        self.assertEqual(reset["status"], "reset")
        self.assertEqual(reset["state"]["notified_thresholds_pct"], [])
        result = self.evaluate(observation(-32, "2026-10-16"), reset["state"])
        self.assertEqual(result["notification"]["threshold_pct"], -30)
        self.assertEqual(result["state"]["episode_id"], 2)

    def test_flat_price_and_expiring_high_require_real_recovery(self):
        initial = self.evaluate(observation(-20, "2026-10-14"))["state"]
        result = self.evaluate(observation(0, "2026-10-15", high=90), initial)
        self.assertEqual(result["status"], "recovery_pending")
        self.assertIsNone(result["notification"])
        self.assertEqual(result["state"]["recovery_target_close"], 94.5)
        self.assertEqual(result["state"]["last_high_3y_close"], 90)
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-20])
        self.assertTrue(result["state"]["episode_active"])

    def test_past_rebound_does_not_allow_window_expiry_alone_to_reset(self):
        initial = self.evaluate(observation(-50, "2026-10-12"))["state"]
        rebounded = self.evaluate(observation(-30, "2026-10-13"), initial)["state"]
        result = self.evaluate(observation(0, "2026-10-14", high=70), rebounded)
        self.assertEqual(result["status"], "recovery_pending")
        self.assertEqual(result["state"]["recovery_target_close"], 73.5)
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-20, -30, -40, -50])
        self.assertEqual(result["state"]["episode_id"], initial["episode_id"])

    def test_pending_recovery_target_does_not_fall_with_window_high(self):
        initial = self.evaluate(observation(-20, "2026-10-14"))["state"]
        pending = self.evaluate(observation(0, "2026-10-15", high=90), initial)["state"]
        result = self.evaluate(observation(-1, "2026-10-16", high=89), pending)
        self.assertEqual(result["state"]["recovery_target_close"], 94.5)
        self.assertEqual(result["state"]["last_high_3y_close"], 89)
        self.assertTrue(result["state"]["episode_active"])
        self.assertIsNone(result["notification"])

    def test_pending_reset_requires_full_five_percent_recovery(self):
        initial = self.evaluate(observation(-20, "2026-10-14"))["state"]
        pending = self.evaluate(observation(0, "2026-10-15", high=90), initial)["state"]
        almost = self.evaluate(observation(0, "2026-10-16", high=94.4999), pending)
        self.assertTrue(almost["state"]["episode_active"])
        self.assertEqual(almost["state"]["recovery_target_close"], 94.5)
        result = self.evaluate(observation(0, "2026-10-17", high=94.5), almost["state"])
        self.assertEqual(result["status"], "reset")
        self.assertFalse(result["state"]["episode_active"])
        self.assertEqual(result["state"]["notified_thresholds_pct"], [])
        self.assertIsNone(result["state"]["recovery_target_close"])

    def test_leaving_recovery_zone_clears_target_but_keeps_history(self):
        initial = self.evaluate(observation(-20, "2026-10-14"))["state"]
        pending = self.evaluate(observation(0, "2026-10-15", high=90), initial)["state"]
        fallen = self.evaluate(observation(-12, "2026-10-16", high=90), pending)
        self.assertIsNone(fallen["state"]["recovery_target_close"])
        self.assertTrue(fallen["state"]["episode_active"])
        self.assertEqual(fallen["state"]["notified_thresholds_pct"], [-20])
        self.assertIsNone(fallen["notification"])
        recovered = self.evaluate(observation(-5, "2026-10-17", high=90), fallen["state"])
        self.assertEqual(recovered["status"], "reset")
        self.assertFalse(recovered["state"]["episode_active"])

    def test_reaching_pending_price_without_recovery_zone_does_not_reset(self):
        initial = self.evaluate(observation(-20, "2026-10-14"))["state"]
        pending = self.evaluate(observation(0, "2026-10-15", high=90), initial)["state"]
        result = self.evaluate(observation(-10, "2026-10-16", high=105), pending)
        self.assertEqual(result["state"]["last_drawdown_pct"], -10)
        self.assertTrue(result["state"]["episode_active"])
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-20])
        self.assertIsNone(result["state"]["recovery_target_close"])
        self.assertIsNone(result["notification"])

    def test_v2_state_is_rebaselined_once_without_delivery(self):
        old = legacy_state(2, notified=(-10,))
        original = deepcopy(old)
        result = self.evaluate(observation(-24, "2026-10-16"), old)
        self.assertEqual(result["status"], "migrated")
        self.assertIsNone(result["notification"])
        self.assertEqual(result["state"]["schema_version"], 3)
        self.assertEqual(result["state"]["policy_version"], 3)
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-20])
        self.assertEqual(result["state"]["last_high_3y_close"], 100)
        self.assertNotIn("last_rolling_high_close", result["state"])
        self.assertEqual(old, original)
        later = self.evaluate(observation(-31, "2026-10-17"), result["state"])
        self.assertEqual(later["notification"]["threshold_pct"], -30)

    def test_sox_v2_history_maps_to_reached_v3_levels_only(self):
        old = legacy_state(2, "SOX", notified=(-20, -25, -30, -35))
        result = self.evaluate(observation(-35.84, symbol="SOX"), old)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-30])
        later = self.evaluate(observation(-41, "2026-10-17", "SOX"), result["state"])
        self.assertEqual(later["notification"]["threshold_pct"], -40)

    def test_v1_state_is_rebaselined_too(self):
        result = self.evaluate(observation(-5.65), legacy_state(1, notified=()))
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["state"]["notified_thresholds_pct"], [])
        self.assertFalse(result["state"]["episode_active"])

    def test_migration_never_crosses_indices(self):
        with self.assertRaisesRegex(ValueError, "state symbol mismatch"):
            self.evaluate(observation(-35, symbol="SOX"), legacy_state(2, "NDX"))

    def test_notification_contains_both_price_references_and_past_bottoms(self):
        initial = self.evaluate(observation(-18))["state"]
        data = observation(-20, "2026-10-17", ath=123)
        result = self.evaluate(data, initial)
        notice = result["notification"]
        self.assertEqual(notice["current_close"], 80)
        self.assertEqual(notice["high_3y_close"], 100)
        self.assertEqual(notice["high_3y_date"], data["high_3y_date"])
        self.assertEqual(notice["drawdown_3y_pct"], -20)
        self.assertEqual(notice["drawdown_pct"], notice["drawdown_3y_pct"])
        self.assertEqual(notice["ath_close"], 123)
        self.assertEqual(notice["ath_date"], data["ath_date"])
        expected = float((Decimal(80) / Decimal(123) - 1) * 100)
        self.assertAlmostEqual(notice["ath_drawdown_pct"], expected, places=12)
        self.assertNotEqual(notice["ath_drawdown_pct"], data["drawdown_pct"])
        self.assertEqual(notice["reference_bottoms_pct"],
                         [["2000-02", -82.9], ["2008", -53.7], ["2020", -28.0], ["2022", -35.6]])

    def test_missing_or_invalid_ath_data_is_rejected_without_mutation(self):
        initial = self.evaluate(observation(-24))["state"]
        original = deepcopy(initial)
        for missing in ("ath_close", "ath_date", "drawdown_pct"):
            bad = observation(-32, "2026-10-17")
            del bad[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.evaluate(bad, initial)
            self.assertEqual(initial, original)
        for key, value in (("ath_close", 99), ("ath_close", 0),
                           ("ath_close", True), ("ath_close", float("nan")),
                           ("ath_date", "2026-10-18"), ("ath_date", "bad-date"),
                           ("drawdown_pct", -99), ("drawdown_pct", True)):
            bad = observation(-32, "2026-10-17")
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.evaluate(bad, initial)
            self.assertEqual(initial, original)

    def test_gap_through_all_levels_emits_only_deepest_stage(self):
        initial = self.evaluate(observation(-18))["state"]
        result = self.evaluate(observation(-52, "2026-10-17"), initial)
        self.assertEqual(result["notification"]["threshold_pct"], -50)
        self.assertEqual(result["notification"]["newly_crossed_thresholds_pct"],
                         [-20, -30, -40, -50])
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-20, -30, -40, -50])
        deeper = self.evaluate(observation(-80, "2026-10-18"), result["state"])
        self.assertIsNone(deeper["notification"])

    def test_same_or_older_observation_never_advances_or_resets_state(self):
        initial = self.evaluate(observation(-32))["state"]
        for data in (observation(-60), observation(-1, "2026-10-15")):
            with self.subTest(data=data):
                result = self.evaluate(data, initial)
                self.assertEqual(result["status"], "ignored")
                self.assertEqual(result["state"], initial)
                self.assertIsNone(result["notification"])

    def test_bad_data_does_not_mutate_existing_state(self):
        initial = self.evaluate(observation(-24))["state"]
        before = deepcopy(initial)
        for field, value in (("ok", False), ("schema_version", 1),
                             ("window_3y_sessions", 252), ("drawdown_3y_pct", -60),
                             ("current_close", float("nan")), ("current_close", True),
                             ("high_3y_close", 79), ("high_3y_date", "2026-10-18")):
            bad = observation(-32, "2026-10-17")
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.evaluate(bad, initial)
            self.assertEqual(initial, before)

    def test_stale_fetch_is_rejected(self):
        data = observation(-20)
        data["fetched_at"] = (NOW - timedelta(hours=4)).isoformat()
        with self.assertRaisesRegex(ValueError, "fetched_at is stale"):
            self.evaluate(data)

    def test_recently_refetched_stale_price_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "current_date is stale"):
            self.evaluate(observation(-20, "2026-10-01"))

    def test_future_or_timezone_free_timestamps_are_rejected(self):
        for stamp in ((NOW + timedelta(hours=1)).isoformat(), "2026-10-18T08:00:00"):
            data = observation(-20)
            data["fetched_at"] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.evaluate(data)
        with self.assertRaisesRegex(ValueError, "current_date is in the future"):
            self.evaluate(observation(-20, "2026-10-19"))

    def test_indices_have_independent_levels_and_reject_mixed_state(self):
        ndx = self.evaluate(observation(-18))["state"]
        sox = self.evaluate(observation(-29, symbol="SOX"))["state"]
        result = self.evaluate(observation(-34, "2026-10-17", "SOX"), sox)
        self.assertEqual(result["notification"]["threshold_pct"], -30)
        self.assertEqual(ndx["notified_thresholds_pct"], [])
        with self.assertRaisesRegex(ValueError, "state symbol mismatch"):
            self.evaluate(observation(-34, "2026-10-17", "SOX"), ndx)

    def test_sp500_is_judged_independently_with_its_own_levels(self):
        spx = self.evaluate(observation(-5.65, symbol="SPX"))
        self.assertEqual(spx["status"], "baseline")
        self.assertEqual(spx["state"]["notified_thresholds_pct"], [])
        result = self.evaluate(observation(-15.2, "2026-10-17", "SPX"), spx["state"])
        self.assertEqual(result["notification"]["symbol"], "SPX")
        self.assertEqual(result["notification"]["threshold_pct"], -15)
        self.assertEqual(result["state"]["notified_thresholds_pct"], [-15])
        ndx = self.evaluate(observation(-9))["state"]
        with self.assertRaisesRegex(ValueError, "state symbol mismatch"):
            self.evaluate(observation(-16, "2026-10-17", "SPX"), ndx)

    def test_nikkei_has_its_own_levels_state_and_past_bottoms(self):
        n225 = self.evaluate(observation(-18.78, symbol="N225"))
        self.assertEqual(n225["status"], "baseline")
        self.assertEqual(n225["state"]["notified_thresholds_pct"], [])
        result = self.evaluate(observation(-20.6, "2026-10-17", "N225"), n225["state"])
        self.assertEqual(result["notification"]["symbol"], "N225")
        self.assertEqual(result["notification"]["threshold_pct"], -20)
        self.assertEqual(result["notification"]["reference_bottoms_pct"][0], ["1990-92", -63.2])
        ndx = self.evaluate(observation(-18))["state"]
        with self.assertRaisesRegex(ValueError, "state symbol mismatch"):
            self.evaluate(observation(-21, "2026-10-17", "N225"), ndx)

    def test_unrounded_sox_drawdown_does_not_cross_40_early(self):
        initial = self.evaluate(observation(-31, "2026-10-14", "SOX"))["state"]
        almost = self.evaluate(observation(-39.991666, "2026-10-15", "SOX"), initial)
        self.assertIsNone(almost["notification"])
        exact = self.evaluate(observation(-40, "2026-10-16", "SOX"), almost["state"])
        self.assertEqual(exact["notification"]["threshold_pct"], -40)

    def test_corrupt_state_is_not_silently_treated_as_first_run(self):
        initial = self.evaluate(observation(-24))["state"]
        for key, value in (("policy_version", 99), ("schema_version", 99),
                           ("notified_thresholds_pct", [-30]), ("episode_active", False),
                           ("episode_id", -1), ("last_high_3y_close", 0)):
            bad = deepcopy(initial)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.evaluate(observation(-32, "2026-10-17"), bad)

    def test_decisions_are_pure_and_policy_publishes_v3_contract(self):
        payload = observation(-18)
        original_payload = deepcopy(payload)
        initial = self.evaluate(payload)["state"]
        original_state = deepcopy(initial)
        result = self.evaluate(observation(-40, "2026-10-17"), initial)
        result["state"]["notified_thresholds_pct"].clear()
        self.assertEqual(initial, original_state)
        self.assertEqual(payload, original_payload)
        policy = json.loads(Path(__file__).with_name("policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["policy_version"], alerts.POLICY_VERSION)
        self.assertEqual(policy["state_schema_version"], alerts.STATE_VERSION)
        self.assertEqual(policy["reference"]["field"], "drawdown_3y_pct")
        self.assertEqual(policy["reference"]["window_sessions"], alerts.WINDOW_SESSIONS)
        self.assertEqual(policy["thresholds_pct"],
                         {"NDX": [-20, -30, -40, -50], "SPX": [-15, -20, -30, -40],
                          "SOX": [-30, -40, -50, -60], "N225": [-20, -30, -40, -50]})
        self.assertEqual(policy["thresholds_pct"],
                         {k: list(v) for k, v in alerts.THRESHOLDS_PCT.items()})
        self.assertEqual(policy["reference_bottoms_pct"],
                         {k: [list(x) for x in v] for k, v in alerts.REFERENCE_BOTTOMS_PCT.items()})
        self.assertEqual(policy["episode_reset_drawdown_pct"], alerts.RESET_DRAWDOWN_PCT)
        self.assertFalse(policy["fear_greed"]["required_for_drawdown_alert"])
        self.assertFalse(policy["forward_pe"]["enabled"])


if __name__ == "__main__":
    unittest.main()
