"""Physical-source, geometry and inventory equivalence for fast local preparation."""
from collections import deque
import hashlib
from itertools import product
from pathlib import Path
import random
import os
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from bug_hunter.core import (Source, Span, _coverage, _window_at, _window_count,
                             multiscale, physical_lines, windows)
from bug_hunter.repository import ProjectIndex


def reference_lines(text):
    """Independent scanner: Unicode separators other than CR/LF are content."""
    rows, start, index = [], 0, 0
    while index < len(text):
        if text[index] not in "\r\n":
            index += 1
            continue
        rows.append(text[start:index])
        index += 2 if text[index:index + 2] == "\r\n" else 1
        start = index
    if start < len(text):
        rows.append(text[start:])
    return tuple(rows)


def reference_multiscale(span, minimum, max_depth, limit):
    pending, seen, result = deque([(span, 0)]), {span}, []
    while pending:
        if len(result) >= limit:
            return result, True
        target, depth = pending.popleft()
        result.append((target, depth))
        if target.size > minimum and depth < max_depth:
            width = max(minimum, (target.size + 1) // 2)
            for child in windows(target, width):
                if child not in seen:
                    seen.add(child)
                    pending.append((child, depth + 1))
    return result, False


class SourcePreparationTests(unittest.TestCase):
    def test_all_short_cr_lf_sequences_preserve_physical_lines(self):
        for length in range(7):
            for chars in product(("x", "\r", "\n"), repeat=length):
                text = "".join(chars)
                self.assertEqual(physical_lines(text), reference_lines(text), repr(text))
        for text in ("a\u2028b\u2029c\x85d\x0be\x0cf", "\u5b57\n\u00e9\r\nx"):
            self.assertEqual(physical_lines(text), reference_lines(text))

    def test_bounded_read_matches_reference_at_every_limit_boundary(self):
        rng = random.Random(82173)
        samples = ["", "\r", "\n", "\r\n", "a\n\n", "x" * 1024]
        samples += ["".join(rng.choice(("x", "\u5b57", "\u2028", "\r", "\n", "\r\n"))
                            for _ in range(rng.randrange(1, 80))) for _ in range(40)]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "source.txt"
            for encoding in ("utf-8-sig", "utf-16"):
                for text in samples:
                    raw = text.encode(encoding)
                    target.write_bytes(raw)
                    expected = reference_lines(text)
                    for limit in {1, max(1, len(expected) - 1), max(1, len(expected)), len(expected) + 1}:
                        with self.subTest(encoding=encoding, text=repr(text), limit=limit):
                            if len(expected) > limit:
                                with self.assertRaisesRegex(ValueError, "^too_many_lines$"):
                                    Source.read(target, encoding=encoding, max_lines=limit)
                            else:
                                source = Source.read(target, encoding=encoding, max_lines=limit)
                                self.assertEqual(source.lines, expected)
                                self.assertEqual(source.byte_count, len(raw))
                                self.assertEqual(source.sha256, hashlib.sha256(raw).hexdigest())
                                self.assertEqual(source.path, str(target.resolve()))

    def test_newline_flood_and_oversized_line_remain_explicit_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "source.txt"
            for raw, kwargs, error in (
                    (b"\n" * 250000, {"max_lines": 200000}, "too_many_lines"),
                    (b"\r\n" * 250000, {"max_lines": 200000}, "too_many_lines"),
                    (b"x" * 100, {"max_bytes": 99}, "input_too_large"),
                    (b"x\x00\n", {}, "nul_in_text_input")):
                target.write_bytes(raw)
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, "^" + error + "$"):
                    Source.read(target, **kwargs)

    def test_crlf_across_counting_blocks_preserves_caps_and_empty_lines(self):
        samples = ("x" * 65535 + "\r\nb\rc\n",
                   "x" * 65534 + "\r\r\nb",
                   "\n" * 65535 + "\r\n",
                   "\n" * 65535 + "\r\nx",
                   "x\r\n" * 65536,
                   "\r\n" * 100001)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.txt"
            for text in samples:
                path.write_bytes(text.encode())
                expected = reference_lines(text)
                self.assertEqual(Source.read(path, max_lines=len(expected)).lines, expected)
                with self.assertRaisesRegex(ValueError, "^too_many_lines$"):
                    Source.read(path, max_lines=len(expected) - 1)

    def test_allocation_hint_never_admits_or_accounts_unread_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.txt"
            original_fstat = os.fstat
            for initial, final, maximum, too_large in (
                    (b"x", b"expanded\nsource\n", 100, False),
                    (b"x", b"x" * 101, 100, True),
                    (b"x" * 200, b"small\n", 100, False),
                    (b"x", b"", 100, False)):
                path.write_bytes(initial)
                def changed_after_stat(fd):
                    status = original_fstat(fd)
                    path.write_bytes(final)
                    return status
                with self.subTest(initial=len(initial), final=len(final)), \
                        patch("bug_hunter.core.os.fstat", changed_after_stat):
                    if too_large:
                        with self.assertRaisesRegex(ValueError, "^input_too_large$"):
                            Source.read(path, max_bytes=maximum)
                    else:
                        source = Source.read(path, max_bytes=maximum)
                        self.assertEqual(source.byte_count, len(final))
                        self.assertEqual(source.sha256, hashlib.sha256(final).hexdigest())
                        self.assertEqual(source.lines, reference_lines(final.decode()))
            raw = b"regular file with a zero size hint\n"
            path.write_bytes(raw)
            def zero_size_hint(fd):
                return SimpleNamespace(st_mode=original_fstat(fd).st_mode, st_size=0)
            with patch("bug_hunter.core.os.fstat", zero_size_hint):
                source = Source.read(path)
            self.assertEqual(source.byte_count, len(raw))
            self.assertEqual(source.sha256, hashlib.sha256(raw).hexdigest())


