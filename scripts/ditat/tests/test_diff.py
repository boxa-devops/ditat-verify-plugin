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


    def test_ditat_zero_weight_pieces_is_warn_not_critical(self):
        # Tenant never enters weight/pieces in Ditat → 0. RC has real values.
        # Should be WARN ("Ditat not entered"), not a critical discrepancy.
        ditat = dict(_DITAT_OK, total_weight_lbs=0, total_pieces=0)
        r = diff.run_diff(ditat, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "Ditat↔RC" and f["field"] in
                             ("total_weight_lbs", "total_pieces")
                             for f in r["critical"]))
        warn_fields = {f["field"] for f in r["warn"] if f["pair"] == "Ditat↔RC"}
        self.assertIn("total_weight_lbs", warn_fields)
        self.assertIn("total_pieces", warn_fields)

    def test_ditat_real_weight_still_compares(self):
        # When Ditat DOES carry weight, a big gap is still critical.
        ditat = dict(_DITAT_OK, total_weight_lbs=20000)  # RC=40000 → 50% off
        r = diff.run_diff(ditat, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=_POD_OK))
        self.assertTrue(any(f["pair"] == "Ditat↔RC" and f["field"] == "total_weight_lbs"
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

    # --- RC-policy: RC terms govern; default only flags silent + POD-detected occurrence ---

    def test_rc_states_terms_no_pod_wait_is_silent(self):
        # RC has terms, POD has no in/out → nothing to detect → no RC-policy finding.
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "RC-policy"
                             for f in r["critical"] + r["warn"]))

    def test_rc_states_low_detention_is_accepted_not_flagged(self):
        # RC states $25/hr (below old floor) AND detention occurred (4h wait).
        # RC governs → accepted, NOT flagged.
        rc = dict(_RC_OK, detention_rate=25.0)
        pod = dict(_POD_OK, arrival_time="08:00", departure_time="12:00")  # 4h
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=pod))
        self.assertFalse(any(f["pair"] == "RC-policy" and f["field"] == "detention"
                             for f in r["critical"] + r["warn"]))

    def test_rc_silent_detention_but_occurred_is_critical(self):
        rc = {k: v for k, v in _RC_OK.items() if not k.startswith("detention_")}
        pod = dict(_POD_OK, arrival_time="08:00", departure_time="12:00")  # 4h > 2 free
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=pod))
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "detention"
                            for f in r["critical"]))

    def test_short_wait_does_not_trigger_detention(self):
        rc = {k: v for k, v in _RC_OK.items() if not k.startswith("detention_")}
        pod = dict(_POD_OK, arrival_time="08:00", departure_time="09:00")  # 1h < 2 free
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=pod))
        self.assertFalse(any(f["pair"] == "RC-policy"
                             for f in r["critical"] + r["warn"]))

    def test_rc_silent_layover_but_long_wait_is_critical(self):
        rc = {k: v for k, v in _RC_OK.items() if not k.startswith("layover_")}
        pod = dict(_POD_OK, arrival_time="08:00", departure_time="14:00")  # 6h ≥ 5 thr
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=pod))
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "layover"
                            for f in r["critical"]))

    def test_pod_in_out_crossing_midnight(self):
        # out before in → crossed midnight → 23:00→02:00 = 3h wait.
        rc = {k: v for k, v in _RC_OK.items() if not k.startswith("detention_")}
        pod = dict(_POD_OK, arrival_time="23:00", departure_time="02:00")  # 3h > 2 free
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=pod))
        self.assertTrue(any(f["pair"] == "RC-policy" and f["field"] == "detention"
                            for f in r["critical"]))

    def test_damages_emits_warning(self):
        pod = dict(_POD_OK, damages_notes="1 pallet damaged")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=_BOL_OK, pod=pod))
        self.assertTrue(any(f["field"] == "damages_notes" for f in r["warn"]))

    def test_bol_rc_bol_number_not_compared(self):
        bol = dict(_BOL_OK, bol_number="AAA")
        rc = dict(_RC_OK, bol_number="ZZZ")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=bol, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "BOL↔RC" and f["field"] == "bol_number"
                             for f in r["critical"] + r["warn"]))

    def test_ditat_rc_equipment_not_compared(self):
        rc = dict(_RC_OK, equipment_type="Box Truck")  # very different from Reefer
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=_BOL_OK, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "Ditat↔RC" and f["field"] == "equipment_type"
                             for f in r["critical"] + r["warn"]))

    def test_commodity_like_shares_word_is_not_flagged(self):
        bol = dict(_BOL_OK, commodity="Coffee closures / food grade packaging")
        rc = dict(_RC_OK, commodity="Packaging Materials")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=bol, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "BOL↔RC" and f["field"] == "commodity"
                             for f in r["warn"]))

    def test_commodity_unrelated_is_warn(self):
        bol = dict(_BOL_OK, commodity="Electronics")
        rc = dict(_RC_OK, commodity="Frozen Beef")
        r = diff.run_diff(_DITAT_OK, _shipment(rc=rc, bol=bol, pod=_POD_OK))
        self.assertTrue(any(f["pair"] == "BOL↔RC" and f["field"] == "commodity"
                            for f in r["warn"]))

    def test_incomplete_pages_is_critical(self):
        bol = dict(_BOL_OK, pages_expected=11, pages_present=1)
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertTrue(any(f["pair"] == "Docs" and f["field"] == "BOL pages"
                            for f in r["critical"]))

    def test_complete_pages_no_flag(self):
        bol = dict(_BOL_OK, pages_expected=2, pages_present=2)
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "Docs" and f["field"] == "BOL pages"
                             for f in r["critical"]))

    def test_is_skipped_customer_amazon(self):
        self.assertTrue(diff.is_skipped_customer({"customer": "Amazon Logistics"},
                                                 diff.default_rules()))
        self.assertFalse(diff.is_skipped_customer({"customer": "Walmart Inc"},
                                                  diff.default_rules()))


