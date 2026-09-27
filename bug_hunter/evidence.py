"""Bounded evidence candidates and multi-source state construction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .core import Source, Span
from .jev import encode_request, JevError
from .questions import state_for


@dataclass(frozen=True)
class EvidenceRef:
    """A real, immutable source passage offered to Jev as evidence."""
    source: Source
    label: str
    span: Span
    kind: str = "source"

    @property
    def id(self):
        raw = f"{self.kind}\0{self.label}\0{self.source.sha256}\0{self.span.start}\0{self.span.end}"
        return "E" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def display(self):
        return f"{self.label}:{self.span.start}-{self.span.end}"

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "file": self.label,
                "source_sha256": self.source.sha256,
                "start_line": self.span.start, "end_line": self.span.end}


def excerpt(ref: EvidenceRef):
    return {"id": ref.id, "file": ref.label, "source_sha256": ref.source.sha256,
            "start_line": ref.span.start, "end_line": ref.span.end,
            "lines": [{"line": i, "text": ref.source.lines[i - 1]}
                      for i in range(ref.span.start, ref.span.end + 1)]}


def pair_state(primary: Source, target: Span, halo: int, spec: str | None,
               evidence: EvidenceRef, *, context_scope=None):
    """Put target and evidence into one bounded state without fabricating links."""
    # Content identity is not file identity: two different files can have the
    # same SHA-256 and line coordinates. Same-file candidates are constructed
    # with the exact primary Source object; cross-file candidates remain in
    # related_sources even when their bytes happen to be identical.
    if evidence.source is primary:
        state = state_for(primary, target, halo, spec, extras=(evidence.span,),
                          context_scope=context_scope)
    else:
        state = state_for(primary, target, halo, spec, context_scope=context_scope)
        state["related_sources"] = [excerpt(evidence)]
    # Even same-file evidence has a generated ID that questions can reference.
    state["evidence_coordinates"] = [{"id": evidence.id, "file": evidence.label,
        "source_sha256": evidence.source.sha256, "start_line": evidence.span.start,
        "end_line": evidence.span.end}]
    return state


def fitting_pair_state(primary: Source, target: Span, halo: int, spec: str | None,
                       evidence: EvidenceRef, probe: dict, *, context_scope=None):
    """Reduce only optional target halo; never truncate target/evidence text."""
    while True:
        state = pair_state(primary, target, halo, spec, evidence,
                           context_scope=context_scope)
        try:
            encode_request(state, {"probe": probe})
            return state
        except JevError as exc:
            if exc.code != "request_too_large":
                raise
            if halo == 0:
                return None
            halo //= 2


def evidence_label(ref: EvidenceRef):
    return (f"{ref.id} (inclusive evidence lines {ref.span.start} through "
            f"{ref.span.end}; file identity supplied in state)")
