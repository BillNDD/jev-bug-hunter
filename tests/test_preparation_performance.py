"""Request equivalence oracles for preparation optimizations; no live calls."""
import copy
import random
import unittest
from unittest.mock import patch

from bug_hunter.core import Source, Span
from bug_hunter.evidence import EvidenceRef, fitting_pair_state, pair_state
from bug_hunter.jev import JevError, encode_request, MAX_STATE_BYTES, _json
from bug_hunter.questions import (choice, fitting_state, load_pack, noul,
                                  pack_questions, prepared_question_batches,
                                  screening, state_for)


def reference_pages(pack, region, targets, state):
    """Original growing-menu/full-envelope algorithm, kept as an oracle."""
    pages, page = [], []
    for target in targets:
        candidate = page + [target]
        fits = len(candidate) <= 200
        if fits and state is not None:
            try:
                encode_request(state, {"rank": choice(pack, "rank", region, candidate)})
            except JevError as exc:
                if exc.code != "request_too_large":
                    raise
                fits = False
        if page and not fits:
            pages.append(page)
            page = []
        page.append(target)
    if page:
        pages.append(page)
    return [choice(pack, "rank", region, page) for page in pages]


def reference_fit(make_state, halo, probe):
    """Original reconstruct-everything loop with the exact same halo sequence."""
    while True:
        state = make_state(halo)
        try:
            encode_request(state, {"probe": probe})
            return state
        except JevError as exc:
            if exc.code != "request_too_large":
                raise
            if halo == 0:
                return None
            halo //= 2


class PreparationEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.pack, _ = load_pack()

    def test_rank_pages_preserve_exact_menu_and_batch_wires(self):
        rng = random.Random(20260930)
        cases = [(200, "x" * 15000), (201, "x" * 16300),
                 (401, ""), (1, "x" * 17000), (0, "")]
        cases.extend((rng.choice((1, 11, 65, 199, 200, 255)),
                      rng.choice(("雪", '\\"\n', "plain")) * rng.randrange(0, 4000))
                     for _ in range(16))
        for count, text in cases:
            targets = [Span(i, i + i % 5) for i in range(100000, 100000 + count)]
            if count > 2:
                targets[1] = targets[0]  # Original dictionary overwrite semantics.
            region, state = Span(100000, 101000), {"text": text}
            with self.subTest(count=count, state_length=len(text)):
                expected_pages = reference_pages(self.pack, region, targets, state)
                qs = screening(self.pack, region, targets, bug_lenses=False, state=state)
                self.assertEqual([q for k, q in qs.items() if k.startswith("rank_")],
                                 expected_pages)
                expected = {k: v for k, v in qs.items() if not k.startswith("rank_")}
                expected.update({f"rank_{i}": q for i, q in enumerate(expected_pages)})
                expected_wire = [wire if wire is not None else failure
                                 for _, failure, wire in prepared_question_batches(state, expected)]
                actual_wire = [wire if wire is not None else failure
                               for _, failure, wire in prepared_question_batches(state, qs)]
                self.assertEqual(actual_wire, expected_wire)

    def test_rank_invalid_state_and_question_fail_closed(self):
        targets, region = [Span(1, 1)], Span(1, 1)
        deep = {}
        for _ in range(34):
            deep = {"next": deep}
        pack = dict(self.pack)
        pack["rank"] = "q" * 12001
        for candidate_pack, state in ((self.pack, []), (self.pack, {"x": float("nan")}),
                                      (self.pack, deep), (pack, {})):
            for run in (reference_pages,
                        lambda p, r, t, s: screening(p, r, t, state=s)):
                with self.assertRaises(JevError) as caught:
                    run(candidate_pack, region, targets, state)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_empty_target_iterator_does_not_introduce_an_encoding_probe(self):
        # A generator already consumed by screening's first comprehension has
        # no rank candidates, just as in the previous implementation.
        result = screening(self.pack, Span(1, 1), iter((Span(1, 1),)),
                           bug_lenses=False, state=[])
        self.assertEqual(list(result), ["screen_L1-1", "need_context"])

    def test_rank_serialization_is_per_page_not_per_target(self):
        from bug_hunter import questions
        targets = [Span(i, i) for i in range(1, 201)]
        with patch.object(questions, "encode_request", wraps=encode_request) as encoder:
            result = screening(self.pack, Span(1, 200), targets,
                               bug_lenses=False, state={"text": "x" * 15000})
        pages = sum(key.startswith("rank_") for key in result)
        self.assertEqual(encoder.call_count, pages)
        self.assertLess(encoder.call_count, 10)

    def test_fitting_state_preserves_exact_halo_and_evidence(self):
        source = Source.from_text(("雪 \\\"" * 15 + "\n") * 500, "primary.py")
        region, extras = Span(245, 255), (Span(300, 308), Span(280, 282))
        probe = noul(self.pack, "verify", region)
        for spec in (None, "line one\r\nline two\u2028literal\n" * 100, "x" * 18000):
            for halo in (0, 2, 17, 256):
                scope = {"mode": "isolate", "arbitrary": "雪"}
                expected = reference_fit(lambda n: state_for(source, region, n, spec, extras,
                                                             context_scope=scope), halo, probe)
                actual = fitting_state(source, region, halo, spec, probe, extras, context_scope=scope)
                self.assertEqual(actual, expected)
                if actual is not None:
                    self.assertEqual(encode_request(actual, {"probe": probe}),
                                     encode_request(expected, {"probe": probe}))

    def test_pair_fitting_preserves_same_file_and_distinct_equal_content_files(self):
        primary = Source.from_text(("line " + "x" * 100 + "\n") * 200, "primary.py")
        duplicate = Source.from_text(("line " + "x" * 100 + "\n") * 200, "duplicate.py")
        region, probe = Span(50, 55), noul(self.pack, "verify", Span(50, 55))
        refs = [EvidenceRef(primary, primary.name, Span(75, 80)),
                EvidenceRef(duplicate, duplicate.name, Span(75, 80), "project")]
        for ref in refs:
            expected = reference_fit(lambda n: pair_state(primary, region, n, "spec\n" * 40,
                                                          ref, context_scope="project"), 100, probe)
            actual = fitting_pair_state(primary, region, 100, "spec\n" * 40, ref, probe,
                                        context_scope="project")
            self.assertEqual(actual, expected)
            self.assertEqual("related_sources" in actual, ref.source is not primary)
            first_id = ref.id
            actual["evidence_coordinates"][0]["id"] = "mutated result"
            self.assertEqual(ref.id, first_id)
            fresh = pair_state(primary, region, 0, None, ref)
            self.assertEqual(fresh["evidence_coordinates"][0]["id"], first_id)

    def test_prepared_batches_own_snapshot_and_keep_named_failures(self):
        state = {"text": "x" * 15000}
        qs = {"too_large": noul(self.pack, "screen", Span(1, 1))}
        qs["too_large"]["instructions"] = "q" * 11000
        qs.update({f"q{i}": noul(self.pack, "screen", Span(i + 1, i + 1)) for i in range(70)})
        prepared = list(prepared_question_batches(state, qs))
        self.assertEqual([(batch, failed) for batch, failed, _ in prepared],
                         list(pack_questions(state, qs)))
        self.assertEqual(prepared[0], ({}, "too_large", None))
        for batch, failed, wire in prepared:
            if failed is None:
                self.assertEqual(wire, encode_request(state, batch))
        original = copy.deepcopy(prepared)
        qs["q0"]["criteria"]["true"] = "mutated"
        state["text"] = "mutated"
        self.assertEqual(prepared, original)


