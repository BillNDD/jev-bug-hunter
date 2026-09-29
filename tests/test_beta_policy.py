"""Actual-engine regressions for the opt-in policy; synthetic judgments only."""
from decimal import Decimal, localcontext
from itertools import permutations
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bug_hunter import jev
from bug_hunter.claims import (ROLES, SourceRef, canonical_decimal, identity,
                               classify_evaluation, reconcile_claim, select_exploration)
from bug_hunter.core import Config, Source, Span
from bug_hunter.engine import Search
from bug_hunter.questions import noul
from bug_hunter.evidence import EvidenceRef
from tests.support import FixtureProvider, KEY, scan, source


def beta(**kwargs):
    return Config(search_policy="scoped-v2-beta", max_action_steps=0,
                  max_localizations=0, whole_file=False, **kwargs)


def role_values(support="0.95", refute="0.05", missing="0.05"):
    return dict(zip(ROLES, map(Decimal, (support, refute, missing))))


class RolesProvider(FixtureProvider):
    def __init__(self, values=None, *, fail=False, **kwargs):
        super().__init__(**kwargs)
        self.values = values or (lambda q, state, target: role_values())
        self.fail = fail

    def answer(self, qid, question, state):
        if qid in ROLES:
            if self.fail and qid == ROLES[1]:
                return {"type": "noul", "noul": "malformed"}
            target = state["scoped_proposition"]["target"]
            return {"type": "noul", "noul": float(self.values(qid, state, target)[qid])}
        return super().answer(qid, question, state)


def group(search, span, extras=()):
    return {"id": "fixture-view", "region": span, "state": search.state_for(span, extras=extras)}


class DomainTests(unittest.TestCase):
    def test_decimal_identity_ignores_context_and_dictionary_order(self):
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(canonical_decimal(Decimal("0.123456000")), "0.123456")
            self.assertEqual(canonical_decimal(Decimal("-0.000")), "0")
            self.assertEqual(identity({"a": Decimal("0.8"), "b": 1}),
                             identity({"b": 1, "a": Decimal("0.800")}))
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("1e999999")):
            with self.assertRaises(ValueError):
                canonical_decimal(value)

    def test_reference_distinguishes_paths_and_rejects_host_paths(self):
        args = dict(namespace="scope", source_sha256="a" * 64, encoding="utf-8",
                    start_line=1, end_line=2)
        self.assertNotEqual(SourceRef(file="a.py", **args).id, SourceRef(file="b.py", **args).id)
        for path in ("/home/private/a.py", "C:\\private\\a.py", "../a.py"):
            with self.assertRaises(ValueError):
                SourceRef(file=path, **args)

    def test_role_truth_table_keeps_unknown_and_refutation_distinct(self):
        cases = [(".79", ".05", ".05", "undetermined"),
                 (".96", ".05", ".05", "supported"),
                 (".05", ".96", ".05", "explicitly_refuted"),
                 (".96", ".96", ".05", "conflicted"),
                 (".96", ".05", ".95", "inconsistent_evidence_assessment"),
                 (".20", ".05", ".95", "insufficient_evidence")]
        for s, r, m, expected in cases:
            self.assertEqual(classify_evaluation(role_values(s, r, m)), expected)
        self.assertEqual(classify_evaluation({}), "unknown_evaluation")

    def test_reconciliation_is_order_independent_and_rejects_other_claim(self):
        rows = [{"id": str(i), "claim_id": "claim", "disposition": state}
                for i, state in enumerate(("undetermined", "insufficient_evidence", "supported"))]
        for order in permutations(rows):
            self.assertEqual(reconcile_claim(order), "supported")
        rows.append({"id": "3", "claim_id": "claim", "disposition": "explicitly_refuted"})
        for order in permutations(rows):
            self.assertEqual(reconcile_claim(order), "conflicted")
        with self.assertRaises(ValueError):
            reconcile_claim([rows[0], {**rows[1], "claim_id": "different"}])

    def test_three_way_beam_ties_one_hot_and_sentinels(self):
        answer = {"choice": "A", "confidence": Decimal(".1"),
                  "probabilities": dict(zip(("A", "B", "C"), map(Decimal, (".42", ".35", ".23"))))}
        self.assertEqual(select_exploration(answer, ["A", "B", "C"], 3), ["A", "B", "C"])
        self.assertEqual(select_exploration(answer, ["A", "B", "C"], 2), ["A", "B"])
        answer["probabilities"] = {"A": Decimal(".5"), "B": Decimal(".5"), "C": Decimal(0)}
        self.assertEqual(select_exploration(answer, ["B", "A", "C"], 3), ["B", "A"])
        answer["probabilities"] = {"A": Decimal(1), "B": Decimal(0), "C": Decimal(0)}
        self.assertEqual(select_exploration(answer, ["A", "B", "C"], 3), ["A"])
        answer["choice"] = "none"
        self.assertEqual(select_exploration(answer, ["A", "B", "C"], 3), [])


