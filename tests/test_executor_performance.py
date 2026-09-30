"""Wire/report preservation at executor bookkeeping boundaries; no live Jev."""
from decimal import Decimal
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from bug_hunter.core import Config, Span
from bug_hunter.engine import Search
from bug_hunter.questions import choice, noul, pack_questions
from tests.support import FixtureProvider, source


class ExecutorBookkeepingTests(unittest.TestCase):
    def test_issue_dedup_reads_current_resolution_and_preserves_scoped_rows(self):
        search = Search(source(2), FixtureProvider(), Config(), None)
        target = Span(1, 2)
        search.issue("context_unresolved", target, "context")
        search.issue("context_unresolved", target, "context")
        self.assertEqual(len(search.issues), 1)
        search.issues[0]["affects_completion"] = False
        search.issue("context_unresolved", target, "context", False)
        self.assertEqual(len(search.issues), 1)
        search.issue("context_unresolved", target, "context")
        self.assertEqual([row["affects_completion"] for row in search.issues], [False, True])
        scoped = {"code": "claim_conflicted", "range": [1, 2],
                  "phase": "claim-verify", "affects_completion": True,
                  "claim_id": "fixture-claim"}
        search.issues.append(scoped)
        search.issue("claim_conflicted", target, "claim-verify")
        search.issue("claim_conflicted", target, "claim-verify")
        self.assertEqual(len(search.issues), 4)
        self.assertIs(search.issues[2], scoped)

    def test_origin_capture_matches_recomputed_provenance_across_budgets(self):
        for limit in (1, 10):
            with self.subTest(max_calls=limit):
                search = Search(source(2), FixtureProvider(), Config(max_calls=limit), None)
                target = Span(1, 2)
                state = search.state_for(target)
                questions = {f"q{i}": noul(search.pack, "screen", target) for i in range(70)}
                origins = []
                search.ask(state, questions, target, "fixture", origins=origins)
                self.assertEqual(origins, search.request_origins_for(state, questions))
                self.assertGreater(len(origins), 1)
                if limit == 1:
                    self.assertEqual(origins[1]["status"], "not-dispatched")
                else:
                    self.assertTrue(all(row["status"] == "validated" for row in origins))

    def test_cached_answers_preserve_independent_mutable_containers(self):
        search = Search(source(2), FixtureProvider(), Config(), None)
        target = Span(1, 2)
        state = search.state_for(target)
        questions = {"screen": noul(search.pack, "screen", target),
                     "where": choice(search.pack, "localize", target, [target])}
        origins = []
        first = search.ask(state, questions, target, "fixture", origins=origins)
        expected = deepcopy(first)
        first["screen"]["noul"] = Decimal("0.77")
        first["where"]["probabilities"]["none"] = Decimal("0.13")
        reused_origins = []
        second = search.ask(state, questions, target, "fixture", origins=reused_origins)
        self.assertEqual(second, expected)
        self.assertEqual(origins, reused_origins)
        self.assertEqual(search.calls, 1)

    def test_bounded_wire_retention_preserves_calls_and_origin_records(self):
        outputs = []
        for cap in (1, 1024 * 1024):
            search = Search(source(2), FixtureProvider(), Config(), None)
            target = Span(1, 2)
            state = search.state_for(target)
            questions = {f"q{i}": noul(search.pack, "screen", target) for i in range(70)}
            origins = []
            with patch("bug_hunter.engine.MAX_PREPARED_WIRE_BYTES", cap):
                answers = search.ask(state, questions, target, "fixture", origins=origins)
            outputs.append((answers, origins, search.trace, search.gw.requests))
        self.assertEqual(outputs[0], outputs[1])

    def test_late_invalid_question_prevents_every_dispatch_even_after_flush(self):
        search = Search(source(2), FixtureProvider(), Config(), None)
        target = Span(1, 2)
        state = search.state_for(target)
        questions = {f"q{i}": noul(search.pack, "screen", target) for i in range(70)}
        questions["invalid"] = {"type": "noul", "instructions": "invalid"}
        origins = []
        with patch("bug_hunter.engine.MAX_PREPARED_WIRE_BYTES", 1):
            self.assertEqual(search.ask(state, questions, target, "fixture", origins=origins), {})
        self.assertEqual(search.gw.requests, [])
        self.assertEqual(search.calls, 0)
        self.assertEqual(origins, search.request_origins_for(state, questions))

    def test_cached_first_batch_keeps_later_overflow_admissible(self):
        search = Search(source(2), FixtureProvider(), Config(max_calls=2), None)
        target = Span(1, 2)
        state = search.state_for(target)
        questions = {f"q{i}": noul(search.pack, "screen", target) for i in range(140)}
        batches = list(pack_questions(state, questions))
        self.assertGreater(len(batches), 2)
        search.ask(state, batches[0][0], target, "fixture")
        origins = []
        with patch("bug_hunter.engine.MAX_PREPARED_WIRE_BYTES", 1):
            answers = search.ask(state, questions, target, "fixture", origins=origins)
        self.assertEqual(set(answers), set(batches[0][0]) | set(batches[1][0]))
        self.assertEqual(search.calls, 2)
        self.assertEqual(search.cache_hits, 1)
        self.assertEqual([row["sequence"] for row in origins[:2]], [1, 2])
        self.assertTrue(all(row["status"] == "not-dispatched" for row in origins[2:]))
        self.assertEqual(origins, search.request_origins_for(state, questions))

    def test_undispatched_provenance_keeps_preflight_and_expiry_distinct(self):
        search = Search(source(2), FixtureProvider(), Config(), None)
        target = Span(1, 2)
        state = search.state_for(target)
        questions = {"q": noul(search.pack, "screen", target)}
        search.deadline_at = time.monotonic() - 1
        origins = []
        self.assertEqual(search.ask(state, questions, target, "fixture", origins=origins), {})
        self.assertEqual(origins, search.request_origins_for(state, questions))
        self.assertEqual(origins[0]["status"], "not-dispatched")
        search.deadline_at = time.monotonic() + 60
        oversized = {**state, "untrusted_fixture": "x" * 100000}
        origins = []
        self.assertEqual(search.ask(oversized, questions, target, "fixture", origins=origins), {})
        self.assertEqual(origins, search.request_origins_for(oversized, questions))
        self.assertEqual(origins[0]["status"], "preflight-failed")
        self.assertEqual(search.calls, 0)


if __name__ == "__main__":
    unittest.main()
