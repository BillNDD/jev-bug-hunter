"""Compact debugger handoff. Coordinates are facts; relevance is judged by Jev.

No source snippets, raw probabilities, explanations, severity or fixes enter the
compact output. The full report retains scores, negative answers and provenance.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import hashlib
import json

from .core import Span, windows
from .evidence import evidence_label
from .questions import noul, target_text

DISPLAY_REFS = 2


def assessment(search, target):
    """Compare only the identical target across source views; never its parent.

    'Conflicting' denotes crossing the configured reporting threshold, not a
    statistical contradiction. Failed evaluations are unknown, not zero.
    """
    bounds = [target.start, target.end]
    values = [Decimal(row["judgments"]["screen"]) for row in search.observations
              if [row["start_line"], row["end_line"]] == bounds
              and row.get("judgments", {}).get("screen") is not None]
    direct = [row for row in search.rechecks if row["target"] == bounds]
    values += [Decimal(row["score"]) for row in direct
               if row["score"] is not None]
    values += [Decimal(row["score"]) for row in search.existence_checks
               if row["target"] == bounds and row["score"] is not None]
    cut = search.cfg.report_threshold
    if any(v >= cut for v in values) and any(v < cut for v in values):
        return "conflicting"
    if any(row["score"] is not None and Decimal(row["score"]) >= cut
           for row in direct):
        return "recheck-supported"
    return "not-rechecked"


def passages(lines, region, width=8):
    """Nonblank blocks, with offset 8-line windows for long blocks; no parsing.

    Original blank lines and coordinates remain untouched in the observation.
    An empty/whitespace-only passage supplies no reference candidate.
    """
    start = None
    for line in range(region.start, region.end + 2):
        present = line <= region.end and bool(lines[line - 1].strip())
        if present and start is None:
            start = line
        elif not present and start is not None:
            yield from windows(Span(start, line - 1), width)
            start = None


def source_candidates(search, target):
    """Only passages actually present in a view assessing this exact target.

    Prefer the latest available view for an identical passage. A candidate is
    judged within that view, not in an invented union of unrelated contexts.
    """
    bounds = [target.start, target.end]
    ids = {row["view_id"] for row in search.observations
           if [row["start_line"], row["end_line"]] == bounds}
    ids.update(row["view_id"] for row in search.rechecks
               if row["target"] == bounds)
    pool = {}
    for group in search.groups:
        if group["id"] not in ids:
            continue
        for excerpt in group["state"]["excerpts"]:
            a, b = excerpt["start_line"], excerpt["end_line"]
            # Do not echo the suspect as its own read-with reference.
            pieces = [(a, min(b, target.start - 1)),
                      (max(a, target.end + 1), b)]
            for lo, hi in pieces:
                if lo <= hi:
                    for candidate in passages(search.source.lines, Span(lo, hi)):
                        pool[candidate] = group
    return pool


def capped(items, limit):
    """Deterministic spread when capped, with omissions reported by the caller."""
    if len(items) <= limit:
        return items
    if limit == 1:
        return items[:1]
    return [items[i * (len(items) - 1) // (limit - 1)] for i in range(limit)]


def state_hash(state):
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def references(search, target, kind, pool):
    """Batched independent relevance Nouls. No generative explanation/coordinates.

    Return selected references, output-only truncation count, and assessment
    status. All evaluated candidates, including negatives, remain in the trace.
    """
    phase = "handoff-" + kind
    candidates = capped(sorted(pool), search.cfg.max_handoff_candidates)
    failed = len(candidates) < len(pool)
    if failed:
        search.issue("reference_pool_limited", target, phase)
    search.events.append({"phase": phase, "target": target.id,
                          "available_candidates": len(pool),
                          "examined_candidates": [s.id for s in candidates],
                          "pool_limited": failed})
    batches = defaultdict(list)
    for candidate in candidates:
        group = pool[candidate]
        batches[group["id"]].append(candidate)
    groups = {g["id"]: g for g in search.groups}
    qualified, answered = [], 0
    for view_id, candidates_in_view in batches.items():
        group = groups[view_id]
        questions = {}
        for candidate in candidates_in_view:
            if kind == "source":
                body = "source passage " + target_text(candidate)
                qkind, qid = "read_with", "read_with_" + candidate.id
            else:
                # Keep the complete original spec in structured state. The
                # question references the exact candidate by SPEC coordinates;
                # untrusted text never enters instructions.
                body = (f"SPEC:R{candidate.start}-R{candidate.end} (inclusive "
                        f"specification lines {candidate.start} through "
                        f"{candidate.end}) supplied in state")
                qkind = "requirement"
                qid = f"requirement_R{candidate.start}-{candidate.end}"
            question = noul(search.pack, qkind, target, passage=body)
            if len(question["instructions"].encode("utf-8")) > 12000:
                search.issue("handoff_question_too_large", target, phase)
                failed = True
            else:
                questions[qid] = question
        answers = search.ask(group["state"], questions, target, phase)
        for candidate in candidates_in_view:
            qid = ("read_with_" + candidate.id if kind == "source" else
                   f"requirement_R{candidate.start}-{candidate.end}")
            answer = answers.get(qid)
            value = answer["noul"] if answer is not None else None
            search.handoff_evidence.append({
                "target": [target.start, target.end], "kind": kind,
                "candidate": [candidate.start, candidate.end],
                "view_id": view_id, "state_sha256": state_hash(group["state"]),
                "question_id": qid, "score": str(value) if value is not None else None})
            if value is None:
                failed = True
            else:
                answered += 1
                if value >= search.cfg.context_threshold:
                    qualified.append((value, candidate))
    qualified.sort(key=lambda item: (-item[0], item[1].size, item[1].start))
    all_refs = [[s.start, s.end] for _, s in qualified]
    status = ("selected" if all_refs else "unknown" if failed else "not-identified")
    return {"status": status, "ranges": all_refs[:DISPLAY_REFS],
            "refs": [{"file": None, "range": pair} for pair in all_refs[:DISPLAY_REFS]],
            "all_refs": [{"file": None, "range": pair} for pair in all_refs],
            "additional_count": max(0, len(all_refs) - DISPLAY_REFS),
            "all_ranges": all_refs, "candidates_assessed": answered}, failed


def relationship_read_with(search, target):
    """Exact-target relationships or fresh exact-target relevance judgments."""
    rows = []
    for row in search.relationships:
        tested = row["target"]
        if (row["relation_score"] is None or not tested
                or not (tested[0] <= target.start
                        and target.end <= tested[1])):
            continue
        relation = Decimal(row["relation_score"])
        if relation < search.cfg.relation_threshold:
            continue
        evidence = row["evidence"]
        if (evidence["kind"] == "source" and target.start <= evidence["start_line"]
                and evidence["end_line"] <= target.end):
            continue
        verified = (Decimal(row["verify_score"]) >= search.cfg.report_threshold
                    if row["verify_score"] is not None else False)
        rows.append((not verified, -relation, evidence["end_line"] - evidence["start_line"],
                     evidence["file"], evidence, tested == [target.start, target.end]))
    rows.sort(key=lambda item: item[:4])
    refs, seen = [], set()
    examined = 0
    for _, _, _, _, evidence, exact in rows:
        file_label = None if evidence["kind"] == "source" else evidence["file"]
        key = (file_label, evidence["start_line"], evidence["end_line"])
        if key in seen:
            continue
        seen.add(key)
        if not exact:
            if examined >= search.cfg.max_handoff_candidates:
                search.issue("reference_pool_limited", target, "handoff-relationship")
                continue
            examined += 1
            ref = search.evidence_refs.get(evidence["id"])
            if ref is None:
                search.issue("reference_evidence_unavailable", target, "handoff-relationship")
                continue
            question = noul(search.pack, "read_with", target, passage=evidence_label(ref))
            state = search.fitting_pair(target, ref, question)
            if state is None:
                search.issue("handoff_question_too_large", target, "handoff-relationship")
                continue
            qid = "read_with_evidence_" + ref.id
            answer = search.ask(state, {qid: question}, target, "handoff-relationship").get(qid)
            value = answer["noul"] if answer is not None else None
            search.handoff_evidence.append({"target": [target.start, target.end],
                "kind": ref.kind, "candidate": [ref.span.start, ref.span.end],
                "evidence": ref.as_dict(), "state_sha256": state_hash(state),
                "question_id": qid, "score": str(value) if value is not None else None})
            if value is None or value < search.cfg.context_threshold:
                continue
        refs.append({"file": file_label,
                     "range": [evidence["start_line"], evidence["end_line"]],
                     "source_sha256": evidence["source_sha256"]})
    return refs


def merge_read_with(entry, relation_refs):
    if not relation_refs:
        return entry
    existing = entry.get("all_refs", [{"file": None, "range": r}
                                      for r in entry.get("all_ranges", entry["ranges"])])
    combined, seen = [], set()
    for ref in relation_refs + existing:
        key = (ref.get("file"), tuple(ref["range"]))
        if key not in seen:
            seen.add(key)
            combined.append(ref)
    result = dict(entry)
    result["status"] = "selected"
    result["refs"] = combined[:DISPLAY_REFS]
    result["ranges"] = [r["range"] for r in result["refs"] if r.get("file") is None]
    result["additional_count"] = max(0, len(combined) - DISPLAY_REFS)
    result["all_refs"] = combined
    result["all_ranges"] = [r["range"] for r in combined if r.get("file") is None]
    return result


# Finite output vocabulary. Original safe error codes remain in report.json.
LIMITS = {
    "context_unresolved": "missing-context",
    "selected_context_too_large": "missing-context",
    "context_packet_too_large": "missing-context",
    "call_budget_exhausted": "budget-exhausted",
    "question_budget_exhausted": "budget-exhausted",
    "window_budget_exhausted": "budget-exhausted",
    "reference_pool_limited": "reference-pool-limited",
    "handoff_question_too_large": "reference-payload-limit",
    "question_too_large": "payload-limit",
    "single_line_too_large": "payload-limit",
    "reconciliation_too_large": "payload-limit",
    "relationship_payload_too_large": "relationship-payload-limit",
    "response_distribution": "choice-distribution-invalid",
    "choice_argmax_mismatch": "choice-argmax-mismatch",
    "choice_distribution_uninformative": "uninformative-choice",
    "action_target_limit": "action-target-limit",
    "response_schema": "response-schema-invalid",
    "response_probability": "response-value-invalid",
    "response_model": "response-model-mismatch",
    "response_usage": "response-usage-invalid",
    "response_truncated": "response-truncated",
    "response_invalid_json": "response-invalid-json",
    "response_sensitive": "response-sensitive",
    "response_too_large": "response-too-large",
    "evidence_payload_too_large": "evidence-payload-limit",
    "evidence_search_limited": "evidence-search-limited",
    "project_candidate_pool_limited": "project-search-limited",
    "project_index_limited": "project-index-limited",
    "action_step_limit": "action-step-limit",
    "action_evidence_payload_limited": "action-evidence-payload-limit",
    "reference_evidence_unavailable": "reference-evidence-unavailable",
    "run_deadline_exceeded": "run-deadline-exceeded",
    "request_too_large": "payload-limit",
}


def annotate_findings(search, selected):
    """Run after reconciliation; never add, remove or rewrite a suspicion."""
    annotations = {}
    groups = {g["id"]: g for g in search.groups}
    for index, target in enumerate(sorted(selected), 1):
        read_with, failed_source = references(
            search, target, "source", source_candidates(search, target))
        read_with = merge_read_with(read_with, relationship_read_with(search, target))
        failed_source = failed_source or any(i["affects_completion"] and
            i["phase"] == "handoff-relationship" and i["range"] == [target.start, target.end]
            for i in search.issues)
        if failed_source and read_with["status"] == "not-identified":
            read_with["status"] = "unknown"
        if search.spec_source is None:
            requirement = {"status": "not-supplied", "ranges": [], "refs": [],
                           "additional_count": 0, "all_ranges": [],
                           "candidates_assessed": 0}
            failed_spec = False
        else:
            # A Jev-selected action may already have searched the specification.
            preselected = search.action_requirement_evidence.get(target)
            if preselected is not None:
                all_ranges = [[s.start, s.end] for s in preselected]
                stats = search.action_requirement_stats.get(target, {})
                failed = bool(stats.get("failed") or stats.get("pool_limited"))
                if all_ranges:
                    status = "selected"
                elif failed:
                    # A failed search is "unknown",
                    # never the positive claim "not-identified".
                    status = "unknown"
                else:
                    status = "not-identified"
                requirement = {"status": status,
                               "ranges": all_ranges[:DISPLAY_REFS],
                               "refs": [{"file": "SPEC", "range": r}
                                        for r in all_ranges[:DISPLAY_REFS]],
                               "additional_count": max(0, len(all_ranges)-DISPLAY_REFS),
                               "all_ranges": all_ranges,
                               "candidates_assessed": stats.get(
                                   "answered", len(all_ranges))}
                failed_spec = failed
            else:
                # Prefer the last directly successful view; otherwise use a real
                # recorded support view. This selection copies evidence, not scores.
                group = search.verified.get(target)
                if group is None:
                    group = groups[search.support[target][-1]["view_id"]]
                pool = {s: group for s in passages(search.spec_source.lines,
                             Span(1, len(search.spec_source.lines)))}
                requirement, failed_spec = references(search, target, "spec", pool)
        parents = sorted((s for s in selected if s != target and
                          s.start <= target.start and target.end <= s.end),
                         key=lambda s: (s.size, s.start))
        limits = set()
        for issue in search.issues:
            lo, hi = issue["range"]
            if not issue["affects_completion"]:
                continue
            if issue["phase"].startswith("handoff-"):
                relevant = [lo, hi] == [target.start, target.end]
            else:
                relevant = lo <= target.end and target.start <= hi
            if relevant:
                limits.add(LIMITS.get(issue["code"], "evaluation-failed"))
        for event in search.events:
            if event["phase"] == "localization_cap":
                # The pre-existing event carries a real target ID, not model text.
                a, b = map(int, event["target"][1:].split("-"))
                if a <= target.start and target.end <= b:
                    limits.add("localization-limit")
        failed = failed_source or failed_spec
        annotations[target] = {"finding_id": f"F{index}",
            "assessment": assessment(search, target), "read_with": read_with,
            "relevant_requirement": requirement,
            "unresolved_parents": [[s.start, s.end] for s in parents],
            "limitations": sorted(limits), "handoff_incomplete": failed}
    return annotations


def compact_report(report):
    """Coordinates/status only; full probabilities and excerpts are not copied."""
    rows = []
    for f in report["findings"]:
        row = {"id": f["finding_id"], "suspect": [f["start_line"], f["end_line"]],
               "assessment": f["assessment"]}
        for key in ("read_with", "relevant_requirement"):
            entry = f[key]
            row[key] = {k: entry[k] for k in ("status", "ranges", "additional_count")}
            if key == "read_with":
                row[key]["refs"] = entry.get("refs",
                    [{"file": None, "range": r} for r in entry["ranges"]])
        parents = f["unresolved_parents"]
        if parents:
            row["unresolved_parents"] = parents[:DISPLAY_REFS]
            row["additional_parent_count"] = max(0, len(parents) - DISPLAY_REFS)
        if f["limitations"]:
            row["limitations"] = f["limitations"]
        rows.append(row)
    return {"schema_version": 2, "source": report["source"],
            "specification": report["specification"],
            "specification_mode": report["specification_mode"],
            "scan_status": report["scan_status"],
            "handoff_status": report["handoff_status"],
            "status": report["status"], "basis": "static-model-assessment-only",
            "line_numbering": "one-based-inclusive", "findings": rows}


def _range(bounds):
    return f"{bounds[0]}-{bounds[1]}"


def _refs(entry, prefix=""):
    refs = entry.get("refs")
    if refs is not None and refs:
        parts = []
        for ref in refs:
            label = ref.get("file")
            base = _range(ref["range"])
            parts.append((json.dumps(label, ensure_ascii=True) + ":" + base)
                         if label not in (None, "SPEC") else prefix + base)
        result = ",".join(parts)
    elif entry.get("ranges"):
        result = ",".join(prefix + _range(pair) for pair in entry["ranges"])
    else:
        return entry["status"]
    if entry["additional_count"]:
        result += f" (+{entry['additional_count']} more in report.json)"
    return result


def format_handoff(handoff):
    """Four shared header lines and two or three lines per finding; never scores."""
    source = handoff["source"]
    # JSON quoting escapes newline, terminal control and bidi characters in paths.
    quoted = lambda value: json.dumps(value, ensure_ascii=True)
    lines = ["FILE: " + quoted(source["name"]),
             "SNAPSHOT: sha256:" + source["sha256"]]
    spec = handoff["specification"]
    if spec is None:
        lines.append("SPEC: none; intent inferred")
    else:
        label = quoted(spec["name"])
        lines.append(f"SPEC: {label} [sha256:{spec['sha256']}; {spec['hash_basis']}]")
    lines.append(f"SCAN: {handoff['scan_status']} | HANDOFF: {handoff['handoff_status']}"
                 " | static assessment only | lines: 1-based inclusive")
    if not handoff["findings"]:
        lines.append("\nNo suspected ranges reported; not a correctness certificate.")
    for f in handoff["findings"]:
        lines.append(f"\n{f['id']} | suspect: {_range(f['suspect'])}"
                     f" | assessment: {f['assessment']}")
        lines.append("   read-with: " + _refs(f["read_with"]) +
                     " | relevant-requirement: " + _refs(f["relevant_requirement"], "SPEC:"))
        optional = []
        if f.get("unresolved_parents"):
            entry = {"ranges": f["unresolved_parents"],
                     "additional_count": f["additional_parent_count"]}
            optional.append("unresolved-parent: " + _refs(entry))
        if f.get("limitations"):
            optional.append("limitation: " + ",".join(f["limitations"]))
        if optional:
            lines.append("   " + " | ".join(optional))
    return "\n".join(lines) + "\n"
