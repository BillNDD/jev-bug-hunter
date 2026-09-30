"""Immutable source coordinates and bounded configuration; no inference logic."""
from __future__ import annotations

import hashlib
import math
import os
import re
import stat
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache
from operator import attrgetter
from pathlib import Path


def physical_lines(text):
    """Only CR, LF and CRLF delimit original physical lines."""
    lines = text.split("\n") if "\r" not in text else re.split(r"\r\n|\r|\n", text)
    if lines[-1] == "":
        lines.pop()
    return tuple(lines)

@dataclass(frozen=True, order=True)
class Span:
    """Inclusive, one-based source lines."""

    start: int
    end: int

    def __post_init__(self):
        if (type(self.start) is not int or type(self.end) is not int
                or not 1 <= self.start <= self.end):
            raise ValueError("invalid_span")

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def id(self) -> str:
        return f"L{self.start}-{self.end}"


@dataclass(frozen=True)
class Source:
    name: str
    sha256: str
    lines: tuple[str, ...]
    encoding: str
    path: str | None = None
    byte_count: int | None = None

    @classmethod
    def read(cls, path, encoding="utf-8-sig", max_bytes=16777216,
             max_lines=200000) -> Source:
        """Read one handle. Hash its actual bytes; split only CR/LF separators."""
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("invalid_input_limit")
        if type(max_lines) is not int or max_lines < 1:
            raise ValueError("invalid_line_limit")
        path = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("input_must_be_regular_file")
            # Size is only an allocation hint. Read observed growth from the
            # same handle; only actual bytes decide admission and accounting.
            first_size = min(max_bytes + 1, max(0, status.st_size) + 1)
            raw = stream.read(first_size)
            if len(raw) == first_size and first_size < max_bytes + 1:
                raw += stream.read(max_bytes + 1 - len(raw))
        if len(raw) > max_bytes:
            raise ValueError("input_too_large")
        text = raw.decode(encoding, errors="strict")
        if "\x00" in text:
            raise ValueError("nul_in_text_input")
        # Reject newline floods before split can copy a large unused remainder.
        # C-level counts retain CR/LF-only coordinates without one Python match
        # object per line. A final terminator does not create another line.
        has_cr = False
        line_count = 0
        for start in range(0, len(text), 65536):
            end = start + 65536
            line_count += text.count("\n", start, end)
            cr_count = text.count("\r", start, end)
            if cr_count:
                has_cr = True
                # Attribute a cross-block CRLF pair to its CR; the LF is
                # counted in the next block, without double-counting the pair.
                line_count += cr_count - text.count("\r\n", start, end + 1)
            if line_count > max_lines:
                raise ValueError("too_many_lines")
        line_count += bool(text) and not text.endswith(("\r", "\n"))
        if line_count > max_lines:
            raise ValueError("too_many_lines")
        byte_count, source_hash = len(raw), hashlib.sha256(raw).hexdigest()
        del raw
        if has_cr:
            # Separate assignments release the previous full text before the
            # next normalization and before allocating individual line strings.
            text = text.replace("\r\n", "\n")
            text = text.replace("\r", "\n")
        lines = text.split("\n", max_lines)
        del text
        if lines[-1] == "":
            lines.pop()
        if len(lines) > max_lines:
            raise ValueError("too_many_lines")
        return cls(path.name, source_hash, tuple(lines), encoding,
                   str(path.resolve()), byte_count)


    @classmethod
    def from_text(cls, text: str, name="<inline-spec>") -> Source:
        """Number supplied text using the same physical-line convention as files."""
        if type(text) is not str or "\x00" in text:
            raise ValueError("invalid_inline_text")
        raw = text.encode("utf-8")
        return cls(Path(name).name, hashlib.sha256(raw).hexdigest(),
                   physical_lines(text), "utf-8", byte_count=len(raw))


