"""Tests of evaluation arithmetic, failure denominators and fixture invariants.

These test the harness, not model quality; no test-fixture results tune extraction.
"""
import copy
from decimal import Decimal
import unittest

from eval.generate import FIELDS, SEEDS, invoice, rows
from eval.run import align, exact, normalized, percentile, rate, score, summarize


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.gold = invoice("dev", 0)
        self.doc = {"invoices": [self.gold]}

    def test_split_values_are_deterministic_and_isolated(self):
        self.assertEqual(invoice("train", 0), invoice("train", 0))
        ids = {invoice(split, family)["invoice_number"] for split in SEEDS for family in range(6)}
        self.assertEqual(len(ids), 18)
        self.assertEqual(len(set(SEEDS.values())), 3)

    def test_visible_labels_and_wrong_totals(self):
        for split in SEEDS:
            for family in range(6):
                inv = invoice(split, family)
                self.assertEqual(sum(Decimal(item["line_total"]) for item in inv["line_items"]), Decimal(inv["subtotal"]))
                self.assertEqual(Decimal(inv["subtotal"]) + Decimal(inv["tax"]) != Decimal(inv["total"]), family == 2)
                visible = rows(inv)
                self.assertEqual(any(row[0] == "Customer Name" for row in visible), family != 1)

    def test_failure_gets_zero_even_for_missing_gold(self):
        gold = invoice("dev", 1)
        result = score({"invoices": [gold]}, [], False)
        self.assertEqual(sum(value["normalized"] for value in result["fields"].values()), 0)
        self.assertEqual(sum(value["denominator"] for value in result["fields"].values()), 8)
        self.assertEqual(result["counts"]["item_fields"], 8)
        self.assertEqual(result["counts"]["item_normalized"], 0)

    def test_perfect_and_numeric_normalization(self):
        prediction = copy.deepcopy(self.gold)
        prediction["needs_review"] = False
        prediction["total"] = float(prediction["total"])
        result = score(self.doc, [prediction], True)
        self.assertEqual(result["fields"]["total"]["normalized"], 1)
        # JSON scalar exact comparison accepts only JSON-native pipeline values.
        self.assertEqual(normalized("100.00", "total"), normalized(100, "total"))
        self.assertFalse(exact("100.00", 100))

    def test_extra_invoice_penalizes_accuracy(self):
        result = score(self.doc, [self.gold, self.gold], True)
        for field in FIELDS:
            self.assertEqual(result["fields"][field]["denominator"], 2)
            self.assertEqual(result["fields"][field]["normalized"], 1)
        self.assertEqual(result["counts"]["item_rows"], 4)
        self.assertEqual(result["counts"]["row_normalized"], 2)

    def test_invoice_alignment_reorders_only_identifier_matches(self):
        other = invoice("dev", 3)
        pairs = align([self.gold, other], [other, self.gold])
        self.assertEqual([gold["invoice_number"] for gold, _ in pairs], [pred["invoice_number"] for _, pred in pairs])

    def test_extra_line_item_is_penalized(self):
        prediction = copy.deepcopy(self.gold)
        prediction["line_items"].append(prediction["line_items"][0])
        result = score(self.doc, [prediction], True)
        self.assertEqual(result["counts"]["item_fields"], 12)
        self.assertEqual(result["counts"]["item_normalized"], 8)

    def test_unsupported_assertion_and_false_accept(self):
        prediction = copy.deepcopy(self.gold)
        prediction["customer_name"] = "Invented Customer"
        prediction["needs_review"] = False
        result = score(self.doc, [prediction], True)
        self.assertEqual(result["counts"]["unsupported"], 1)
        self.assertTrue(result["false_accept"])
        self.assertFalse(result["labeled_false_accept"])

    def test_labeled_review_and_failure_not_false_accept(self):
        gold = invoice("dev", 2)
        prediction = copy.deepcopy(gold)
        prediction["needs_review"] = False
        self.assertTrue(score({"invoices": [gold]}, [prediction], True)["labeled_false_accept"])
        self.assertFalse(score({"invoices": [gold]}, [], False)["false_accept"])

    def test_wilson_empty_and_percentiles(self):
        self.assertIsNone(rate(0, 0)["rate"])
        self.assertLess(rate(7, 7)["wilson95"][0], 1)
        self.assertGreater(rate(0, 7)["wilson95"][1], 0)
        self.assertEqual(percentile([0, 10], .95), 9.5)
        self.assertIsNone(percentile([], .5))

    def test_corrupt_not_in_valid_denominators(self):
        valid = {"expected_failure": False, "expected_invoices": 1, "success": False, "elapsed_seconds": 1,
                 "expected_units": 1, "score": score(self.doc, [], False)}
        corrupt = {"id": "corrupt.pdf", "expected_failure": True, "success": False, "elapsed_seconds": 9}
        result = summarize([valid, corrupt])
        self.assertEqual(result["failure"]["denominator"], 1)
        self.assertEqual(result["known_corrupt"]["correct_rejection"]["denominator"], 1)
        self.assertEqual(result["latency_seconds_valid_all_attempts"]["sum"], 1)
        self.assertEqual(result["document_macro_scalar_accuracy"]["normalized"]["mean_document_accuracy"], 0)


if __name__ == "__main__":
    unittest.main()
