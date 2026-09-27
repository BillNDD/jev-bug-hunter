"""Source mapping, offset geometry and speculative planning regressions."""
import hashlib
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from bug_hunter.core import Config, Source, Span, windows, multiscale, contiguous_candidates

class SourceTests(unittest.TestCase):
    def read(self, data, **kwargs):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.txt"
            path.write_bytes(data)
            return Source.read(path, **kwargs)

    def test_cr_lf_crlf_only_and_original_byte_hash(self):
        data = "a\r\nb\rc\nd\u2028e\x0cf\x85g\n".encode()
        loaded = self.read(data)
        self.assertEqual(loaded.lines, ("a", "b", "c", "d\u2028e\x0cf\x85g"))
        self.assertEqual(loaded.sha256, hashlib.sha256(data).hexdigest())

    def test_empty_trailing_and_blank_lines(self):
        for data, expected in [(b"", ()), (b"a", ("a",)),
                               (b"a\n", ("a",)), (b"\n", ("",)),
                               (b"a\n\n", ("a", ""))]:
            with self.subTest(data=data):
                self.assertEqual(self.read(data).lines, expected)

    def test_bom_and_nonascii_survive(self):
        self.assertEqual(self.read(b"\xef\xbb\xbf" + "\u5b57\n".encode()).lines,
                         ("\u5b57",))

    def test_invalid_encoding_binary_and_limits_rejected(self):
        for data, kwargs in [(b"\xff", {}), (b"a\x00b", {}),
                             (b"1234", {"max_bytes": 3}),
                             (b"a\nb", {"max_lines": 1})]:
            with self.subTest(data=data, kwargs=kwargs):
                with self.assertRaises((ValueError, UnicodeError, OSError)):
                    self.read(data, **kwargs)

    def test_directory_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises((ValueError, OSError)):
                Source.read(Path(td))


class GeometryTests(unittest.TestCase):
    def test_all_widths_cover_have_fixed_size_and_remain_bounded(self):
        for length in range(1, 101):
            for width in range(1, 33):
                for offset in (False, True):
                    parent = Span(7, length + 6)
                    actual = windows(parent, width, offsets=offset)
                    pairs = [(s.start, s.end) for s in actual]
                    self.assertEqual(pairs, sorted(set(pairs)))
                    covered = set()
                    for child in actual:
                        self.assertGreaterEqual(child.start, parent.start)
                        self.assertLessEqual(child.end, parent.end)
                        self.assertEqual(child.end - child.start + 1,
                                         min(width, length))
                        covered.update(range(child.start, child.end + 1))
                    self.assertEqual(covered, set(range(7, length + 7)))

    def test_73_line_example_is_full_width_with_tail_anchor(self):
        actual = windows(Span(1, 73), 48)
        self.assertEqual([(s.start, s.end) for s in actual],
                         [(1, 48), (25, 72), (26, 73)])

    def test_invalid_configuration_rejected(self):
        for values in [{"width": 0}, {"min_width": 0},
                       {"width": 12, "min_width": 24}, {"max_depth": -1},
                       {"context_lines": -1}, {"max_calls": 0},
                       {"drill_threshold": Decimal("NaN")},
                       {"report_threshold": Decimal("1.01")}]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    Config(**values)



class MultiscaleTests(unittest.TestCase):
    def test_default_48_has_eleven_unique_targets(self):
        tree, capped = multiscale(Span(1, 48), 12, 4, 1000)
        self.assertFalse(capped)
        self.assertEqual(len(tree), 11)
        self.assertEqual([s for s,d in tree if d == 1],
                         [Span(1,24), Span(13,36), Span(25,48)])
        self.assertEqual({s.size for s,d in tree}, {48,24,12})

    def test_depth_limits_are_independent_of_scores(self):
        for depth, count in [(0,1),(1,4),(2,11)]:
            tree, capped = multiscale(Span(1,48),12,depth,1000)
            self.assertEqual(len(tree), count)
            self.assertFalse(capped)

    def test_planning_cap_is_explicit(self):
        tree, capped = multiscale(Span(1,48),1,16,5)
        self.assertEqual(len(tree), 5)
        self.assertTrue(capped)

    def test_all_contiguous_candidates_and_bounds(self):
        for n in range(1,21):
            options = contiguous_candidates(Span(50,49+n))
            self.assertEqual(len(options), n*(n+1)//2)
            self.assertEqual(len(options), len(set(options)))
            self.assertTrue(all(50<=p.start<=p.end<=49+n for p in options))
            self.assertLessEqual(len(options)+2,255)
        self.assertEqual(len(contiguous_candidates(Span(1,12))),78)
        with self.assertRaises(ValueError):
            contiguous_candidates(Span(1,21))

    def test_new_bounds_reject_booleans_nonfinite_and_invalid_limits(self):
        for values in ({"max_questions":0},{"max_windows":0},
                       {"max_localizations":-1},{"max_context_packets":65},
                       {"choice_confidence":True},{"context_threshold":float("nan")},
                       {"whole_file":1},{"width":4097}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Config(**values)

if __name__ == "__main__":
    unittest.main()