from datetime import date  # noqa: E402

_AS_OF = date(2026, 6, 10)


def _delivered(deliv="2026-06-05", **kw):
    """Ditat record with a delivery appointment date (default in the past)."""
    d = dict(_DITAT_OK, **kw)
    d["delivery"] = {"city": "Atlanta", "state": "GA",
                     "appointment_to": f"{deliv}T10:00:00.000Z"}
    return d


class TestDeliveryAndCompleteness(unittest.TestCase):

    def test_is_pending_future_delivery(self):
        self.assertTrue(diff.is_pending(_delivered("2026-06-20"), _AS_OF))
        self.assertFalse(diff.is_pending(_delivered("2026-06-01"), _AS_OF))
        # No delivery date → unknown → not treated as pending.
        self.assertFalse(diff.is_pending(_DITAT_OK, _AS_OF))

    def test_delivered_missing_bol_is_critical(self):
        ext = {"rc": _RC_OK, "pod": _POD_OK, "docs_missing": ["BOL"]}
        r = diff.run_diff(_delivered(), ext, as_of=_AS_OF)
        self.assertEqual(r["verdict"], "ISSUES")
        self.assertTrue(any(f["pair"] == "Docs" and f["field"] == "BOL"
                            for f in r["critical"]))

    def test_delivered_missing_pod_is_critical(self):
        ext = {"rc": _RC_OK, "bol": _BOL_OK, "docs_missing": ["POD"]}
        r = diff.run_diff(_delivered(), ext, as_of=_AS_OF)
        self.assertTrue(any(f["pair"] == "Docs" and f["field"] == "POD"
                            for f in r["critical"]))

    def test_delivered_missing_rc_is_critical(self):
        ext = {"bol": _BOL_OK, "pod": _POD_OK, "docs_missing": ["RC"]}
        r = diff.run_diff(_delivered(customer="Walmart"), ext, as_of=_AS_OF)
        self.assertEqual(r["verdict"], "ISSUES")
        self.assertTrue(any(f["pair"] == "Docs" and f["field"] == "RC"
                            for f in r["critical"]))

    def test_delivered_missing_rc_amazon_exempt(self):
        ext = {"bol": _BOL_OK, "pod": _POD_OK, "docs_missing": ["RC"]}
        r = diff.run_diff(_delivered(customer="Amazon Logistics"), ext, as_of=_AS_OF)
        self.assertFalse(any(f["pair"] == "Docs" and f["field"] == "RC"
                             for f in r["critical"]))

    def test_pending_shipment_docs_not_required(self):
        # Future delivery → completeness must NOT fire even with missing docs.
        ext = {"rc": _RC_OK, "docs_missing": ["BOL", "POD"]}
        r = diff.run_diff(_delivered("2026-06-20"), ext, as_of=_AS_OF)
        self.assertFalse(any(f["pair"] == "Docs" for f in r["critical"]))

    def test_no_as_of_skips_completeness(self):
        ext = {"rc": _RC_OK, "docs_missing": ["BOL", "POD"]}
        r = diff.run_diff(_delivered(), ext)  # as_of omitted
        self.assertFalse(any(f["pair"] == "Docs" for f in r["critical"]))

    def test_completed_status_requires_docs_no_date_needed(self):
        # Status=Completed is authoritative — missing docs critical even without as_of.
        ditat = dict(_DITAT_OK, status="Completed")
        ext = {"rc": _RC_OK, "docs_missing": ["BOL", "POD"]}
        r = diff.run_diff(ditat, ext)  # no as_of, no delivery date
        self.assertTrue(any(f["pair"] == "Docs" and f["field"] == "BOL"
                            for f in r["critical"]))

    def test_cancelled_status_exempt_from_doc_completeness(self):
        ditat = dict(_DITAT_OK, status="Cancelled")
        ext = {"rc": _RC_OK, "docs_missing": ["BOL", "POD"]}
        r = diff.run_diff(ditat, ext, as_of=_AS_OF)
        self.assertFalse(any(f["pair"] == "Docs" for f in r["critical"]))

    def test_terminal_status_never_pending(self):
        # Cancelled with a future delivery date is still terminal (not pending).
        ditat = dict(_delivered("2026-06-20"), status="Cancelled")
        self.assertFalse(diff.is_pending(ditat, _AS_OF))
        completed = dict(_delivered("2026-06-20"), status="Completed")
        self.assertFalse(diff.is_pending(completed, _AS_OF))


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

    def test_date_missing_one_side_not_flagged(self):
        # A date present on only one doc is not a discrepancy.
        self.assertIsNone(diff._cmp_date("2026-05-01", None))
        self.assertIsNone(diff._cmp_date(None, "2026-05-01"))

    def test_bol_rc_delivery_date_missing_on_bol_no_warn(self):
        bol = {k: v for k, v in _BOL_OK.items() if k != "delivery_date"}
        r = diff.run_diff(_DITAT_OK, _shipment(rc=_RC_OK, bol=bol, pod=_POD_OK))
        self.assertFalse(any(f["pair"] == "BOL↔RC" and f["field"] == "delivery_date"
                             for f in r["critical"] + r["warn"]))


if __name__ == "__main__":
    unittest.main()
