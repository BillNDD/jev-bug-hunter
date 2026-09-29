"""Versioned, generic decision questions. No bug taxonomy or generated text."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from .core import Span, physical_lines
from .jev import (encode_request, JevError, MAX_QUESTIONS, MAX_STATE_BYTES,
                  MAX_REQUEST_BYTES, MODEL, _snapshot, _json, validate_questions)


def load_pack():
    raw = files("bug_hunter").joinpath("question_pack.json").read_bytes()
    pack = json.loads(raw)
    expected = {"version", "policy", "screen", "verify", "need_context", "rank",
                "localize", "relevance", "evidence_relevance", "relation",
                "verify_with_evidence", "continue_investigation", "next_action",
                "covered", "yes", "no", "none", "unlocalized", "context_pick",
                "read_with", "requirement", "lens_functional", "lens_contract",
                "lens_boundary", "lens_state", "lens_integration"}
    if (type(pack) is not dict or set(pack) != expected
            or any(type(v) is not str or not v.strip() for v in pack.values())):
        raise ValueError("invalid_question_pack")
    return pack, hashlib.sha256(raw).hexdigest()


def target_text(span):
    # Question IDs are not sent to the model. The bounds MUST be in instructions.
    return f"{span.id} (inclusive original lines {span.start} through {span.end})"


def noul(pack, kind, span, **values):
    # Callers supply only generated IDs and coordinates. Filtering controls is
    # formatting hygiene, NOT an injection boundary for arbitrary source text.
    clean = {k: "".join(ch for ch in str(v) if ch == " " or ch.isprintable())
             for k, v in values.items()}
    body = pack[kind].format(target=target_text(span), **clean)
    return {"type": "noul", "instructions": pack["policy"] + " " + body,
            "criteria": {"true": pack["yes"], "false": pack["no"]}}


def choice(pack, kind, span, candidates):
    criteria = {s.id: target_text(s) for s in candidates}
    criteria.update(none=pack["none"], unlocalized=pack["unlocalized"])
    return {"type": "choice", "instructions": pack["policy"] + " " +
            pack[kind].format(target=target_text(span)), "criteria": criteria}


LENS_KINDS = ("lens_functional", "lens_contract", "lens_boundary",
              "lens_state", "lens_integration")


def screening(pack, region, targets, *, bug_lenses=True, state=None):
    result = {"screen_" + span.id: noul(pack, "screen", span) for span in targets}
    if bug_lenses:
        for span in targets:
            for kind in LENS_KINDS:
                result[f"{kind}_{span.id}"] = noul(pack, kind, span)
    result["need_context"] = noul(pack, "need_context", region)
    # Rank pages separately. Never compare probabilities across different menus.
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
    for index, page in enumerate(pages):
        result[f"rank_{index}"] = choice(pack, "rank", region, page)
    return result


def action_choice(pack, span, options):
    """Choice over a caller-supplied closed action vocabulary."""
    if type(options) is not dict or len(options) < 2:
        raise ValueError("action_choice_needs_options")
    return {"type": "choice",
            "instructions": pack["policy"] + " " +
                pack["next_action"].format(target=target_text(span)),
            "criteria": dict(options)}


def evidence_noul(pack, kind, target, label):
    key = "evidence_relevance" if kind == "relevance" else kind
    return noul(pack, key, target, evidence=label)


def state_for(source, region, halo, spec, extras=(), *, context_scope=None):
    ranges = [Span(max(1, region.start - halo),
                   min(len(source.lines), region.end + halo)), *extras]
    merged = []
    for span in sorted(ranges):
        if merged and span.start <= merged[-1].end + 1:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    excerpts = [{"start_line": span.start, "end_line": span.end,
                 "lines": [{"line": i, "text": source.lines[i - 1]}
                           for i in range(span.start, span.end + 1)]}
                for span in merged]
    state = {"source_sha256": source.sha256, "file": source.name,
            "total_lines": len(source.lines), "excerpts": excerpts,
            "specification": {
                "mode": "provided" if spec else "inferred",
                # Numbered lines so questions can reference SPEC:R{a}-R{b}
                # without repeating untrusted text in instructions.
                "lines": [{"line": i, "text": line}
                          for i, line in enumerate(physical_lines(spec or ""), 1)]}}
    if context_scope is not None:
        state["context_scope"] = context_scope
    return state


def fitting_state(source, region, halo, spec, probe, extras=(), *, context_scope=None):
    """Reduce optional halo only; all target and selected context stays intact."""
    while True:
        state = state_for(source, region, halo, spec, extras, context_scope=context_scope)
        try:
            encode_request(state, {"probe": probe})
            return state
        except JevError as exc:
            if exc.code != "request_too_large":
                raise
            if halo == 0:
                return None
            halo //= 2


def pack_questions(state, questions):
    """Exact incremental UTF-8 sizing; authoritative validation at each flush.

Preserves option, question, state and batch order. A singleton that cannot fit
is returned as a named failure. The caller still validates each actual response
atomically. No evidence or question is dropped to make a request smaller.
"""
    if not questions:
        return
    try:
        state, questions = _snapshot(state), _snapshot(questions)
        if type(state) is not dict or type(questions) is not dict:
            raise JevError("invalid_request")
        state_fits = len(_json(state)) <= MAX_STATE_BYTES
        base = len(_json({"model": MODEL, "state": state, "questions": {}}))
        batch, size = {}, base
        for qid, question in questions.items():
            validate_questions({qid: question})
            entry_size = len(_json({qid: question})) - 2
            predicted = size + entry_size + bool(batch)
            if batch and (len(batch) == MAX_QUESTIONS or predicted > MAX_REQUEST_BYTES):
                if len(encode_request(state, batch)) != size:
                    raise JevError("invalid_request")
                yield batch, None
                batch, size = {}, base
            if not state_fits or base + entry_size > MAX_REQUEST_BYTES:
                yield {}, qid
                continue
            size += entry_size + bool(batch)
            batch[qid] = question
        if batch:
            if len(encode_request(state, batch)) != size:
                raise JevError("invalid_request")
            yield batch, None
    except JevError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise JevError("invalid_request") from None