class GeometryPreparationTests(unittest.TestCase):
    def test_indexed_windows_match_every_small_ordered_grid(self):
        for origin in (1, 7, 100001):
            for length in range(1, 101):
                parent = Span(origin, origin + length - 1)
                for width in range(1, 34):
                    for offsets in (False, True):
                        expected = windows(parent, width, offsets)
                        actual_count = _window_count(parent, width, offsets)
                        self.assertEqual(actual_count, len(expected),
                                         (origin, length, width, offsets))
                        self.assertEqual([_window_at(parent, width, i, offsets)
                                          for i in range(actual_count)], expected,
                                         (origin, length, width, offsets))

    def test_indexed_huge_grids_select_only_requested_positions(self):
        origin, length = 101, 10 ** 30 + 19
        parent = Span(origin, origin + length - 1)
        for width in (1, 2, 3, 5, 48, 4095, 4096):
            for offsets in (False, True):
                # Independent rank oracle: count regular/shifted starts before
                # each selected start. No reference grid is materialized.
                expected_count = (length - width) // width + 1
                half = width // 2 if offsets and width > 1 else None
                if half is not None:
                    expected_count += max(0, (length - width - half) // width + 1)
                last = parent.end - width + 1
                anchor_on_grid = ((last - origin) % width == 0 or
                                  half is not None and (last - origin - half) % width == 0)
                if not anchor_on_grid:
                    expected_count += 1
                self.assertEqual(_window_count(parent, width, offsets), expected_count)
                indices = {0, 1, 2, expected_count // 3, expected_count // 2,
                           expected_count - 2, expected_count - 1}
                for index in sorted(i for i in indices if 0 <= i < expected_count):
                    actual = _window_at(parent, width, index, offsets)
                    self.assertEqual(actual.size, width)
                    self.assertTrue(origin <= actual.start <= last)
                    earlier = (actual.start - origin + width - 1) // width
                    if half is not None:
                        earlier += max(0, (actual.start - origin - half + width - 1) // width)
                    self.assertEqual(earlier, index)
                    self.assertTrue(actual.start == last or
                                    (actual.start - origin) % width == 0 or
                                    half is not None and (actual.start - origin - half) % width == 0)
                self.assertEqual(_window_at(parent, width, expected_count - 1, offsets).end,
                                 parent.end)

    def test_indexed_window_argument_validation_and_single_span_identity(self):
        parent = Span(7, 12)
        self.assertIs(_window_at(parent, 48, 0), parent)
        for width in (0, -1, True, 2.0, "2"):
            with self.assertRaisesRegex(ValueError, "^invalid_window_width$"):
                _window_count(parent, width)
            with self.assertRaisesRegex(ValueError, "^invalid_window_width$"):
                _window_at(parent, width, 0)
        for index in (-1, _window_count(parent, 2)):
            with self.assertRaisesRegex(IndexError, "^window_index_out_of_range$"):
                _window_at(parent, 2, index)
        for index in (True, 0.0, "0"):
            with self.assertRaisesRegex(ValueError, "^invalid_window_index$"):
                _window_at(parent, 2, index)

    def test_cached_geometry_preserves_breadth_first_order_and_every_cap(self):
        lengths = [*range(1, 20), 31, 32, 47, 48, 63, 64, 96, 127, 128, 129, 256]
        for length in lengths:
            for minimum in (1, 12, 48):
                for depth in (0, 1, 2, 4, 16):
                    parent = Span(103, 102 + length)
                    full = reference_multiscale(parent, minimum, depth, 100000)
                    for limit in {0, 1, 3, len(full[0]), len(full[0]) + 1}:
                        expected = (full[0][:limit], limit < len(full[0]))
                        self.assertEqual(multiscale(parent, minimum, depth, limit), expected,
                                         (length, minimum, depth, limit))

    def test_result_lists_are_owned_and_coordinates_do_not_bleed_between_roots(self):
        first = Span(1, 48)
        expected = reference_multiscale(first, 12, 4, 1000)
        result, _ = multiscale(first, 12, 4, 1000)
        result.clear()
        self.assertEqual(multiscale(first, 12, 4, 1000), expected)
        other = Span(1001, 1048)
        self.assertEqual(multiscale(other, 12, 4, 1000),
                         reference_multiscale(other, 12, 4, 1000))
        self.assertEqual(multiscale(Span(1, 4096), 12, 4, 1000),
                         reference_multiscale(Span(1, 4096), 12, 4, 1000))
        for minimum in (-1, 0, True, 1.0):
            self.assertEqual(multiscale(Span(1, 1), minimum, 4, 1),
                             reference_multiscale(Span(1, 1), minimum, 4, 1))

    def test_coverage_uses_exact_union_with_shuffled_equal_start_intervals(self):
        rng = random.Random(38191)
        for count in (0, 1, 13, 100, 1024):
            for trial in range(30):
                spans = []
                if count:
                    for _ in range(trial):
                        start = rng.randint(1, count)
                        end = rng.randint(start, min(count, start + 20))
                        spans.extend((Span(start, end), Span(start, start), Span(start, end)))
                rng.shuffle(spans)
                covered = {line for span in spans for line in range(span.start, span.end + 1)}
                missing, cursor = [], 1
                while cursor <= count:
                    if cursor in covered:
                        cursor += 1
                        continue
                    start = cursor
                    while cursor <= count and cursor not in covered:
                        cursor += 1
                    missing.append([start, cursor - 1])
                expected = {"assessed_lines": len(covered), "unassessed_ranges": missing,
                            "all_lines_assessed": not missing}
                self.assertEqual(_coverage(iter(spans), count), expected)


class InventoryPreparationTests(unittest.TestCase):
    def test_exclusions_use_path_components_and_inventory_caps_keep_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("output", "output_extra", "source"):
                (root / folder).mkdir()
                (root / folder / "a.py").write_bytes(b"x\n")
            (root / "primary.py").write_bytes(b"main\n")
            (root / "requirements.txt").write_bytes(b"contract\n")
            (root / ".env").write_bytes(b"synthetic policy exclusion\n")
            (root / "bad.bin").write_bytes(b"\x00")
            options = {"primary_path": root / "primary.py",
                       "exclude_paths": (root / "output", root / "requirements.txt")}
            full = ProjectIndex.read(root, **options)
            self.assertEqual([entry.relpath for entry in full.entries],
                             ["output_extra/a.py", "source/a.py"])
            self.assertEqual(full.files_considered, 6)
            self.assertEqual((full.skipped_secret, full.skipped_binary_or_invalid), (1, 1))
            self.assertFalse(full.limited)
            capped = ProjectIndex.read(root, max_files=1, **options)
            self.assertEqual([entry.relpath for entry in capped.entries], ["output_extra/a.py"])
            self.assertTrue(capped.limited_file_count)
            self.assertTrue(capped.limited)
            self.assertEqual(capped.bytes_loaded, 2)
            byte_capped = ProjectIndex.read(root, max_total_bytes=2, **options)
            self.assertEqual([entry.relpath for entry in byte_capped.entries], ["output_extra/a.py"])
            self.assertTrue(byte_capped.limited_total_bytes)
            self.assertEqual(byte_capped.bytes_loaded, 2)

    def test_expired_inventory_and_unreadable_resolution_are_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            with patch("bug_hunter.repository.time.monotonic", return_value=2):
                expired = ProjectIndex.read(root, deadline_at=1)
            self.assertTrue(expired.deadline_exceeded)
            self.assertTrue(expired.limited)
            resolve = Path.resolve
            def unavailable(path, *args, **kwargs):
                if path.name == "source":
                    raise PermissionError("synthetic unavailable directory")
                return resolve(path, *args, **kwargs)
            with patch.object(Path, "resolve", unavailable):
                unreadable = ProjectIndex.read(root)
            self.assertEqual(unreadable.unreadable_directories, 1)
            self.assertTrue(unreadable.limited)


if __name__ == "__main__":
    unittest.main()