@dataclass(frozen=True)
class Config:
    width: int = 48
    min_width: int = 12
    max_depth: int = 4
    context_lines: int = 12
    drill_threshold: Decimal = Decimal("0.60")
    report_threshold: Decimal = Decimal("0.80")
    context_threshold: Decimal = Decimal("0.60")
    choice_confidence: Decimal = Decimal("0.50")
    max_calls: int = 1000
    max_questions: int = 20000
    max_windows: int = 50000
    max_context_packets: int = 8
    max_localizations: int = 4
    whole_file: bool = True
    max_handoff_candidates: int = 64
    bug_lenses: bool = True
    max_action_steps: int = 4
    max_action_targets: int = 32
    evidence_min_width: int = 8
    evidence_beam_width: int = 3
    evidence_max_depth: int = 4
    max_evidence_candidates: int = 96
    relation_threshold: Decimal = Decimal("0.70")
    max_relationships: int = 4
    max_project_candidates: int = 128
    localization_beam_width: int = 3
    run_deadline_seconds: float = 300.0
    search_policy: str = "legacy-v1"
    refute_threshold: Decimal = Decimal("0.80")

    def __post_init__(self):
        limits = ((self.width, 2, 4096), (self.min_width, 1, self.width),
                  (self.max_depth, 0, 16), (self.context_lines, 0, 10000),
                  (self.max_calls, 1, 100000), (self.max_questions, 1, 1000000),
                  (self.max_windows, 1, 200000), (self.max_context_packets, 0, 64),
                  (self.max_localizations, 0, 32),
                  (self.max_handoff_candidates, 1, 256),
                  (self.max_action_steps, 0, 32),
                  (self.max_action_targets, 0, 512),
                  (self.evidence_min_width, 1, 4096),
                  (self.evidence_beam_width, 1, 32),
                  (self.evidence_max_depth, 0, 16),
                  (self.max_evidence_candidates, 1, 4096),
                  (self.max_relationships, 0, 64),
                  (self.max_project_candidates, 1, 4096),
                  (self.localization_beam_width, 1, 32))
        if any(type(v) is not int or not lo <= v <= hi for v, lo, hi in limits):
            raise ValueError("invalid_search_configuration")
        for name in ("drill_threshold", "report_threshold", "context_threshold",
                     "choice_confidence", "relation_threshold", "refute_threshold"):
            value = getattr(self, name)
            if type(value) not in (int, float, Decimal):
                raise ValueError("invalid_threshold")
            value = Decimal(str(value))
            if not value.is_finite() or not 0 <= value <= 1:
                raise ValueError("invalid_threshold")
            object.__setattr__(self, name, value)
        if self.drill_threshold > self.report_threshold:
            raise ValueError("drill_threshold_exceeds_report_threshold")
        if self.search_policy not in ("legacy-v1", "scoped-v2-beta"):
            raise ValueError("invalid_search_policy")
        if type(self.whole_file) is not bool:
            raise ValueError("invalid_whole_file_flag")
        if type(self.bug_lenses) is not bool:
            raise ValueError("invalid_bug_lenses_flag")
        if (type(self.run_deadline_seconds) not in (int, float)
                or not math.isfinite(self.run_deadline_seconds)
                or not 1 <= self.run_deadline_seconds <= 86400):
            raise ValueError("invalid_run_deadline")

    def as_dict(self):
        return {k: str(v) if isinstance(v, Decimal) else v
                for k, v in asdict(self).items()}


