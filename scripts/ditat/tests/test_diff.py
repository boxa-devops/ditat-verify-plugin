"""Tests for ditat.diff.

Run with either:
  python -m unittest ditat.tests.test_diff   (from scripts/)
  pytest scripts/ditat/tests/test_diff.py
"""

from __future__ import annotations

import unittest

from ditat import diff


def _shipment(rc=None, bol=None, pod=None):
    extracted = {}
    if rc is not None:
        extracted["rc"] = rc
    if bol is not None:
        extracted["bol"] = bol
    if pod is not None:
        extracted["pod"] = pod
    return extracted


_DITAT_OK = {
    "bol_number": "BOL-1", "load_number": "LD-1", "equipment_type": "Reefer",
    "total_weight_lbs": 40000, "total_pieces": 20, "total_revenue": 1500.0,
    "commodity": "Frozen Goods",
    "pickup":   {"city": "Dallas",  "state": "TX"},
    "delivery": {"city": "Atlanta", "state": "GA"},
}

_RC_OK = {
    "load_number": "LD-1", "agreed_rate": 1500.0,
    "pickup_date": "2026-05-01", "delivery_date": "2026-05-03",
    "equipment_type": "Reefer",
    "pickup_location":   {"city": "Dallas",  "state": "TX"},
    "delivery_location": {"city": "Atlanta", "state": "GA"},
    "commodity": "Frozen Goods", "weight_lbs": 40000, "pieces": 20,
    "bol_number": "BOL-1",
    # Accessorial policy terms at-or-better than company defaults.
    "detention_rate": 50.0, "detention_free_hrs": 2, "detention_max_hrs": 5,
    "layover_rate": 250.0, "layover_threshold_hrs": 5,
}

_BOL_OK = {
    "bol_number": "BOL-1", "weight_lbs": 40000, "pieces": 20,
    "pickup_date": "2026-05-01", "delivery_date": "2026-05-03",
    "shipper":   {"city": "Dallas",  "state": "TX"},
    "consignee": {"city": "Atlanta", "state": "GA"},
    "commodity": "Frozen Goods",
}

_POD_OK = {
    "bol_number": "BOL-1", "delivery_date": "2026-05-03",
    "signed_by": "J. Doe", "pieces_received": 20,
    "weight_received_lbs": 40000, "damages_notes": None,
}