class ScopedEngineTests(unittest.TestCase):
    def test_direct_recheck_receives_retrieved_project_text(self):
        def values(q, state, target):
            return role_values() if state.get("related_sources") else role_values(".20", ".05", ".95")
        search = Search(source(4), RolesProvider(values), beta(), None)
        target = Span(1, 2)
        helper = Source.from_text("def value():\n    return 7\n", "helper.py")
        ref = EvidenceRef(helper, "helper.py", Span(1, 2), "project")
        search.evidence_by_target[target] = {ref.id: {"ref": ref, "score": Decimal(".95")}}
        search.verify(group(search, target), [target])
        state = search.gw.requests[-1]["state"]
        self.assertEqual(state["related_sources"][0]["lines"][1]["text"], "    return 7")
        self.assertNotIn("score", state["related_sources"][0])
        self.assertEqual(set(search.scoped.dispositions.values()), {"supported"})

    def test_oversized_union_keeps_conflict_and_no_fabricated_dispatch(self):
        def values(q, state, target):
            visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
            return role_values() if 4 in visible else role_values(".05", ".95")
        provider = RolesProvider(values)
        search = Search(source(8, "x" * 2500), provider, beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target, (Span(3, 4),)), [target])
        search.verify(group(search, target, (Span(7, 8),)), [target])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(set(search.scoped.dispositions.values()), {"conflicted"})
        union = next(r for r in search.scoped.report()["evaluations"] if r["origin"] == "union-adjudication")
        self.assertEqual(union["disposition"], "unknown_evaluation")
        self.assertTrue(all(o["status"] == "preflight-failed" for o in union["origin_requests"]))
        self.assertTrue(any(i["affects_completion"] for i in search.issues))

    def test_new_uncovered_missing_evidence_does_not_become_complete(self):
        def values(q, state, target):
            return role_values() if len(state["excerpts"]) == 1 else role_values(".20", ".05", ".95")
        search = Search(source(8), RolesProvider(values), beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target), [target])
        search.verify(group(search, target, (Span(7, 8),)), [target])
        self.assertEqual(set(search.scoped.dispositions.values()), {"supported_unresolved"})
        self.assertTrue(any(i["affects_completion"] for i in search.issues))
        search.finalize_relationship_promotions()
        self.assertEqual(search.scoped.assessment(target), "unknown")
        self.assertIn(target, search.support)  # Former valid support remains visibly disputed.

    def test_relation_gate_cannot_migrate_to_different_supported_revision(self):
        def values(q, state, target):
            return role_values(".20") if len(state["excerpts"]) == 1 else role_values()
        def override(qid, q, state, target):
            if qid.startswith("relation_"):
                return {"type": "noul", "noul": .95 if len(state["excerpts"]) == 1 else .05}
        search = Search(source(8), RolesProvider(values, override=override), beta(), None)
        target = Span(1, 2)
        ref = EvidenceRef(Source.from_text("helper", "helper.txt"), "helper.txt", Span(1, 1))
        record = {"ref": ref, "score": Decimal(".95"), "depth": 0}
        states = [group(search, target)["state"], group(search, target, (Span(7, 8),))["state"]]
        with patch.object(search, "_relationship_candidates", return_value=[record]), \
             patch.object(search, "fitting_pair", side_effect=states):
            search.test_relationships(target)
            search.test_relationships(target)
        search.finalize_relationship_promotions()
        self.assertNotIn(target, search.support)

    def test_completed_and_capped_search_memos_skip_reconstruction(self):
        search = Search(source(30), FixtureProvider(), beta(max_evidence_candidates=1), None)
        target = Span(1, 2)
        view = group(search, target)
        search.hierarchical_evidence_search(target, "source", view)
        first_calls = search.calls
        with patch.object(search, "_same_file_evidence_roots", side_effect=AssertionError("re-enumerated")):
            search.hierarchical_evidence_search(target, "source", view)
        self.assertEqual(search.calls, first_calls)
        self.assertTrue(any(e["phase"] == "evidence_search_reuse" for e in search.events))

    def test_whole_view_does_not_suppress_local_project_obligations(self):
        from tests.test_autonomous_project import inputs
        primary, spec, project = inputs()
        primary = source(12)
        search = Search(primary, FixtureProvider(), beta(width=6, min_width=3), spec,
                        project=project, context_scope="project")
        search.groups = [{"region": Span(1, 12), "phase": "whole", "scores": {}},
                         {"region": Span(1, 6), "phase": "screen", "scores": {Span(1, 2): Decimal(".95")}},
                         {"region": Span(7, 12), "phase": "screen", "scores": {}}]
        with patch.object(search, "hierarchical_evidence_search") as retrieve, \
             patch.object(search, "test_relationships"):
            search.project_relationship_pass()
        self.assertEqual([c.args[0] for c in retrieve.call_args_list], [Span(1, 6), Span(7, 12)])

    def test_local_insufficiency_does_not_veto_enriched_same_proposition(self):
        def values(q, state, target):
            visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
            return role_values() if 6 in visible else role_values(".20", ".05", ".95")
        search = Search(source(6), RolesProvider(values), beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target), [target])
        first_issue = next(i for i in search.issues if i["code"] == "claim_insufficient_evidence")
        search.issue("unrelated_failure", Span(5, 6), "other")
        search.verify(group(search, target, (Span(5, 6),)), [target])
        search.finalize_relationship_promotions()
        self.assertIn(target, search.support)
        self.assertFalse(first_issue["affects_completion"])
        self.assertTrue(next(i for i in search.issues if i["code"] == "unrelated_failure")["affects_completion"])
        self.assertEqual(len(search.scoped.claims), 1)
        self.assertEqual(len(search.scoped.revisions), 2)
        self.assertTrue(all(r["origin_requests"] for r in search.scoped.report()["evaluations"]))

    def test_near_threshold_support_is_not_refutation(self):
        provider = RolesProvider(lambda q, state, target:
            role_values(".79" if len(state["excerpts"]) == 1 else ".96"))
        search = Search(source(6), provider, beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target), [target])
        search.verify(group(search, target, (Span(5, 6),)), [target])
        search.finalize_relationship_promotions()
        self.assertEqual(search.scoped.assessment(target), "scoped-support")
        self.assertIn(target, search.support)

    def test_other_counterpart_refutation_cannot_veto_relationship(self):
        def values(q, state, target):
            other = state["scoped_proposition"]["counterpart"]
            return role_values(".05", ".95") if other["file"] == "a.txt" else role_values()
        def override(qid, q, state, target):
            if qid.startswith("relation_"):
                return {"type": "noul", "noul": .95}
        search = Search(source(4), RolesProvider(values, override=override), beta(), None)
        target = Span(1, 2)
        for name in ("a.txt", "b.txt"):
            ref = EvidenceRef(Source.from_text("value", name), name, Span(1, 1), "project")
            record = {"ref": ref, "score": Decimal(".95"), "depth": 0}
            with patch.object(search, "_relationship_candidates", return_value=[record]):
                search.test_relationships(target)
        search.finalize_relationship_promotions()
        self.assertIn(target, search.support)
        self.assertEqual(set(search.scoped.dispositions.values()), {"supported", "explicitly_refuted"})
        self.assertEqual(len(search.support[target]), 1)

    def test_true_conflict_uses_actual_union_and_retains_history(self):
        def values(q, state, target):
            visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
            if 4 in visible and 8 in visible:
                return role_values(".95", ".95")
            return role_values() if 4 in visible else role_values(".05", ".95")
        provider = RolesProvider(values)
        search = Search(source(8), provider, beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target, (Span(3, 4),)), [target])
        search.verify(group(search, target, (Span(7, 8),)), [target])
        self.assertEqual(len(provider.requests), 3)
        union = provider.requests[-1]["state"]
        visible = {line["line"] for e in union["excerpts"] for line in e["lines"]}
        self.assertTrue({1, 2, 3, 4, 7, 8} <= visible)
        self.assertNotIn("roles", union)
        self.assertNotIn("disposition", json.dumps(union))
        self.assertEqual(set(search.scoped.dispositions.values()), {"conflicted"})
        self.assertEqual(len(search.scoped.report()["evaluations"]), 3)
        search.finalize_relationship_promotions()
        self.assertIn(target, search.support)
        self.assertEqual(search.scoped.assessment(target), "conflicting")

    def test_union_resolution_links_all_superseded_observations(self):
        def values(q, state, target):
            visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
            return role_values() if 4 in visible else role_values(".05", ".95")
        search = Search(source(8), RolesProvider(values), beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target, (Span(3, 4),)), [target])
        search.verify(group(search, target, (Span(7, 8),)), [target])
        rows = search.scoped.report()["evaluations"]
        union = next(r for r in rows if r["origin"] == "union-adjudication")
        self.assertEqual(set(union["supersedes"]), {r["id"] for r in rows if r is not union})
        self.assertTrue(union["union_coverage_verified"])
        self.assertEqual(set(search.scoped.dispositions.values()), {"supported"})

    def test_union_does_not_erase_uncovered_missing_material(self):
        def values(q, state, target):
            visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
            if 6 in visible:
                return role_values(".20", ".05", ".95")
            return role_values() if 4 in visible else role_values(".05", ".95")
        search = Search(source(8), RolesProvider(values), beta(), None)
        target = Span(1, 2)
        for extra in (Span(5, 6), Span(3, 4), Span(7, 8)):
            search.verify(group(search, target, (extra,)), [target])
        rows = search.scoped.report()["evaluations"]
        missing = next(r for r in rows if r["disposition"] == "insufficient_evidence")
        union = next(r for r in rows if r["origin"] == "union-adjudication")
        self.assertNotIn(missing["id"], union["supersedes"])
        self.assertEqual(set(search.scoped.dispositions.values()), {"supported_unresolved"})
        self.assertTrue(any(i["code"] == "claim_insufficient_evidence" and i["affects_completion"]
                            for i in search.issues))

    def test_superseded_support_cannot_supply_union_relation_gate(self):
        for union_relation in (.05, .95):
            with self.subTest(union_relation=union_relation):
                def values(q, state, target):
                    visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
                    return role_values() if 4 in visible else role_values(".05", ".95")
                def override(qid, q, state, target):
                    if qid.startswith("relation_"):
                        return {"type": "noul", "noul": union_relation if len(state["excerpts"]) == 3 else .95}
                search = Search(source(8), RolesProvider(values, override=override), beta(), None)
                target = Span(1, 2)
                ref = EvidenceRef(Source.from_text("helper", "helper.txt"), "helper.txt", Span(1, 1))
                record = {"ref": ref, "score": Decimal(".95"), "depth": 0}
                states = [group(search, target, (s,))["state"] for s in (Span(3, 4), Span(7, 8))]
                with patch.object(search, "_relationship_candidates", return_value=[record]), \
                     patch.object(search, "fitting_pair", side_effect=states):
                    search.test_relationships(target)
                    search.test_relationships(target)
                search.finalize_relationship_promotions()
                union = next(r for r in search.scoped.report()["evaluations"]
                             if r["origin"] == "union-adjudication")
                self.assertEqual(union["route_qualified"], union_relation >= .70)
                if union_relation < .70:
                    self.assertNotIn(target, search.support)
                else:
                    self.assertEqual(search.support[target][0]["evaluation_id"], union["id"])

    def test_subset_of_prior_view_does_not_create_unchanged_adjudication(self):
        provider = RolesProvider(lambda q, state, target:
            role_values() if len(state["excerpts"]) == 1 else role_values(".05", ".95"))
        search = Search(source(6), provider, beta(), None)
        target = Span(1, 2)
        search.verify(group(search, target), [target])
        search.verify(group(search, target, (Span(5, 6),)), [target])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(set(search.scoped.dispositions.values()), {"conflicted"})

    def test_missing_material_blocks_promotion_and_failed_batch_is_not_retried(self):
        target = Span(1, 2)
        for provider in (RolesProvider(lambda *args: role_values(".95", ".05", ".95")),
                         RolesProvider(fail=True)):
            search = Search(source(4), provider, beta(), None)
            view = group(search, target)
            search.verify(view, [target])
            search.verify(view, [target])
            search.finalize_relationship_promotions()
            self.assertNotIn(target, search.support)
            self.assertEqual(len(provider.requests), 1)
            self.assertTrue(any(i["affects_completion"] for i in search.issues))

    def test_call_budget_exhaustion_is_unknown(self):
        search = Search(source(6), RolesProvider(), beta(max_calls=1), None)
        search.verify(group(search, Span(1, 2)), [Span(1, 2)])
        search.verify(group(search, Span(3, 4)), [Span(3, 4)])
        self.assertEqual(search.calls, 1)
        self.assertIn("unknown_evaluation", search.scoped.dispositions.values())
        self.assertTrue(any(i["code"] == "call_budget_exhausted" for i in search.issues))

    def test_full_engine_reports_policy_and_never_invents_exact_defect(self):
        provider = RolesProvider(bugs=[(2, 2)])
        result = scan(source(4), provider, beta(run_deadline_seconds=2))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["calls_attempted"], 2)
        self.assertEqual(result["schema_version"], 6)
        self.assertEqual(result["handoff"]["schema_version"], 3)
        self.assertEqual(result["configuration"]["search_policy"], "scoped-v2-beta")
        self.assertEqual(result["calibration"], "uncalibrated-operating-policy")
        self.assertTrue(result["findings"])
        self.assertTrue(all(c["kind"] == "provisional-scoped-proposition"
                            for c in result["scoped_verification"]["claims"]))
        self.assertTrue(all("fixture line" not in json.dumps(x) for x in
                            result["scoped_verification"].values()))


