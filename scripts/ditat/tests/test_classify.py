"""Tests for ditat.classify — the single doc-classification source."""

from __future__ import annotations

import unittest

from ditat import classify


class TestClassifyFilename(unittest.TestCase):

    def test_rate_con_variants(self):
        for name in ("rate-con.pdf", "RateConfirmation_123.pdf", "load RC.pdf", "ratecnf.pdf"):
            self.assertEqual(classify.classify_filename(name), "RC", name)

    def test_pod_variants(self):
        for name in ("POD.pdf", "proof-of-delivery.pdf", "delivery receipt.pdf"):
            self.assertEqual(classify.classify_filename(name), "POD", name)

    def test_bol_variants(self):
        for name in ("BOL.pdf", "bill-of-lading.pdf", "bill_of_lading_99.pdf"):
            self.assertEqual(classify.classify_filename(name), "BOL", name)

    def test_unknown(self):
        self.assertEqual(classify.classify_filename("invoice_4421.pdf"), "UNKNOWN")
        self.assertEqual(classify.classify_filename(None), "UNKNOWN")

    def test_rc_checked_before_bol(self):
        # A filename carrying both tokens resolves to RC (checked first).
        self.assertEqual(classify.classify_filename("RC for bol 12.pdf"), "RC")


class TestClassifyDoc(unittest.TestCase):

    def test_trusts_explicit_classification(self):
        self.assertEqual(classify.classify_doc({"classification": "POD",
                                                 "file_name": "scan.pdf"}), "POD")

    def test_falls_back_to_filename_when_unknown(self):
        self.assertEqual(classify.classify_doc({"classification": "UNKNOWN",
                                                 "file_name": "rate-con.pdf"}), "RC")

    def test_falls_back_when_no_classification(self):
        self.assertEqual(classify.classify_doc({"file_name": "BOL-9.pdf"}), "BOL")


class TestPresenceHelpers(unittest.TestCase):

    def _docs(self):
        return [
            {"classification": "RC", "file_name": "rc.pdf"},
            {"classification": "BOL", "file_name": "bol.pdf"},
        ]

    def test_doc_present(self):
        docs = self._docs()
        self.assertTrue(classify.doc_present(docs, "RC"))
        self.assertTrue(classify.doc_present(docs, "BOL"))
        self.assertFalse(classify.doc_present(docs, "POD"))

    def test_missing_labels_order(self):
        self.assertEqual(classify.missing_labels(self._docs()), ["POD"])
        self.assertEqual(classify.missing_labels([]), ["RC", "BOL", "POD"])


if __name__ == "__main__":
    unittest.main()