def windows(span: Span, width: int, offsets: bool = True) -> list[Span]:
    """Union of regular and shifted grids, plus a full-width EOF anchor."""
    if type(width) is not int or width < 1:
        raise ValueError("invalid_window_width")
    if span.size <= width:
        return [span]
    last = span.end - width + 1
    starts = set(range(span.start, last + 1, width))
    if offsets and width > 1:
        starts.update(range(span.start + width // 2, last + 1, width))
    starts.add(last)
    return [Span(start, start + width - 1) for start in sorted(starts)]


def _window_layout(span, width, offsets):
    """Count the two interleaved grids and their possibly distinct EOF anchor."""
    if type(width) is not int or width < 1:
        raise ValueError("invalid_window_width")
    if span.size <= width:
        return 1, 1, 0
    quotient, remainder = divmod(span.size - width, width)
    shift = width // 2 if offsets and width > 1 else 0
    regular = quotient + 1
    shifted = quotient + int(remainder >= shift) if shift else 0
    grid_count = regular + shifted
    extra_anchor = remainder != 0 and (not shift or remainder != shift)
    return grid_count + int(extra_anchor), grid_count, shift


def _window_count(span: Span, width: int, offsets: bool = True) -> int:
    """Exact len(windows(...)) without constructing any windows."""
    return _window_layout(span, width, offsets)[0]


def _window_at(span: Span, width: int, index: int,
               offsets: bool = True) -> Span:
    """One zero-based window in the exact ordering of windows(...)."""
    count, grid_count, shift = _window_layout(span, width, offsets)
    if type(index) is not int:
        raise ValueError("invalid_window_index")
    if not 0 <= index < count:
        raise IndexError("window_index_out_of_range")
    if span.size <= width:
        return span
    if index == grid_count:
        start = span.end - width + 1
    elif shift:
        # Odd widths have alternating gaps of floor(width/2) and ceil(width/2).
        start = span.start + (index // 2) * width + (shift if index % 2 else 0)
    else:
        start = span.start + index * width
    return Span(start, start + width - 1)



def multiscale(span, minimum, max_depth, limit):
    """Breadth-first speculative descendants, independent of model answers.

    Return ([(span, depth), ...], capped). All children strictly shrink.
    """
    if all(type(value) is int for value in (minimum, max_depth, limit)):
        if limit <= 0:
            return [], True
        if span.size <= minimum or max_depth <= 0:
            return [(span, 0)], False
        if limit == 1 and minimum >= 1:
            return [(span, 0)], True
        if (1 <= minimum and span.size <= 128 and max_depth <= 16
                and limit >= span.size * (max_depth + 1) + 1):
            # The common root widths repeat throughout a file. Only relative
            # integer geometry is shared; callers receive a fresh ordered list.
            # Tight budgets keep the incremental planner and never precompute
            # descendants beyond their declared planning cap.
            shape = _multiscale_shape(span.size, minimum, max_depth)
            return [(Span(span.start + lo, span.start + hi), depth)
                    for lo, hi, depth in shape[:limit]], len(shape) > limit
    return _multiscale_uncached(span, minimum, max_depth, limit)


@lru_cache(maxsize=64)
def _multiscale_shape(size, minimum, max_depth):
    # Each depth uses one width, so it has at most `size` unique starts.
    tree, _ = _multiscale_uncached(Span(1, size), minimum, max_depth,
                                  size * (max_depth + 1) + 1)
    return tuple((target.start - 1, target.end - 1, depth)
                 for target, depth in tree)


def _multiscale_uncached(span, minimum, max_depth, limit):
    from collections import deque
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


def contiguous_candidates(span):
    """Complete intervals, never independent start/end choices. Max 210."""
    if span.size > 20:
        raise ValueError("localization_region_too_large")
    return [Span(lo, hi) for lo in range(span.start, span.end + 1)
            for hi in range(lo, span.end + 1)]


def _coverage(spans, count):
    merged = []
    # Union depends on starts only; equal-start ends merge through the same max.
    for span in sorted(spans, key=attrgetter("start")):
        if merged and span.start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], span.end)
        else:
            merged.append([span.start, span.end])
    cursor, missing = 1, []
    for lo, hi in merged:
        if cursor < lo:
            missing.append([cursor, lo - 1])
        cursor = hi + 1
    if cursor <= count:
        missing.append([cursor, count])
    return {"assessed_lines": sum(hi - lo + 1 for lo, hi in merged),
            "unassessed_ranges": missing, "all_lines_assessed": not missing}



def scan(source, gateway, config=Config(), spec=None, project=None,
         context_scope=None, *, deadline_at=None):
    """Public API. The gateway must implement validated evaluate().

    *project* is an optional ProjectIndex used only as bounded evidence supply;
    Python never decides semantic relevance from repository structure alone.

    *context_scope* is the caller's explicit declaration: "project",
    "declared_standalone", or "owner_isolated". It is required and never
    inferred — an omitted scope is a caller error, not a silent default.

    The caller owns *gateway*: this function does not close it. Use
    ``with HostedJev(receipt_dir) as gateway:`` around one or more scans to
    finish reusable-worker cleanup when the caller's work ends.
    """
    if context_scope is None:
        raise ValueError("context_scope is required: project | "
                         "declared_standalone | owner_isolated")
    if context_scope not in ("project", "declared_standalone",
                             "owner_isolated"):
        raise ValueError("invalid context_scope")
    if (context_scope == "project") != (project is not None):
        raise ValueError("context_scope and project index must agree")
    from .engine import Search
    return Search(source, gateway, config, spec, project=project,
                  context_scope=context_scope, deadline_at=deadline_at).run()


def format_findings(result: dict) -> str:
    return "".join(
        f"Bug suspected between lines {f['start_line']} and {f['end_line']}.\n"
        for f in result["findings"]
    )