class ExplorationIntegrationTests(unittest.TestCase):
    def test_diffuse_choice_reaches_three_real_verifications(self):
        def override(qid, q, state, target):
            if qid == "where":
                masses = {k: 0 for k in q["criteria"]}
                masses.update({"L1-1": .42, "L2-2": .35, "L3-3": .23})
                return {"type": "choice", "choice": "L1-1", "confidence": .1,
                        "probabilities": masses}
            if qid == "exists":
                return {"type": "noul", "noul": .99}
        search = Search(source(3), RolesProvider(override=override),
                        Config(search_policy="scoped-v2-beta", max_localizations=3), None)
        search.localize(group(search, Span(1, 3)), Span(1, 3))
        self.assertEqual([r["target"] for r in search.rechecks], [[1, 1], [2, 2], [3, 3]])
        self.assertEqual(search.support, {})  # No report support before validated final reduction.
        search.finalize_relationship_promotions()
        self.assertEqual(set(search.support), {Span(1, 1), Span(2, 2), Span(3, 3)})

    def test_sentinel_with_existence_routes_recovery_and_keeps_parent(self):
        def override(qid, q, state, target):
            if qid == "exists":
                return {"type": "noul", "noul": .99}
        search = Search(source(4), RolesProvider(override=override),
                        Config(search_policy="scoped-v2-beta", max_localizations=3), None)
        view = group(search, Span(1, 4))
        search.add_support(Span(1, 4), Decimal(".95"), "screen", view["state"], view["id"])
        search.localize(view, Span(1, 4))
        self.assertIn(Span(1, 4), search.reconcile())
        self.assertTrue(any(x["phase"] == "localization_recovery" for x in search.events))
        self.assertTrue(any(x["code"] == "localization_unresolved" for x in search.issues))

    def test_legacy_gate_is_preserved(self):
        search = Search(source(3), FixtureProvider(), Config(), None)
        answer = {"choice": "L1-1", "confidence": Decimal(".1"),
                  "probabilities": {"L1-1": Decimal(".42"), "L2-2": Decimal(".35"),
                                    "L3-3": Decimal(".23")}}
        self.assertEqual(search.gated_candidates(answer, [Span(i, i) for i in (1, 2, 3)], 3), [])


