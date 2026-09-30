"""Versioned, generic decision questions. No bug taxonomy or generated text."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from .core import Source, Span, physical_lines
from .jev import (encode_request, JevError, MAX_QUESTIONS, MAX_STATE_BYTES,
                  MAX_REQUEST_BYTES, MODEL, _snapshot, _json, validate_questions,
                  _encode_validated_request)


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
    pages = _rank_pages(pack, region, targets, state)
    for index, page in enumerate(pages):
        result[f"rank_{index}"] = choice(pack, "rank", region, page)
    return result


def _rank_pages(pack, region, targets, state):
    """Size growing menus from exact JSON fragments, validating each final page.

    A menu adds entries before the two fixed sentinels. Fragment sizes include
    their commas, so UTF-8, escaping and duplicate span IDs have the same byte
    cost as the former full-state serialization at every candidate.
    """
    pages, page, entries, added, prepared = [], [], {}, 0, False
    for target in targets:
        if state is not None and not prepared:
            try:
                state = _snapshot(state)
                if type(state) is not dict:
                    raise JevError("invalid_request")
                template = choice(pack, "rank", region, [])
                validate_questions({"rank": template})
                state_fits = len(_json(state)) <= MAX_STATE_BYTES
                base = len(_json({"model": MODEL, "state": state,
                                  "questions": {"rank": template}}))
                prepared = True
            except JevError:
                raise
            except (ValueError, TypeError, UnicodeError):
                raise JevError("invalid_request") from None
        fits = len(page) < 200
        if state is not None:
            # Keep field/schema validation even though these are generated
            # coordinates. The complete page is authoritatively encoded below.
            item = choice(pack, "rank", region, [target])
            validate_questions({"rank": item})
            try:
                entry = len(_json({target.id: target_text(target)})) - 1
            except (ValueError, TypeError, UnicodeError):
                raise JevError("invalid_request") from None
            predicted = added + entry - entries.get(target.id, 0)
            fits = fits and state_fits and base + predicted <= MAX_REQUEST_BYTES
        if page and not fits:
            pages.append(page)
            page, entries, added = [], {}, 0
        page.append(target)
        if state is not None:
            added += entry - entries.get(target.id, 0)
            entries[target.id] = entry
    if page:
        pages.append(page)
    if state is not None:
        for page in pages:
            try:
                encode_request(state, {"rank": choice(pack, "rank", region, page)})
            except JevError as exc:
                # The existing contract retains an oversized singleton as a
                # named question failure for the execution layer to record.
                if exc.code != "request_too_large" or len(page) != 1:
                    raise
    return pages


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


def _source_ranges(source, region, halo, extras=()):
    ranges = [Span(max(1, region.start - halo),
                   min(len(source.lines), region.end + halo)), *extras]
    merged = []
    for span in sorted(ranges):
        if merged and span.start <= merged[-1].end + 1:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    return merged


def _source_excerpts(source, region, halo, extras=()):
    return [{"start_line": span.start, "end_line": span.end,
             "lines": [{"line": i, "text": source.lines[i - 1]}
                       for i in range(span.start, span.end + 1)]}
            for span in _source_ranges(source, region, halo, extras)]


def _state_with_excerpts(source, excerpts, spec, context_scope):
    state = {"source_sha256": source.sha256, "file": source.name,
            "total_lines": len(source.lines),
            "excerpts": excerpts,
            "specification": {
                "mode": "provided" if spec else "inferred",
                # Numbered lines so questions can reference SPEC:R{a}-R{b}
                # without repeating untrusted text in instructions.
                "lines": [{"line": i, "text": line}
                          for i, line in enumerate(physical_lines(spec or ""), 1)]}}
    if context_scope is not None:
        state["context_scope"] = context_scope
    return state


def state_for(source, region, halo, spec, extras=(), *, context_scope=None):
    # Keep construction order for unsupported library inputs: source metadata,
    # then line access, then specification parsing.
    state = {"source_sha256": source.sha256, "file": source.name,
             "total_lines": len(source.lines),
             "excerpts": _source_excerpts(source, region, halo, extras),
             "specification": {"mode": "provided" if spec else "inferred",
                               "lines": [{"line": i, "text": line}
                                         for i, line in enumerate(physical_lines(spec or ""), 1)]}}
    if context_scope is not None:
        state["context_scope"] = context_scope
    return state


def _mandatory_view_too_large(source, region, halo, spec, probe, extras, context_scope):
    """Prove all halo choices exceed the state cap without rendering doomed rows.

    Each physical line requires at least 20 JSON bytes plus its text, before
    commas. Charging 21 per line is still a lower bound: each nonempty excerpt
    has a header larger than its missing first comma. Excerpt headers are
    otherwise ignored, so halo-induced merging cannot invalidate the bound.

    Only immutable primitive inputs use this path. All initially visible text
    and the original probe must validate before rejection; unsupported or
    invalid inputs fall through to the ordinary renderer and its error order.
    Nothing is cached across calls, and a potentially fitting view is unchanged.
    """
    if (type(source) is not Source or type(source.lines) is not tuple
            or type(source.name) is not str or type(source.sha256) is not str
            or type(region) is not Span or type(halo) is not int or halo < 0
            or type(extras) is not tuple
            or any(type(span) is not Span for span in extras)
            or (spec is not None and type(spec) is not str)
            or (context_scope is not None and type(context_scope) is not str)):
        return False
    lines = source.lines
    if region.end > len(lines) or any(span.end > len(lines) for span in extras):
        return False
    try:
        minimum = 0
        mandatory = _source_ranges(source, region, 0, extras) if extras else (region,)
        for span in mandatory:
            for i in range(span.start - 1, span.end):
                line = lines[i]
                if type(line) is not str:
                    return False
                minimum += 21 + len(line)
                if minimum > MAX_STATE_BYTES:
                    break
            if minimum > MAX_STATE_BYTES:
                break
        # This only bypasses an optional optimization for ordinary small views;
        # the normal path still performs the complete request validation.
        if minimum < MAX_STATE_BYTES // 2:
            return False
        fixed = _state_with_excerpts(source, [], spec, context_scope)
        if minimum + len(_json(fixed)) <= MAX_STATE_BYTES:
            return False
        for span in _source_ranges(source, region, halo, extras):
            for i in range(span.start - 1, span.end):
                line = lines[i]
                if type(line) is not str:
                    return False
                if not line.isascii():
                    line.encode("utf-8")
        try:
            encode_request(fixed, {"probe": probe})
        except JevError as exc:
            if exc.code != "request_too_large":
                return False
        return True
    except (JevError, ValueError, TypeError, UnicodeError, IndexError, AttributeError):
        return False


def fitting_state(source, region, halo, spec, probe, extras=(), *, context_scope=None):
    """Reduce optional halo only; all target and selected context stays intact."""
    if _mandatory_view_too_large(source, region, halo, spec, probe, extras, context_scope):
        return None
    state = state_for(source, region, halo, spec, extras, context_scope=context_scope)
    while True:
        try:
            encode_request(state, {"probe": probe})
            return state
        except JevError as exc:
            if exc.code != "request_too_large":
                raise
            if halo == 0:
                return None
            halo //= 2
            # The specification, scope and source identity are invariant across
            # halo probes; keep this private state and replace only its excerpts.
            state["excerpts"] = _source_excerpts(source, region, halo, extras)


def prepared_question_batches(state, questions):
    """Own and validate values once, then assemble exact UTF-8 batch fragments.

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
        state_wire = _json(state)
        state_fits = len(state_wire) <= MAX_STATE_BYTES
        base = len(_encode_validated_request(state_wire, b"{}")) if state_fits else 0
        batch, fragments, size = {}, [], base
        for qid, question in questions.items():
            validate_questions({qid: question})
            fragment = _json({qid: question})[1:-1]
            entry_size = len(fragment)
            predicted = size + entry_size + bool(batch)
            if batch and (len(batch) == MAX_QUESTIONS or predicted > MAX_REQUEST_BYTES):
                wire = _encode_validated_request(state_wire, b"{" + b",".join(fragments) + b"}")
                if len(wire) != size:
                    raise JevError("invalid_request")
                yield batch, None, wire
                batch, fragments, size = {}, [], base
            if not state_fits or base + entry_size > MAX_REQUEST_BYTES:
                yield {}, qid, None
                continue
            size += entry_size + bool(batch)
            batch[qid] = question
            fragments.append(fragment)
        if batch:
            wire = _encode_validated_request(state_wire, b"{" + b",".join(fragments) + b"}")
            if len(wire) != size:
                raise JevError("invalid_request")
            yield batch, None, wire
    except JevError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise JevError("invalid_request") from None


def pack_questions(state, questions):
    """Public pair-form iterator; preparation also exposes each validated wire."""
    for batch, failure, _wire in prepared_question_batches(state, questions):
        yield batch, failure