class MandatoryViewPreflightTests(unittest.TestCase):
    def setUp(self):
        self.pack, _ = load_pack()
        self.probe = noul(self.pack, "screen", Span(1, 1))

    def assert_reference(self, source, region, halo, spec=None, extras=(), probe=None, scope=None):
        probe = self.probe if probe is None else probe
        def original():
            return reference_fit(lambda n: state_for(source, region, n, spec, extras,
                                                     context_scope=scope), halo, probe)
        def actual():
            return fitting_state(source, region, halo, spec, probe, extras, context_scope=scope)
        try:
            expected = original()
        except Exception as exc:
            with self.assertRaises(type(exc)) as caught:
                actual()
            if isinstance(exc, JevError):
                self.assertEqual(caught.exception.code, exc.code)
        else:
            got = actual()
            self.assertEqual(got, expected)
            if got is not None:
                self.assertEqual(encode_request(got, {"probe": probe}),
                                 encode_request(expected, {"probe": probe}))

    def test_necessarily_oversized_view_validates_probe_without_rendering_rows(self):
        from bug_hunter import questions
        source = Source.from_text(("x" * 320 + "\n") * 3000, "large.py")
        with patch.object(questions, "state_for", wraps=state_for) as renderer, \
             patch.object(questions, "encode_request", wraps=encode_request) as encoder:
            result = fitting_state(source, Span(1400, 1447), 12, None, self.probe,
                                   context_scope="owner_isolated")
        self.assertIsNone(result)
        self.assertEqual(renderer.call_count, 0)
        self.assertEqual(encoder.call_count, 1)
        self.assertEqual(encoder.call_args.args[1], {"probe": self.probe})

    def test_exact_fit_boundary_and_single_oversized_line_keep_same_result(self):
        base = Source.from_text("\n", "primary.py")
        count = len(_json(state_for(base, Span(1, 1), 0, None)))
        for delta in (-1, 0, 1, 50, 5000):
            source = Source.from_text("x" * (MAX_STATE_BYTES - count + delta), "primary.py")
            self.assert_reference(source, Span(1, 1), 0)

    def test_larger_halo_can_merge_excerpts_and_fit_when_zero_halo_does_not(self):
        base = Source.from_text("\n\n\n", "primary.py")
        region, extras = Span(1, 1), (Span(3, 3),)
        count = len(_json(state_for(base, region, 0, None, extras)))
        source = Source.from_text("x" * (MAX_STATE_BYTES - count + 1) + "\n\n\n", "primary.py")
        self.assertGreater(len(_json(state_for(source, region, 0, None, extras))), MAX_STATE_BYTES)
        self.assertLessEqual(len(_json(state_for(source, region, 1, None, extras))), MAX_STATE_BYTES)
        actual = fitting_state(source, region, 1, None, self.probe, extras)
        self.assertIsNotNone(actual)
        self.assert_reference(source, region, 1, extras=extras)

    def test_surrogate_in_optional_halo_spec_probe_or_metadata_preserves_error(self):
        source = Source("primary.py", "a" * 64, ("x" * 20000, "\ud800"), "utf-8")
        bad_probe = copy.deepcopy(self.probe)
        bad_probe["instructions"] = "\ud800"
        self.assert_reference(source, Span(1, 1), 1)
        self.assert_reference(source, Span(1, 1), 1, probe=bad_probe)
        valid_source = Source.from_text("x" * 20000, "primary.py")
        self.assert_reference(valid_source, Span(1, 1), 0, spec="\ud800")
        self.assert_reference(valid_source, Span(1, 1), 0, probe=bad_probe)
        self.assert_reference(valid_source, Span(1, 1), 0, scope="\ud800")
        bad_name = Source("\ud800", "a" * 64, ("x" * 20000,), "utf-8")
        self.assert_reference(bad_name, Span(1, 1), 0)

    def test_mutable_library_fields_fall_back_and_do_not_leave_stale_rejection(self):
        lines = ["x" * 20000]
        source = Source("mutable.py", "a" * 64, lines, "utf-8")
        self.assert_reference(source, Span(1, 1), 0)
        lines[0] = "fits now"
        self.assert_reference(source, Span(1, 1), 0)
        self.assertIsNotNone(fitting_state(source, Span(1, 1), 0, None, self.probe))
        source = Source("mutable.py", ["mutable hash"], ("x" * 20000,), "utf-8")
        self.assert_reference(source, Span(1, 1), 0)
        source = Source("nested.py", "a" * 64, ("x" * 20000, ["mutable line"]), "utf-8")
        self.assert_reference(source, Span(1, 1), 1)

    def test_unsupported_geometry_and_extra_iterators_keep_original_path(self):
        source = Source.from_text(("x" * 320 + "\n") * 100, "primary.py")
        self.assert_reference(source, Span(1, 2), 12, extras=(Span(101, 101),))
        self.assert_reference(source, Span(100, 110), 12)
        self.assert_reference(source, Span(1, 48), 0, extras=[Span(90, 95)])
        expected = reference_fit(lambda n: state_for(source, Span(1, 48), n, None,
                                                     extras=(Span(90, 95),)), 0, self.probe)
        actual = fitting_state(source, Span(1, 48), 0, None, self.probe,
                               extras=iter((Span(90, 95),)))
        self.assertEqual(actual, expected)

    def test_varied_unicode_ranges_and_specs_match_original_fitting(self):
        rng = random.Random(20261001)
        for _ in range(35):
            line = rng.choice(("x", "雪", '\\"', "\t")) * rng.randrange(100, 700)
            source = Source.from_text((line + "\n") * 90, "sample.py")
            region = Span(rng.randrange(1, 20), rng.randrange(21, 50))
            extra = (Span(70, 75),) if rng.randrange(2) else ()
            spec = "requirement\n" * rng.randrange(0, 100)
            self.assert_reference(source, region, rng.randrange(0, 30), spec=spec,
                                  extras=extra, scope="owner_isolated")


if __name__ == "__main__":
    unittest.main()
