"""Tests for ditat.rules — load/merge + that overrides reach the diff."""

from __future__ import annotations

import unittest

from ditat import diff
from ditat.rules import DEFAULTS, _deep_merge, default_rules, load_rules


class TestMerge(unittest.TestCase):

    def test_missing_file_returns_defaults(self):
        self.assertEqual(load_rules("/no/such/rules.yaml"), default_rules())

    def test_deep_merge_overrides_nested_only(self):
        merged = _deep_merge(DEFAULTS, {"money": {"critical_abs": 99.0}})
        self.assertEqual(merged["money"]["critical_abs"], 99.0)
        # Sibling key untouched.
        self.assertEqual(merged["money"]["critical_pct"], DEFAULTS["money"]["critical_pct"])
        # Unrelated top-level untouched.
        self.assertEqual(merged["date"], DEFAULTS["date"])

    def test_default_rules_is_a_copy(self):
        r = default_rules()
        r["money"]["critical_abs"] = 1234.0
        self.assertNotEqual(DEFAULTS["money"]["critical_abs"], 1234.0)


_DITAT = {
    "bol_number": "BOL-1", "load_number": "LD-1", "equipment_type": "Reefer",
    "total_weight_lbs": 40000, "total_pieces": 20, "total_revenue": 1500.0,
    "commodity": "Frozen Goods",
    "pickup": {"city": "Dallas", "state": "TX"},
    "delivery": {"city": "Atlanta", "state": "GA"},
}
_RC = {
    "load_number": "LD-1", "agreed_rate": 1500.0,
    "pickup_date": "2026-05-01", "delivery_date": "2026-05-03",
    "equipment_type": "Reefer",
    "pickup_location": {"city": "Dallas", "state": "TX"},
    "delivery_location": {"city": "Atlanta", "state": "GA"},
    "commodity": "Frozen Goods", "weight_lbs": 40000, "pieces": 20,
    "bol_number": "BOL-1",
    "detention_rate": 50.0, "detention_free_hrs": 2, "detention_max_hrs": 5,
    "layover_rate": 250.0, "layover_threshold_hrs": 5,
}


class TestRulesAffectDiff(unittest.TestCase):

    def test_custom_free_hours_changes_detention_detection(self):
        # RC silent on detention; POD shows a 4h wait. With default free=2h it's
        # detention (critical); raise free to 5h and the same 4h wait is within
        # free time → no finding. Proves the rule drives detection.
        rc = {k: v for k, v in _RC.items() if not k.startswith("detention_")}
        pod = {"arrival_time": "08:00", "departure_time": "12:00"}  # 4h
        crit = diff.run_diff(_DITAT, {"rc": rc, "pod": pod})  # default free=2
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "detention"
                            for f in crit["critical"]))
        lax = _deep_merge(DEFAULTS, {"accessorial": {"detention_free_hrs": 5.0}})
        r = diff.run_diff(_DITAT, {"rc": rc, "pod": pod}, lax)
        self.assertFalse(any(f["pair"] == "RC-policy" and f["field"] == "detention"
                             for f in r["critical"] + r["warn"]))

    def test_custom_ok_customer_list(self):
        ditat = dict(_DITAT, customer="Walmart Inc")
        rules = _deep_merge(DEFAULTS, {"rc_missing_ok_customers": ["walmart"]})
        r = diff.run_diff(ditat, {"bol": {"bol_number": "BOL-1"}}, rules)
        self.assertEqual(r["verdict"], "OK")

    def test_default_amazon_still_ok_without_file(self):
        ditat = dict(_DITAT, customer="Amazon Logistics")
        r = diff.run_diff(ditat, {"bol": {"bol_number": "BOL-1"}})
        self.assertEqual(r["verdict"], "OK")


if __name__ == "__main__":
    unittest.main()
