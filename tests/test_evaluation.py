"""Metrics tests with constructed reports, not model-performance claims."""
from copy import deepcopy
import unittest

from bug_hunter.evaluation import evaluate
from bug_hunter.jev import MODEL


class EvaluationTests(unittest.TestCase):
    def case(self, ident="case1", bugs=None):
        return {"id": ident, "source_sha256": "a" * 64, "line_count": 10,
                "bug_ranges": [[4, 5]] if bugs is None else bugs}

    def report(self, spans=(), status="complete"):
        return {"model": MODEL, "source": {"sha256": "a" * 64, "line_count": 10},
                "status": status, "configuration": {"relation_threshold": "0.70"},
                "question_pack_sha256": "b" * 64,
                "findings": [{"start_line": a, "end_line": b} for a, b in spans],
                "usage": {"input_tokens": 10, "output_tokens": 2, "attempts_with_unknown_usage": 1}}

    def suite(self, *cases):
        return {"schema_version": 1, "partition": "heldout", "cases": list(cases)}

    def test_broad_detection_does_not_count_as_exact_localization(self):
        result = evaluate(self.suite(self.case()), {"case1": self.report([(1, 10)])})
        self.assertEqual(result["counts"]["covered_bugs"], 1)
        self.assertEqual(result["counts"]["exact_localizations"], 0)
        self.assertEqual(result["counts"]["unknown_usage_attempts"], 1)
        self.assertEqual(result["live_api_calls"], 0)

    def test_incomplete_and_missing_are_not_clears_or_completed_misses(self):
        suite = self.suite(self.case("bug"), self.case("valid", []), self.case("missing", []))
        result = evaluate(suite, {"bug": self.report(status="incomplete"), "valid": self.report(status="incomplete")})
        self.assertEqual(result["counts"]["valid_cases_cleared"], 0)
        self.assertEqual(result["counts"]["valid_cases_unresolved"], 2)
        self.assertEqual(result["counts"]["unresolved_expected_bugs"], 1)
        self.assertEqual(result["counts"]["complete_run_misses"], 0)

    def test_complete_misses_and_valid_flags_are_counted(self):
        result = evaluate(self.suite(self.case("bug"), self.case("valid", [])),
                          {"bug": self.report(), "valid": self.report([(2, 2)])})
        self.assertEqual(result["counts"]["complete_run_misses"], 1)
        self.assertEqual(result["counts"]["valid_cases_flagged"], 1)

    def test_stale_snapshot_and_threshold_changes_are_rejected(self):
        report = self.report()
        for field, value in (("source", {"sha256": "c" * 64, "line_count": 10}),
                             ("configuration", {"relation_threshold": ".69"})):
            modified = deepcopy(report)
            modified[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evaluate(self.suite(self.case()), {"case1": modified})

    def test_labels_must_be_frozen_and_case_ids_unique(self):
        for suite in ({"schema_version": 1, "partition": "development", "cases": [self.case()]},
                      self.suite(self.case(), self.case())):
            with self.assertRaises(ValueError):
                evaluate(suite, {})


if __name__ == "__main__":
    unittest.main()
