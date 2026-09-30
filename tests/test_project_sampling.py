"""Project sampling must preserve the old enumeration and actual search inputs."""
from dataclasses import replace
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bug_hunter.claims import ROLES
from bug_hunter.core import Config, Span, windows
from bug_hunter.engine import Search
from bug_hunter.evidence import EvidenceRef
from bug_hunter.jev import encode_request
from bug_hunter.repository import ProjectEntry, ProjectIndex
from tests.support import FixtureProvider, source


def project(entries):
    return ProjectIndex(".", entries, len(entries), len(entries), 0, False,
                        0, 0, 0, False, False)


def enumerated_roots(search, target):
    """Independent oracle: the original full materialization and spread."""
    if search.project is None:
        return []
    refs = []
    width = max(search.cfg.evidence_min_width, search.cfg.width * 2)
    for entry in search.project.entries:
        if not entry.source.lines:
            continue
        entire = Span(1, len(entry.source.lines))
        for span in windows(entire, min(width, entire.size)):
            refs.append(EvidenceRef(entry.source, entry.relpath, span, "project"))
    total = len(refs)
    limited = total > search.cfg.max_project_candidates
    refs = search._spread(refs, search.cfg.max_project_candidates)
    if limited:
        search.issue("project_candidate_pool_limited", target, "evidence-project")
        search.events.append({"phase": "project_candidate_pool", "available": total,
                              "examined": len(refs), "limited": True})
    return refs


class ScopedProvider(FixtureProvider):
    def answer(self, qid, question, state):
        if qid in ROLES:
            bound = state["scoped_proposition"]["target"]
            present = self.contains_bug(Span(bound["start_line"], bound["end_line"]))
            score = .95 if qid == ROLES[0] and present else .05
            return {"type": "noul", "noul": score}
        return super().answer(qid, question, state)


