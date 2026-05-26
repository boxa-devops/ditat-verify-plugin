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

    def test_weight_10pct_off_is_critical(self):
        bol = dict(_BOL_OK, weight_lbs=44000)  # +10%
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertEqual(r["verdict"], "ISSUES")
        self.assertTrue(any(f["field"] == "weight_lbs" for f in r["critical"]))

    def test_weight_2pct_off_is_warn(self):
        bol = dict(_BOL_OK, weight_lbs=40800)  # +2%
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertEqual(r["verdict"], "WARN")
        self.assertTrue(any(f["field"] == "weight_lbs" for f in r["warn"]))

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

    def test_zero_pieces_does_not_fall_through(self):
        """Regression: `or` short-circuit on 0 used to leak BOL pieces into RC fallback."""
        rc = dict(_RC_OK, pieces=0)
        pod = dict(_POD_OK, pieces_received=0)
        bol = dict(_BOL_OK, pieces=99)  # would have triggered fallback under `or`
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=bol, pod=pod))
        # 0 vs 0 in POD↔RC should be a match, NOT compared against BOL's 99.
        pod_rc = [f for f in r["info"] if f["pair"] == "POD↔RC" and f["field"] == "pieces_received"]
        # info-level findings are filtered out by _emit unless include_info=True; instead
        # assert there is no critical pieces_received finding.
        self.assertFalse(any(f["field"] == "pieces_received" and f["pair"] == "POD↔RC"
                             for f in r["critical"]))

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