class TransportRepairTests(unittest.TestCase):
    def test_effective_deadline_and_environment_are_passed_to_child(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,
                {"TYPESAFE_API_KEY": KEY, "UNRELATED_SECRET": "must-not-inherit", "HTTPS_PROXY": "unused"}):
            gateway = jev.HostedJev(td, deadline_seconds=60)
            provider = FixtureProvider()
            state = Search(source(2), provider, Config(), None).state_for(Span(1, 2))
            with patch.object(jev.subprocess, "run", side_effect=provider.process) as child:
                question = noul(Search(source(2), provider, Config(), None).pack, "screen", Span(1, 2))
                gateway.evaluate(state, {"q": question}, metadata={"phase": "test"})
            env = child.call_args.kwargs["env"]
            self.assertEqual(float(env["JEV_CALL_TIMEOUT_SECONDS"]), 60)
            self.assertNotIn("UNRELATED_SECRET", env)
            self.assertNotIn("HTTPS_PROXY", env)
            receipt = json.loads(Path(gateway.last_receipt).read_text())
            self.assertEqual(receipt["transport_timeout_seconds"], 60)
            self.assertGreaterEqual(receipt["transport_elapsed_seconds"], 0)

    def test_child_timeout_has_specific_code_and_uses_effective_budget(self):
        wire = jev.encode_request({}, {"q": jev.QUESTION})
        stderr = io.StringIO()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY, "JEV_CALL_TIMEOUT_SECONDS": "57.5"}), \
             patch.object(jev.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(wire))), \
             patch.object(jev.sys, "stdout", SimpleNamespace(buffer=io.BytesIO())), \
             patch.object(jev.sys, "stderr", stderr), \
             patch.object(jev, "_http_exchange", side_effect=TimeoutError) as exchange:
            code = jev._transport_child()
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "deadline_exceeded\n")
        self.assertEqual(exchange.call_args.kwargs["timeout"], 57.5)

    def test_socket_constructor_uses_provided_timeout(self):
        with patch.object(jev.http.client, "HTTPSConnection") as connect:
            connect.return_value.request.side_effect = TimeoutError
            with self.assertRaises(TimeoutError):
                jev._http_exchange(b"{}", KEY, timeout=57.5)
            self.assertEqual(connect.call_args.kwargs["timeout"], 57.5)


if __name__ == "__main__":
    unittest.main()