class ProjectSamplingTests(unittest.TestCase):
    def searches(self, inventory, cfg):
        return [Search(source(12), FixtureProvider(), cfg, None,
                       project=inventory, context_scope="project") for _ in range(2)]

    def compare_roots(self, candidate, reference, target):
        actual = candidate._project_evidence_roots(target)
        expected = enumerated_roots(reference, target)
        self.assertEqual([r.as_dict() for r in actual], [r.as_dict() for r in expected])
        self.assertEqual(candidate.events, reference.events)
        self.assertEqual(candidate.issues, reference.issues)
        for a, b in zip(actual, expected):
            self.assertIs(a.source, b.source)
        return actual

    def test_random_inventories_caps_and_multiple_target_events_match_enumeration(self):
        rng = random.Random(80719)
        for trial in range(160):
            sizes = [rng.randrange(0, 301) for _ in range(rng.randrange(0, 10))]
            entries = tuple(ProjectEntry(f"file_{i}.txt", source(n)) for i, n in enumerate(sizes))
            cfg = Config(width=rng.randrange(2, 25), min_width=1,
                         evidence_min_width=rng.randrange(1, 60),
                         max_project_candidates=rng.choice((1, 2, 3, 4, 7, 16, 128)))
            candidate, reference = self.searches(project(entries), cfg)
            with self.subTest(trial=trial, sizes=sizes):
                for target in (Span(1, 2), Span(8, 12), Span(1, 2)):
                    self.compare_roots(candidate, reference, target)

    def test_only_selected_references_are_materialized(self):
        shared = source(50000, "x")
        inventory = project(tuple(ProjectEntry(f"file_{i}.txt", shared) for i in range(8)))
        candidate, reference = self.searches(inventory, Config(width=2, min_width=1,
                    evidence_min_width=1, max_project_candidates=7))
        with patch("bug_hunter.engine.EvidenceRef", wraps=EvidenceRef) as created, \
             patch("bug_hunter.engine.windows", side_effect=AssertionError("full enumeration")):
            actual = candidate._project_evidence_roots(Span(1, 2))
        self.assertEqual(created.call_count, 7)
        expected = enumerated_roots(reference, Span(1, 2))
        self.assertEqual([r.as_dict() for r in actual], [r.as_dict() for r in expected])
        self.assertEqual(candidate.events, reference.events)
        self.assertEqual(candidate.issues, reference.issues)

    def test_replaced_inventory_and_returned_lists_do_not_reuse_stale_geometry(self):
        inventory = project((ProjectEntry("z.txt", source(7)),
                             ProjectEntry("a.txt", source(39))))
        candidate, reference = self.searches(inventory, Config(width=3, min_width=1,
                    evidence_min_width=7, max_project_candidates=4))
        first = self.compare_roots(candidate, reference, Span(1, 2))
        first.clear()
        self.assertTrue(self.compare_roots(candidate, reference, Span(1, 2)))
        updated = replace(inventory, entries=(ProjectEntry("z.txt", source(73)),
                           ProjectEntry("a.txt", source(8)), ProjectEntry("b.txt", source(0))))
        candidate.project = reference.project = updated
        self.compare_roots(candidate, reference, Span(8, 12))
        candidate.cfg = reference.cfg = replace(candidate.cfg, max_project_candidates=1)
        selected = self.compare_roots(candidate, reference, Span(3, 4))
        self.assertEqual(selected[0].label, "z.txt")  # Caller inventory order, not lexical order.

    def test_mutable_and_custom_library_inventories_use_original_fallback(self):
        mutable = replace(source(15), lines=list(source(15).lines))
        entries = [ProjectEntry("mutable.txt", mutable)]
        inventories = (project(entries), project(tuple(entries)),
                       SimpleNamespace(entries=entries, limited=False))
        for inventory in inventories:
            candidate, reference = self.searches(inventory, Config(width=3, min_width=1,
                        max_project_candidates=3))
            with patch("bug_hunter.engine._window_count", side_effect=AssertionError("fast path")):
                self.compare_roots(candidate, reference, Span(1, 2))
                mutable.lines.extend(["extra"] * 13)
                self.compare_roots(candidate, reference, Span(8, 12))

    def test_custom_configuration_keeps_original_cap_read_order(self):
        class DynamicCap(Config):
            def __getattribute__(self, name):
                if name == "max_project_candidates":
                    pending = object.__getattribute__(self, "__dict__").get("_pending_caps")
                    if pending:
                        return pending.pop(0)
                return super().__getattribute__(name)

        inventory = project((ProjectEntry("helper.txt", source(80)),))
        candidate, reference = self.searches(inventory, Config())
        for search in (candidate, reference):
            cfg = DynamicCap(width=3, min_width=1, max_project_candidates=3)
            object.__setattr__(cfg, "_pending_caps", [1, 3])
            search.cfg = cfg
        with patch("bug_hunter.engine._window_count", side_effect=AssertionError("fast path")):
            selected = self.compare_roots(candidate, reference, Span(1, 2))
        self.assertEqual(len(selected), 3)

    def test_secondary_evidence_cap_keeps_the_existing_two_stage_spread(self):
        inventory = project((ProjectEntry("helper.txt", source(100)),))
        cfg = Config(width=4, min_width=1, evidence_min_width=1,
                     max_project_candidates=4, max_evidence_candidates=3,
                     evidence_max_depth=0)
        candidate, reference = self.searches(inventory, cfg)
        target = Span(1, 2)
        group = {"id": "fixture", "region": target, "state": candidate.state_for(target)}
        candidate._hierarchical_evidence_search(target, "project", group)
        with patch.object(Search, "_project_evidence_roots", enumerated_roots):
            reference._hierarchical_evidence_search(target, "project", group)
        self.assertEqual(candidate.evidence_searches, reference.evidence_searches)
        self.assertEqual(candidate.events, reference.events)
        self.assertEqual(candidate.issues, reference.issues)
        self.assertEqual(candidate.gw.requests, reference.gw.requests)
        candidates = windows(Span(1, 100), 8)
        expected = Search._spread(Search._spread(candidates, 4), 3)
        actual = [Span(row["candidate"]["start_line"], row["candidate"]["end_line"])
                  for row in candidate.evidence_searches]
        self.assertEqual(actual, expected)
        self.assertNotEqual(actual, Search._spread(candidates, 3))

    def test_complete_search_reports_and_request_bytes_match_old_enumeration(self):
        inventory = project(tuple(ProjectEntry(f"helper_{i}.txt", source(n))
                            for i, n in enumerate((0, 13, 35, 40, 65))))
        for policy in ("legacy-v1", "scoped-v2-beta"):
            cfg = Config(width=8, min_width=4, max_depth=1, search_policy=policy,
                         whole_file=False, max_action_steps=0, max_localizations=0,
                         max_project_candidates=4, max_evidence_candidates=3,
                         evidence_max_depth=0, max_relationships=0)
            outputs = []
            for original in (False, True):
                gateway = ScopedProvider(((2, 2),))
                search = Search(source(12), gateway, cfg, None, project=inventory,
                                context_scope="project")
                if original:
                    with patch.object(Search, "_project_evidence_roots", enumerated_roots):
                        report = search.run()
                else:
                    report = search.run()
                self.assertTrue(report["evidence_searches"])
                self.assertTrue(report["findings"])
                outputs.append((report, [encode_request(r["state"], r["questions"])
                                         for r in gateway.requests]))
            with self.subTest(policy=policy):
                self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