class TestVerdicts(unittest.TestCase):

    def test_all_match_is_ok(self):
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=_POD_OK))
        self.assertEqual(r["verdict"], "OK")
        self.assertEqual(r["critical_count"], 0)
        self.assertEqual(r["warn_count"], 0)

    def test_no_rc_is_rc_missing(self):
        r = diff.run_diff(_DITAT_OK, _shipment(bol=_BOL_OK, pod=_POD_OK))
        self.assertEqual(r["verdict"], "RC MISSING")

    def test_bol_weight_10pct_over_rc_is_critical(self):
        bol = dict(_BOL_OK, weight_lbs=44000)  # bol > rc by 10%
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertEqual(r["verdict"], "ISSUES")
        self.assertTrue(any(f["field"] == "weight_lbs" and f["pair"] == "BOL↔RC"
                            for f in r["critical"]))

    def test_bol_weight_2pct_over_rc_is_ok(self):
        bol = dict(_BOL_OK, weight_lbs=40800)  # bol > rc by 2% — below 10% threshold
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertEqual(r["verdict"], "OK")

    def test_bol_weight_under_rc_is_ok(self):
        bol = dict(_BOL_OK, weight_lbs=12000)  # bol << rc — under is always ok
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertEqual(r["verdict"], "OK")
        self.assertFalse(any(f["field"] == "weight_lbs" and f["pair"] == "BOL↔RC"
                             for f in r["critical"] + r["warn"]))

    def test_bol_pieces_under_rc_is_ok(self):
        bol = dict(_BOL_OK, pieces=10)  # rc=20, bol<rc
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertFalse(any(f["field"] == "pieces" and f["pair"] == "BOL↔RC"
                             for f in r["critical"] + r["warn"]))

    def test_bol_pieces_over_rc_by_50pct_is_critical(self):
        bol = dict(_BOL_OK, pieces=30)  # rc=20, +50%
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertTrue(any(f["field"] == "pieces" and f["pair"] == "BOL↔RC"
                            for f in r["critical"]))

    def test_delivery_3d_late_is_critical(self):
        pod = dict(_POD_OK, delivery_date="2026-05-06")  # +3d
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=pod))
        self.assertEqual(r["verdict"], "ISSUES")
        self.assertTrue(any(f["field"] == "delivery_date" and "+3d" in f["message"]
                            for f in r["critical"]))

    def test_delivery_1d_late_is_warn(self):
        pod = dict(_POD_OK, delivery_date="2026-05-04")  # +1d
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=pod))
        self.assertEqual(r["verdict"], "WARN")
        self.assertTrue(any(f["field"] == "delivery_date" for f in r["warn"]))

    def test_money_2_dollar_diff_is_critical(self):
        rc = dict(_RC_OK, agreed_rate=1503.00)
        ditat = dict(_DITAT_OK, total_revenue=1500.00)
        r = diff.run_diff(ditat, _shipment(rc=rc, bol=_BOL_OK, pod=_POD_OK))
        self.assertTrue(any(f["field"] == "revenue_vs_rate" for f in r["critical"]))

    def test_bol_number_mismatch_is_critical(self):
        pod = dict(_POD_OK, bol_number="BOL-XXX")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=pod))
        self.assertEqual(r["verdict"], "ISSUES")
        self.assertTrue(any(f["field"] == "bol_number" and f["pair"] == "BOL↔POD"
                            for f in r["critical"]))


    def test_amazon_rc_missing_is_ok(self):
        ditat = dict(_DITAT_OK, customer="Amazon Logistics LLC")
        r = diff.run_diff(ditat, _shipment(bol=_BOL_OK, pod=_POD_OK))
        self.assertEqual(r["verdict"], "OK")

    def test_non_amazon_rc_missing_is_rc_missing(self):
        ditat = dict(_DITAT_OK, customer="Walmart Inc")
        r = diff.run_diff(ditat, _shipment(bol=_BOL_OK, pod=_POD_OK))
        self.assertEqual(r["verdict"], "RC MISSING")

    def test_pod_rc_bol_number_skipped_when_bol_present(self):
        pod = dict(_POD_OK, bol_number="DIFFERENT-BOL")
        rc = dict(_RC_OK, bol_number="BOL-1")
        bol = dict(_BOL_OK, bol_number="BOL-1")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=bol, pod=pod))
        # POD↔RC bol_number must NOT fire — BOL↔POD already catches the discrepancy.
        self.assertFalse(any(f["pair"] == "POD↔RC" and f["field"] == "bol_number"
                             for f in r["critical"] + r["warn"]))
        # BOL↔POD must still catch it.
        self.assertTrue(any(f["pair"] == "BOL↔POD" and f["field"] == "bol_number"
                            for f in r["critical"]))

    def test_pod_rc_weight_and_pieces_dropped(self):
        pod = dict(_POD_OK, weight_received_lbs=10, pieces_received=1)
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=pod))
        self.assertFalse(any(f["pair"] == "POD↔RC" and f["field"] in
                             ("weight_received", "pieces_received")
                             for f in r["critical"] + r["warn"] + r["info"]))

    def test_rc_policy_default_terms_pass(self):
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "RC-policy"
                             for f in r["critical"] + r["warn"]))

    def test_rc_detention_rate_below_default_is_critical(self):
        rc = dict(_RC_OK, detention_rate=35.0)  # 26/05 chat example
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=_POD_OK))
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "detention_rate"
                            for f in r["critical"]))

    def test_rc_detention_free_hrs_over_default_is_critical(self):
        rc = dict(_RC_OK, detention_free_hrs=4)
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=_POD_OK))
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "detention_free_hrs"
                            for f in r["critical"]))

    def test_rc_missing_policy_terms_are_warn(self):
        rc = {k: v for k, v in _RC_OK.items() if not k.startswith(("detention_", "layover_"))}
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=_POD_OK))
        warn_fields = {f["field"] for f in r["warn"] if f["pair"] == "RC-policy"}
        self.assertEqual(warn_fields, {
            "detention_rate", "detention_free_hrs", "detention_max_hrs",
            "layover_rate", "layover_threshold_hrs",
        })

    def test_rc_layover_threshold_over_default_is_warn(self):
        rc = dict(_RC_OK, layover_threshold_hrs=8)
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=_POD_OK))
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "layover_threshold_hrs"
                            for f in r["warn"]))

    def test_damages_emits_warning(self):
        pod = dict(_POD_OK, damages_notes="1 pallet damaged")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=pod))
        self.assertTrue(any(f["field"] == "damages_notes" for f in r["warn"]))


class TestComparators(unittest.TestCase):

    def test_weight_comparator_returns_none_when_both_absent(self):
        self.assertIsNone(diff._cmp_weight(None, None))

    def test_weight_missing_one_side_is_warn(self):
        sev, _ = diff._cmp_weight(None, 100)
        self.assertEqual(sev, "warn")

    def test_id_mismatch_is_critical(self):
        sev, _ = diff._cmp_id("A", "B")
        self.assertEqual(sev, "critical")

    def test_str_fuzzy_match(self):
        sev, msg = diff._cmp_str("Reefer", "Refrigerated Reefer Trailer")
        self.assertEqual(sev, "info")
        self.assertIn("fuzzy", msg)


if __name__ == "__main__":
    unittest.main()
