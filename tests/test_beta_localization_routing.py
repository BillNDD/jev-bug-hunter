"""Outcome tests for routing an admitted beta interval to absolute verification."""
import json
import time
import unittest

from bug_hunter.claims import ROLES
from bug_hunter.core import Config, Span
from bug_hunter.engine import Search
from tests.support import FixtureProvider, source


class LensRoutedProvider(FixtureProvider):
    """Broad suspicion stays below inspect; only a lens admits localization."""
    def __init__(self, outcome="supported", *, second_candidate=False, sentinel=None):
        super().__init__()
        self.outcome = outcome
        self.second_candidate = second_candidate
        self.sentinel = sentinel

    def answer(self, qid, question, state):
        if qid.startswith("screen_") or qid == "exists" or qid.startswith("verify_"):
            return {"type": "noul", "noul": .49}
        if qid.startswith("lens_"):
            return {"type": "noul", "noul": .60 if qid.startswith("lens_contract_") else .05}
        if qid == "where":
            selected = self.sentinel or ("L2-2" if "L2-2" in question["criteria"] else
                                         next(k for k in question["criteria"] if k.startswith("L")))
            probabilities = {k: int(k == selected) for k in question["criteria"]}
            if self.second_candidate and selected == "L2-2" and "L3-3" in probabilities:
                probabilities.update({"L2-2": .55, "L3-3": .45})
            return {"type": "choice", "choice": selected, "confidence": 1,
                    "probabilities": probabilities}
        if qid in ROLES:
            target = state["scoped_proposition"]["target"]
            if (target["start_line"], target["end_line"]) == (2, 2):
                if self.outcome == "invalid" and qid == ROLES[2]:
                    return {"type": "noul", "noul": "invalid"}
                values = {"supported": (.95, .05, .05), "refuted": (.05, .95, .05),
                          "insufficient": (.05, .05, .95),
                          "invalid": (.95, .05, .05), "missing_answer": (.95, .05, .05)}
                value = values[self.outcome][ROLES.index(qid)]
            else:
                value = .49 if qid == ROLES[0] else .05
            return {"type": "noul", "noul": value}
        return super().answer(qid, question, state)

    def raw(self, state, questions):
        raw = super().raw(state, questions)
        target = state.get("scoped_proposition", {}).get("target", {})
        if (self.outcome == "missing_answer" and ROLES[2] in questions
                and (target.get("start_line"), target.get("end_line")) == (2, 2)):
            response = json.loads(raw)
            del response["answers"][ROLES[2]]
            return json.dumps(response).encode()
        return raw


def configuration(**overrides):
    defaults = dict(search_policy="scoped-v2-beta", width=4, min_width=4,
                    max_depth=0, whole_file=False, max_action_steps=0,
                    max_localizations=1, localization_beam_width=1)
    defaults.update(overrides)
    return Config(**defaults)


def direct_view(search):
    target = Span(1, 4)
    return {"id": "fixture", "region": target, "state": search.state_for(target)}


