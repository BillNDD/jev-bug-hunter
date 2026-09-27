"""v0.4 Jev-maximizing evidence-search and action-controller tests."""
from __future__ import annotations

from pathlib import Path
import tempfile
from decimal import Decimal
import unittest

from bug_hunter.core import Config, Source, Span
from bug_hunter.evidence import EvidenceRef, pair_state
from bug_hunter.engine import Search
from bug_hunter.repository import ProjectIndex
from bug_hunter.jev import JevError
from tests.support import FixtureProvider, source, ranges, scan


def visible_lines(state):
    result = set()
    for excerpt in state.get("excerpts", []):
        result.update(row["line"] for row in excerpt["lines"])
    for excerpt in state.get("related_sources", []):
        result.update((excerpt["file"], row["line"]) for row in excerpt["lines"])
    return result


class V04Tests(unittest.TestCase):
    def test_bug_lenses_route_but_do_not_create_support_when_screen_is_cold(self):
        def override(qid, q, state, target):
            if qid.startswith("screen_"):
                return {"type": "noul", "noul": 0.05}
            if qid.startswith("verify_"):
                return {"type": "noul", "noul": 0.05}
            if qid == "action":
                return {"type": "choice", "choice": "finish", "confidence": 1,
                        "probabilities": {k: int(k == "finish") for k in q["criteria"]}}
        provider = FixtureProvider([(9, 9)], override=override)
        report = scan(source(12), provider, Config(whole_file=False))
        # A lens signal alone never creates a suspicion...
        self.assertFalse(ranges(report))
        # ...but it still routes the target into the investigation.
        self.assertTrue(any(r["target"] == [1, 12] for r in report["jev_actions"]))
        obs = next(row for row in report["windows"]
                   if row["start_line"] == 1 and row["end_line"] == 12)
        self.assertEqual(obs["judgments"]["screen"], "0.05")
        self.assertEqual(obs["judgments"]["lens_contract"], "0.95")

    def test_relationship_promotion_blocked_by_contrary_direct_recheck(self):
        action_count = {}
        def override(qid, q, state, target):
            # Only L1-12 is initially interesting, and not yet reportable.
            if qid.startswith(("screen_", "lens_")):
                return {"type": "noul", "noul": 0.65 if target == Span(1, 12) else 0.05}
            if qid.startswith("verify_") and not qid.startswith("verify_evidence_"):
                return {"type": "noul", "noul": 0.05}
            if qid == "continue":
                return {"type": "noul", "noul": 0.95 if target == Span(1, 12) else 0.05}
            if qid == "action":
                if target != Span(1, 12):
                    choice = "finish"
                else:
                    n = action_count.get(target, 0)
                    wanted = ["search_file", "test_relationship", "finish"]
                    choice = wanted[min(n, len(wanted)-1)]
                    action_count[target] = n + 1
                    if choice not in q["criteria"]:
                        choice = "finish"
                return {"type": "choice", "choice": choice, "confidence": 1,
                        "probabilities": {k: int(k == choice) for k in q["criteria"]}}
            if qid.startswith("evidence_"):
                vis = visible_lines(state)
                return {"type": "noul", "noul": 0.95 if 100 in vis else 0.05}
            if qid.startswith(("relation_", "verify_evidence_")):
                vis = visible_lines(state)
                return {"type": "noul", "noul": 0.95 if 100 in vis else 0.05}
        provider = FixtureProvider(override=override)
        cfg = Config(whole_file=False, max_action_targets=4,
                     max_action_steps=3, evidence_beam_width=2,
                     evidence_max_depth=4, max_evidence_candidates=40)
        report = scan(source(120), provider, cfg)
        # The contrary direct recheck (0.05) blocks relationship promotion:
        # a marginal cross-file claim is never raised over contrary evidence.
        self.assertNotIn((1, 12), ranges(report))
        self.assertTrue(any(e["phase"] == "hierarchical_evidence_search"
                            and e["target"] == "L1-12" for e in report["events"]))
        self.assertTrue(any(r["target"] == [1, 12] and r["verify_score"] == "0.95"
                            for r in report["relationships"]))
        actions = [r["action"] for r in report["jev_actions"] if r["target"] == [1, 12]]
        self.assertEqual(actions[:2], ["search_file", "test_relationship"])

    def test_relationship_promotion_with_supportive_recheck(self):
        action_count = {}
        def override(qid, q, state, target):
            if qid.startswith(("screen_", "lens_")):
                return {"type": "noul", "noul": 0.65 if target == Span(1, 12) else 0.05}
            if qid.startswith("verify_") and not qid.startswith("verify_evidence_"):
                return {"type": "noul", "noul": 0.95}
            if qid == "continue":
                return {"type": "noul", "noul": 0.95 if target == Span(1, 12) else 0.05}
            if qid == "action":
                if target != Span(1, 12):
                    choice = "finish"
                else:
                    n = action_count.get(target, 0)
                    wanted = ["search_file", "test_relationship", "finish"]
                    choice = wanted[min(n, len(wanted)-1)]
                    action_count[target] = n + 1
                    if choice not in q["criteria"]:
                        choice = "finish"
                return {"type": "choice", "choice": choice, "confidence": 1,
                        "probabilities": {k: int(k == choice) for k in q["criteria"]}}
            if qid.startswith(("evidence_", "relation_", "verify_evidence_")):
                vis = visible_lines(state)
                return {"type": "noul", "noul": 0.95 if 100 in vis else 0.05}
        provider = FixtureProvider(override=override)
        cfg = Config(whole_file=False, max_action_targets=4,
                     max_action_steps=3, evidence_beam_width=2,
                     evidence_max_depth=4, max_evidence_candidates=40)
        report = scan(source(120), provider, cfg)
        # Supportive recheck plus qualifying relationship: promoted and cited.
        self.assertIn((1, 12), ranges(report))
        handoff = next(f for f in report["handoff"]["findings"] if f["suspect"] == [1, 12])
        self.assertTrue(handoff["read_with"].get("refs"))

    def relationship_order_case(self, first, second):
        counts = {"n": 0}
        def override(qid, q, state, target):
            if qid.startswith(("screen_", "lens_")):
                return {"type": "noul", "noul": 0.65 if target == Span(1, 12) else 0.05}
            if qid.startswith("verify_evidence_"):
                counts["n"] += 1
                return {"type": "noul",
                        "noul": first if counts["n"] == 1 else second}
            if qid.startswith("verify_"):
                raise JevError("transport_failed")
            if qid == "continue":
                return {"type": "noul", "noul": 0.95 if target == Span(1, 12) else 0.05}
            if qid == "action":
                if target != Span(1, 12):
                    choice = "finish"
                else:
                    n = counts.get("steps", 0)
                    wanted = ["search_file", "test_relationship", "finish"]
                    choice = wanted[min(n, len(wanted)-1)]
                    counts["steps"] = n + 1
                    if choice not in q["criteria"]:
                        choice = "finish"
                return {"type": "choice", "choice": choice, "confidence": 1,
                        "probabilities": {k: int(k == choice) for k in q["criteria"]}}
            if qid.startswith(("evidence_", "relation_")):
                vis = visible_lines(state)
                return {"type": "noul", "noul": 0.95 if 100 in vis else 0.05}
        provider = FixtureProvider(override=override)
        cfg = Config(whole_file=False, max_action_targets=4,
                     max_action_steps=3, evidence_beam_width=2,
                     evidence_max_depth=4, max_evidence_candidates=40)
        return scan(source(120), provider, cfg)

    def test_relationship_recheck_history_blocks_in_any_order(self):
        # A contrary relationship recheck blocks promotion whether it lands
        # before or after the qualifying one (order-independent finalize).
        for first, second in ((0.05, 0.95), (0.95, 0.05)):
            with self.subTest(order=(first, second)):
                report = self.relationship_order_case(first, second)
                self.assertNotIn((1, 12), ranges(report))

    def test_failed_recheck_is_unknown_not_negative(self):
        counts = {"n": 0}
        def override(qid, q, state, target):
            if qid.startswith(("screen_", "lens_")):
                return {"type": "noul", "noul": 0.65 if target == Span(1, 12) else 0.05}
            if qid.startswith("verify_evidence_"):
                return {"type": "noul", "noul": 0.95}
            if qid.startswith("verify_"):
                raise JevError("transport_failed")
            if qid == "continue":
                return {"type": "noul", "noul": 0.95 if target == Span(1, 12) else 0.05}
            if qid == "action":
                if target != Span(1, 12):
                    choice = "finish"
                else:
                    n = counts.get("steps", 0)
                    wanted = ["search_file", "test_relationship", "finish"]
                    choice = wanted[min(n, len(wanted)-1)]
                    counts["steps"] = n + 1
                    if choice not in q["criteria"]:
                        choice = "finish"
                return {"type": "choice", "choice": choice, "confidence": 1,
                        "probabilities": {k: int(k == choice) for k in q["criteria"]}}
            if qid.startswith(("evidence_", "relation_")):
                vis = visible_lines(state)
                return {"type": "noul", "noul": 0.95 if 100 in vis else 0.05}
        provider = FixtureProvider(override=override)
        cfg = Config(whole_file=False, max_action_targets=4,
                     max_action_steps=3, evidence_beam_width=2,
                     evidence_max_depth=4, max_evidence_candidates=40)
        report = scan(source(120), provider, cfg)
        # The failed direct check is recorded unknown and does not block
        # the qualifying relationship.
        self.assertIn((1, 12), ranges(report))
        self.assertTrue(any(r["score"] is None for r in report["rechecks"]))
        self.assertEqual(report["status"], "incomplete")

    def test_repository_evidence_search_and_cross_file_read_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "main.py"
            helper = root / "helper.py"
            primary.write_text("\n".join(f"main {i}" for i in range(1, 25)) + "\n")
            helper.write_text("\n".join(f"helper {i}" for i in range(1, 41)) + "\n")
            src = Source.read(primary)
            project = ProjectIndex.read(root, primary_path=primary)
            action_count = 0
            def override(qid, q, state, target):
                nonlocal action_count
                if qid.startswith(("screen_", "lens_")):
                    return {"type": "noul", "noul": 0.65 if target == Span(1, 12) else 0.05}
                if qid.startswith("verify_") and not qid.startswith("verify_evidence_"):
                    return {"type": "noul", "noul": 0.95}
                if qid == "continue":
                    return {"type": "noul", "noul": 0.95 if target == Span(1, 12) else 0.05}
                if qid == "action":
                    # Project search is deterministic now (scope contract);
                    # Jev only orders the remaining bounded actions.
                    wanted = ["test_relationship", "finish"]
                    choice = wanted[min(action_count, 1)] if target == Span(1, 12) else "finish"
                    action_count += int(target == Span(1, 12))
                    if choice not in q["criteria"]:
                        choice = "finish"
                    return {"type": "choice", "choice": choice, "confidence": 1,
                            "probabilities": {k: int(k == choice) for k in q["criteria"]}}
                if qid.startswith("evidence_"):
                    related = state.get("related_sources", [])
                    hit = any(e["file"] == "helper.py" and e["start_line"] <= 20 <= e["end_line"]
                              for e in related)
                    return {"type": "noul", "noul": 0.95 if hit else 0.05}
                if qid.startswith(("relation_", "verify_evidence_")):
                    related = state.get("related_sources", [])
                    hit = any(e["file"] == "helper.py" and e["start_line"] <= 20 <= e["end_line"]
                              for e in related)
                    return {"type": "noul", "noul": 0.95 if hit else 0.05}
            report = scan(src, FixtureProvider(override=override),
                          Config(whole_file=False, max_action_targets=4,
                                 max_action_steps=3, evidence_beam_width=2,
                                 max_evidence_candidates=40), project=project,
                          context_scope="project")
            self.assertIn((1, 12), ranges(report))
            self.assertIsNotNone(report["project"])
            row = next(f for f in report["handoff"]["findings"] if f["suspect"] == [1, 12])
            refs = row["read_with"].get("refs", [])
            self.assertTrue(any(ref.get("file") == "helper.py" for ref in refs), refs)

    def test_action_requirement_search_is_jev_selected_and_reused_by_handoff(self):
        count = 0
        def override(qid, q, state, target):
            nonlocal count
            if qid == "continue":
                return {"type":"noul", "noul":0.95}
            if qid == "action":
                if target == Span(9, 9) and count == 0 and "search_requirement" in q["criteria"]:
                    choice = "search_requirement"
                    count += 1
                else:
                    choice = "finish"
                return {"type":"choice", "choice":choice, "confidence":1,
                        "probabilities":{k:int(k==choice) for k in q["criteria"]}}
            if qid.startswith("action_requirement_"):
                return {"type":"noul", "noul":0.95 if "R1-2" in qid else 0.05}
        spec = "Requirement alpha.\nRequirement beta.\n\nOther text."
        report = scan(source(12), FixtureProvider([(9,9)], override=override),
                      Config(whole_file=False, max_action_targets=8, max_action_steps=2),
                      spec=spec)
        self.assertTrue(any(row["target"] == [9, 9] and
                            row["action"] == "search_requirement"
                            for row in report["jev_actions"]))
        row = report["handoff"]["findings"][0]
        self.assertEqual(row["relevant_requirement"]["status"], "selected")


    def test_identical_cross_file_bytes_remain_cross_file_evidence(self):
        primary = Source.from_text("same\ncontent", name="main.py")
        helper = Source.from_text("same\ncontent", name="helper.py")
        self.assertEqual(primary.sha256, helper.sha256)
        ref = EvidenceRef(helper, "helper.py", Span(1, 2), "project")
        state = pair_state(primary, Span(1, 1), 0, None, ref)
        self.assertIn("related_sources", state)
        self.assertEqual(state["related_sources"][0]["file"], "helper.py")
        self.assertEqual(state["related_sources"][0]["start_line"], 1)

    def test_relationship_candidates_prefer_distinct_passages(self):
        src = source(120)
        search = Search(src, FixtureProvider(), Config(max_relationships=2), None)
        target = Span(100, 105)
        refs = [
            (EvidenceRef(src, src.name, Span(1, 20), "source"), Decimal("0.99")),
            (EvidenceRef(src, src.name, Span(5, 12), "source"), Decimal("0.98")),
            (EvidenceRef(src, src.name, Span(50, 60), "source"), Decimal("0.90")),
        ]
        search.evidence_by_target[target] = {
            (r.kind, r.label, r.source.sha256, r.span.start, r.span.end):
            {"ref": r, "score": score, "state": {}, "depth": 0}
            for r, score in refs
        }
        selected = search._relationship_candidates(target)
        self.assertEqual([r["ref"].span for r in selected],
                         [Span(1, 20), Span(50, 60)])

    def test_project_index_is_bounded_and_skips_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("hello\n")
            (root / "b.bin").write_bytes(b"\x00\xff")
            index = ProjectIndex.read(root, max_files=8, max_file_bytes=100,
                                      max_total_bytes=1000)
            self.assertEqual([e.relpath for e in index.entries], ["a.txt"])
            self.assertGreaterEqual(index.skipped_binary_or_invalid, 1)

    def test_project_index_excludes_output_and_spec_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "main.py"
            spec = root / "spec.txt"
            output = root / "runs"
            output.mkdir()
            primary.write_text("main\n")
            spec.write_text("spec\n")
            (output / "old.json").write_text("{}\n")
            (root / "helper.py").write_text("helper\n")
            index = ProjectIndex.read(root, primary_path=primary,
                                      exclude_paths=(spec, output))
            self.assertEqual([e.relpath for e in index.entries], ["helper.py"])


if __name__ == "__main__":
    unittest.main()
