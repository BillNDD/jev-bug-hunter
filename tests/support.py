"""Deterministic synthetic provider. Tests routing, NOT Jev bug detection.

No network, no learned model, no runtime fallback. Faults are preassigned spans.
"""
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import re
import subprocess
from unittest.mock import patch

from bug_hunter.core import Source, Span
from bug_hunter import jev

KEY = "offline-synthetic-key-not-a-real-credential"


@contextmanager
def legacy_exchange():
    """Pin legacy subprocess fakes at the adapter's private transport seam.

    The tests inside this context must also fake subprocess.run. A default
    transport change must never bypass those fakes and start a real worker.
    Dedicated worker tests exercise its actual protocol separately.
    """
    from bug_hunter.transport import KillableWorkerTransport
    with patch.object(jev.HostedJev, "_exchange", jev.HostedJev._exchange_once), \
            patch.object(KillableWorkerTransport, "_spawn",
                         side_effect=AssertionError("unexpected worker in offline fixture")) as spawn:
        try:
            yield
        finally:
            spawn.assert_not_called()


def source(n, text=None):
    lines = tuple(text if text is not None else f"fixture line {i}"
                  for i in range(1, n + 1))
    return Source("fixture.txt", hashlib.sha256("\n".join(lines).encode()).hexdigest(),
                  lines, "utf-8")


def ranges(result):
    return {(f["start_line"], f["end_line"]) for f in result["findings"]}


def spans_in(text):
    return [Span(int(a), int(b)) for a, b in re.findall(r"L(\d+)-(\d+)", text)]


class FixtureProvider:
    def __init__(self, bugs=(), *, override=None, need_packet=None):
        self.bugs = [Span(*s) if isinstance(s, tuple) else s for s in bugs]
        self.override = override
        self.need_packet = need_packet
        self.requests = []

    def contains_bug(self, span):
        return any(span.start <= b.start and b.end <= span.end for b in self.bugs)

    def answer(self, qid, q, state):
        parsed = spans_in(q["instructions"])
        target = parsed[0]
        answer = None
        if self.override:
            answer = self.override(qid, q, state, target)
        if answer is not None:
            return answer
        if q["type"] == "choice":
            if qid == "action":
                chosen = "finish"
                return {"type": "choice", "choice": chosen, "confidence": 1,
                        "probabilities": {k: int(k == chosen) for k in q["criteria"]}}
            options = [s for s in spans_in(" ".join(q["criteria"]))
                       if self.contains_bug(s)]
            if qid == "context_where":
                options = [s for s in spans_in(" ".join(q["criteria"]))
                           if self.need_packet is not None and
                           s.start <= self.need_packet <= s.end]
            elif qid == "where":
                # Prefer a known exact fixture span. When all have been selected,
                # abstain rather than invent a second independent defect.
                options = [s for s in options if s in self.bugs]
            chosen = min(options, key=lambda s: (s.size, s.start)).id if options else "none"
            return {"type": "choice", "choice": chosen, "confidence": 1,
                    "probabilities": {k: int(k == chosen) for k in q["criteria"]}}
        if qid.startswith(("read_with_", "requirement_", "action_requirement_",
                           "evidence_", "relation_", "verify_evidence_")):
            return {"type": "noul", "noul": 0.05}
        if qid == "continue":
            return {"type": "noul", "noul": 0.95}
        value = 0.95 if self.contains_bug(target) else 0.05
        if qid == "need_context":
            visible = {line["line"] for e in state["excerpts"] for line in e["lines"]}
            value = 0.95 if (self.need_packet is not None and
                    self.contains_bug(target) and self.need_packet not in visible) else 0.05
        elif qid.startswith("relevance_"):
            packet = parsed[0]
            value = 0.95 if (self.need_packet is not None and
                            packet.start <= self.need_packet <= packet.end) else 0.05
        elif qid == "covered":
            children = parsed[1:]
            relevant = [b for b in self.bugs if target.start <= b.start and b.end <= target.end]
            value = 0.95 if relevant and all(any(
                c.start <= b.start and b.end <= c.end for c in children)
                for b in relevant) else 0.05
        return {"type": "noul", "noul": value}

    def raw(self, state, questions):
        self.requests.append({"state": state, "questions": questions})
        return json.dumps({"model": jev.MODEL,
            "answers": {qid: self.answer(qid, q, state) for qid, q in questions.items()},
            "usage": {"input_tokens": 100, "output_tokens": len(questions)}}).encode()

    def evaluate(self, state, questions, *, metadata):
        # Even pure engine tests use the real response validator.
        return jev.validate_answers(self.raw(state, questions), questions)[0]

    def process(self, *args, **kwargs):
        body = json.loads(kwargs["input"])
        assert body["model"] == jev.MODEL
        return subprocess.CompletedProcess(args[0], 0,
            self.raw(body["state"], body["questions"]), b"")


def scan(source, gateway, *args, context_scope="declared_standalone", **kwargs):
    """Test helper. The public scan() requires an explicit scope; tests
    declare theirs here, in one visible place, never via a silent default."""
    from bug_hunter.core import scan as _scan
    return _scan(source, gateway, *args, context_scope=context_scope, **kwargs)