class BetaLocalizationRoutingTests(unittest.TestCase):
    def run_search(self, provider=None, *, lines=4, **overrides):
        gateway = provider or LensRoutedProvider()
        search = Search(source(lines), gateway, configuration(**overrides), None)
        return search, search.run()

    def test_lens_admitted_interval_reports_only_after_absolute_narrow_support(self):
        search, report = self.run_search()
        self.assertEqual(report["status"], "complete")
        self.assertEqual([(r["start_line"], r["end_line"]) for r in report["findings"]], [(2, 2)])
        self.assertTrue(all(s["origin"] == "scoped_verification"
                            for s in report["findings"][0]["support"]))
        route = next(e for e in report["events"] if e["phase"] == "localize")
        self.assertEqual(route["exists"], "0.49")
        self.assertEqual(route["routing_basis"], "positive-mass-interval-verification")
        self.assertEqual(route["planned_verification_candidates"], ["L2-2"])
        stop = next(e for e in report["events"] if e["phase"] == "localization_policy_stop")
        self.assertEqual(stop["verified_candidates"], ["L2-2"])
        self.assertEqual(sum("exists" in r["questions"] for r in search.gw.requests), 1)

    def test_refutation_and_missing_evidence_do_not_become_report_support(self):
        for outcome, disposition, status in (("refuted", "explicitly_refuted", "complete"),
                                              ("insufficient", "insufficient_evidence", "incomplete")):
            with self.subTest(outcome=outcome):
                _, report = self.run_search(LensRoutedProvider(outcome))
                self.assertEqual(report["findings"], [])
                self.assertEqual(report["status"], status)
                narrow = next(r for r in report["rechecks"] if r["target"] == [2, 2])
                self.assertEqual(narrow["disposition"], disposition)
                stop = next(e for e in report["events"] if e["phase"] == "localization_policy_stop")
                self.assertEqual(stop["verified_candidates"], ["L2-2"])

    def test_legacy_broad_existence_gate_remains_unchanged(self):
        _, report = self.run_search(search_policy="legacy-v1")
        self.assertEqual(report["findings"], [])
        self.assertEqual([r["target"] for r in report["rechecks"]], [[1, 4]])
        self.assertTrue(any(e["phase"] == "localize" for e in report["events"]))
        self.assertFalse(any("routing_basis" in e for e in report["events"]))

    def test_call_and_question_caps_never_label_unsubmitted_triads_verified(self):
        for limits, code in (({"max_calls": 3}, "call_budget_exhausted"),
                             ({"max_questions": 13}, "question_budget_exhausted")):
            with self.subTest(limits=limits):
                _, report = self.run_search(**limits)
                self.assertEqual(report["findings"], [])
                self.assertEqual(report["status"], "incomplete")
                self.assertTrue(any(i["code"] == code for i in report["issues"]))
                stop = next(e for e in report["events"] if e["phase"] == "localization_policy_stop")
                self.assertEqual(stop["planned_verification_candidates"], ["L2-2"])
                self.assertEqual(stop["verified_candidates"], [])
                self.assertLessEqual(report["calls_attempted"], limits.get("max_calls", 1000))
                self.assertLessEqual(report["questions_attempted"], limits.get("max_questions", 20000))

    def test_invalid_or_partial_triads_cannot_supply_verified_labels(self):
        for outcome in ("invalid", "missing_answer"):
            with self.subTest(outcome=outcome):
                _, report = self.run_search(LensRoutedProvider(outcome))
                self.assertEqual(report["findings"], [])
                self.assertEqual(report["status"], "incomplete")
                stop = next(e for e in report["events"] if e["phase"] == "localization_policy_stop")
                self.assertEqual(stop["verified_candidates"], [])

    def test_localization_cap_preserves_unexamined_beam_as_incomplete(self):
        _, report = self.run_search(LensRoutedProvider(second_candidate=True),
                                    localization_beam_width=3, max_localizations=1)
        self.assertEqual([(r["start_line"], r["end_line"]) for r in report["findings"]], [(2, 2)])
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(any(i["code"] == "localization_limit" for i in report["issues"]))
        narrow = [r for r in report["rechecks"] if r["target"] != [1, 4]]
        self.assertEqual([r["target"] for r in narrow], [[2, 2]])
        search = Search(source(4), LensRoutedProvider(), configuration(max_localizations=0), None)
        search.localize(direct_view(search), Span(1, 4))
        self.assertEqual(search.calls, 0)
        self.assertEqual(search.rechecks, [])
        self.assertTrue(any(i["code"] == "localization_limit" for i in search.issues))

    def test_later_targets_losing_budget_remain_explicitly_incomplete(self):
        _, report = self.run_search(lines=8, max_calls=6)
        self.assertEqual(report["calls_attempted"], 6)
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(any(r["start_line"] == r["end_line"] == 2 for r in report["findings"]))
        self.assertTrue(any(r["target"][0] > 1 and r["target"] != [2, 2]
                            and r["score"] is None for r in report["rechecks"]))
        self.assertTrue(any(i["code"] == "call_budget_exhausted" and i["range"][0] > 1
                            for i in report["issues"]))

    def test_cache_reuse_is_verified_but_stale_prior_support_is_not(self):
        gateway = LensRoutedProvider()
        search = Search(source(4), gateway, configuration(), None)
        view = direct_view(search)
        search.localize(view, Span(1, 4))
        search.localize(view, Span(1, 4))
        self.assertEqual(search.calls, 2)
        self.assertEqual(search.cache_hits, 2)
        stops = [e for e in search.events if e["phase"] == "localization_policy_stop"]
        self.assertTrue(all(e["verified_candidates"] == ["L2-2"] for e in stops))
        search.finalize_relationship_promotions()
        self.assertIn(Span(2, 2), search.support)
        gateway.outcome = "invalid"
        view["state"]["new_evidence_marker"] = "synthetic second view"
        search.localize(view, Span(1, 4))
        last = [e for e in search.events if e["phase"] == "localization_policy_stop"][-1]
        self.assertEqual(last["planned_verification_candidates"], ["L2-2"])
        self.assertEqual(last["verified_candidates"], [])

    def test_none_unlocalized_and_expired_paths_keep_existing_behavior(self):
        for sentinel, rechecks in (("none", 0), ("unlocalized", 1)):
            with self.subTest(sentinel=sentinel):
                search = Search(source(4), LensRoutedProvider(sentinel=sentinel), configuration(), None)
                search.localize(direct_view(search), Span(1, 4))
                self.assertEqual(len(search.rechecks), rechecks)
                self.assertFalse(any("routing_basis" in e for e in search.events))
        search = Search(source(4), LensRoutedProvider(), configuration(), None)
        search.deadline_at = time.monotonic() - 1
        search.localize(direct_view(search), Span(1, 4))
        self.assertEqual(search.calls, 0)
        self.assertEqual(search.rechecks, [])
        self.assertTrue(any(i["code"] == "run_deadline_exceeded" for i in search.issues))

    def test_valid_rounded_zero_mass_menu_does_not_create_a_positive_beam(self):
        class ZeroMassProvider(LensRoutedProvider):
            def answer(self, qid, question, state):
                if qid == "where":
                    # 212 displayed zeros admit normalized mass under nearest
                    # rounding at two places; none has positive displayed mass.
                    return {"type": "choice", "choice": "L2-2", "confidence": 0,
                            "probabilities": {key: 0 for key in question["criteria"]}}
                return super().answer(qid, question, state)

        search = Search(source(20), ZeroMassProvider(),
                        configuration(width=20, min_width=20), None)
        target = Span(1, 20)
        view = {"id": "fixture", "region": target, "state": search.state_for(target)}
        search.localize(view, target)
        self.assertEqual(search.calls, 1)
        self.assertEqual(search.validated_questions, 2)
        self.assertEqual(search.rechecks, [])
        self.assertEqual(search.support, {})
        self.assertFalse(any("routing_basis" in e for e in search.events))


if __name__ == "__main__":
    unittest.main()
