"""Jev-led speculative screening, context selection and interval localization.

Only the gateway judges text. This executor supplies real candidate IDs, enforces
bounds, preserves unresolved suspicions, and builds a compact debugger handoff.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
import time

from . import __version__
from .core import Config, Source, Span, _coverage, windows, multiscale
from .core import contiguous_candidates
from .evidence import EvidenceRef, evidence_label, fitting_pair_state, excerpt
from .jev import (JevError, encode_request, plain, validate_answer_objects, validate_usage,
                  MODEL)
from .questions import (load_pack, noul, choice, screening, fitting_state,
                        state_for, pack_questions, target_text, action_choice,
                        evidence_noul, LENS_KINDS)


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class Search:
    def __init__(self, source, gateway, config, spec, project=None,
                 context_scope="declared_standalone", deadline_at=None):
        if not isinstance(source, Source) or not isinstance(config, Config):
            raise ValueError("invalid_scan_arguments")
        self.spec_source = spec if isinstance(spec, Source) else (
            Source.from_text(spec) if type(spec) is str else None)
        if isinstance(spec, Source):
            spec = "\n".join(spec.lines)
        if self.spec_source is not None and len(self.spec_source.lines) > 2000:
            raise ValueError("specification_too_many_lines")
        if spec is not None and (type(spec) is not str or not spec.strip()):
            raise ValueError("specification_must_be_nonempty_text")
        if spec is not None and len(spec.encode("utf-8")) > 8192:
            raise ValueError("specification_too_large")
        if hasattr(gateway, "check_sensitive"):
            gateway.check_sensitive(source.name, source.path,
                                    "\n".join(source.lines), spec,
                                    self.spec_source.path if self.spec_source else None)
        self.source, self.gw, self.cfg, self.spec = source, gateway, config, spec
        self.project = project
        self.context_scope = context_scope
        self.deadline_at = (time.monotonic() + config.run_deadline_seconds
                            if deadline_at is None else deadline_at)
        if hasattr(gateway, "set_run_deadline"):
            gateway.set_run_deadline(self.deadline_at)
        self.pack, self.pack_hash = load_pack()
        if hasattr(gateway, "check_sensitive"):
            gateway.check_sensitive(self.pack)
            if project is not None:
                for entry in project.entries:
                    gateway.check_sensitive(entry.relpath, entry.source.path,
                                            "\n".join(entry.source.lines))
        self.calls = self.questions = self.validated_questions = 0
        self.window_count = self.cache_hits = 0
        self.trace, self.observations, self.issues, self.events = [], [], [], []
        self.cache, self.groups, self.support, self.verified = {}, [], {}, {}
        self.verify_outcomes = {}
        self.rechecks, self.existence_checks, self.handoff_evidence = [], [], []
        self.action_targets = set()
        self.relationship_promotions = []
        self.evidence_by_target = {}
        self.evidence_searches, self.relationships, self.action_trace = [], [], []
        self.action_requirement_evidence = {}
        self.action_requirement_stats = {}
        self.evidence_refs = {}
        self.context_reassessed_states = set()
        self.usage = {"input_tokens": 0, "output_tokens": 0,
                      "attempts_with_known_usage": 0, "attempts_with_unknown_usage": 0}
        if self.project is not None and self.project.limited and self.source.lines:
            self.issue("project_index_limited", Span(1, len(self.source.lines)),
                       "project-index")

    def issue(self, code, region, phase, affects=True):
        item = {"code": code, "range": [region.start, region.end],
                "phase": phase, "affects_completion": affects}
        if item not in self.issues:
            self.issues.append(item)

    def expired(self, region, phase):
        if time.monotonic() < self.deadline_at:
            return False
        self.issue("run_deadline_exceeded", region, phase)
        return True

    def state_for(self, region, halo=0, extras=()):
        return state_for(self.source, region, halo, self.spec, extras,
                         context_scope=self.context_scope)

    def fitting_state(self, region, halo, probe, extras=()):
        return fitting_state(self.source, region, halo, self.spec, probe, extras,
                             context_scope=self.context_scope)

    def fitting_pair(self, target, ref, question):
        return fitting_pair_state(self.source, target, self.cfg.context_lines,
                                  self.spec, ref, question,
                                  context_scope=self.context_scope)

    def ask(self, state, questions, region, phase):
        """Byte-bounded batches; dependent decisions require a later request."""
        # Every Jev request carries the caller's declared scope (project-scope
        # fix); the judge sees the declaration, never an inference task.
        state = {**state, "context_scope": self.context_scope}
        result = {}
        if self.expired(region, phase):
            return result
        try:
            batches = list(pack_questions(state, questions))
        except JevError as exc:
            self.issue(exc.code, region, phase)
            return result
        for batch, failed in batches:
            if self.expired(region, phase):
                break
            if failed is not None:
                self.issue("question_too_large", region, phase)
                continue
            wire = encode_request(state, batch)
            key = hashlib.sha256(wire).hexdigest()
            if key in self.cache:
                # Never return cache-owned mutable dictionaries to a caller.
                result.update(deepcopy(self.cache[key]))
                self.cache_hits += 1
                continue
            error = None
            if self.calls >= self.cfg.max_calls:
                error = "call_budget_exhausted"
            elif self.questions + len(batch) > self.cfg.max_questions:
                error = "question_budget_exhausted"
            if error:
                self.issue(error, region, phase)
                continue
            self.calls += 1
            self.questions += len(batch)
            row = {"sequence": self.calls, "phase": phase,
                   "range": [region.start, region.end], "request_sha256": key,
                   "state_sha256": digest(state), "question_sha256": digest(batch),
                   "question_ids": list(batch), "question_count": len(batch),
                   "status": "failed", "answers": None, "error": None}
            try:
                sent = json.loads(wire)
                answers = self.gw.evaluate(sent["state"], sent["questions"], metadata={
                    "sequence": self.calls, "phase": phase,
                    "window_id": region.id, "source_sha256": self.source.sha256})
                # A conforming Gateway validates types and values atomically;
                # the engine re-checks shapes anyway; custom gateways are not trusted.
                answers = validate_answer_objects(answers, batch,
                                  getattr(self.gw, "rounding_places", None))
                # Menu size controls maximum mass tolerance, not semantic
                # informativeness. Retain the actual numeric diagnostic only.
                places = getattr(self.gw, "rounding_places", None)
                row["choice_mass_allowance"] = {
                    qid: str(Fraction(len(raw["probabilities"]), 2 * 10 ** places))
                    for qid, raw in answers.items()
                    if raw["type"] == "choice" and places is not None}
                self.cache[key] = answers
                result.update(deepcopy(answers))
                self.validated_questions += len(batch)
                row.update(status="validated", answers=plain(answers))
            except JevError as exc:
                # Only documented failure codes are recorded here; other
                # exceptions are real defects and must reach the CLI's
                # unexpected_error.
                error = exc.code
                row["error"] = error
                self.issue(error, region, phase)
            stats = getattr(self.gw, "last_call_stats", None)
            usage = None
            if (type(stats) is dict and stats.get("request_sha256") == key
                    and stats.get("usage") is not None):
                try:
                    usage = validate_usage(stats.get("usage"))
                except JevError as exc:
                    row["usage_error"] = exc.code
                    self.issue(exc.code, region, phase + "-usage")
            row["usage"] = usage
            if usage is None:
                self.usage["attempts_with_unknown_usage"] += 1
            else:
                self.usage["attempts_with_known_usage"] += 1
                for name in ("input_tokens", "output_tokens"):
                    self.usage[name] += usage[name]
            self.trace.append(row)
            if self.expired(region, phase):
                break
        return result

    def add_support(self, span, score, origin, state, group_id):
        if score >= self.cfg.report_threshold:
            self.support.setdefault(span, []).append({
                "score": str(score), "origin": origin, "view_id": group_id,
                "state_sha256": digest(state)})

    def view(self, region, targets, depths, state, phase):
        state = {**state, "context_scope": self.context_scope}
        remaining = self.cfg.max_windows - self.window_count
        if len(targets) > remaining:
            self.issue("window_budget_exhausted", region, phase)
            targets = targets[:remaining]
        if not targets:
            return None
        self.window_count += len(targets)
        group = {"id": f"V{len(self.groups) + 1}", "region": region,
                 "targets": targets, "depths": depths, "state": state,
                 "phase": phase, "scores": {}, "priority": [], "context_needed": None}
        self.groups.append(group)
        answers = self.ask(state, screening(self.pack, region, targets,
                                             bug_lenses=self.cfg.bug_lenses, state=state),
                           region, phase)
        context = [{"start_line": e["start_line"], "end_line": e["end_line"]}
                   for e in state["excerpts"]]
        for span in targets:
            judgments = {}
            answer = answers.get("screen_" + span.id)
            if answer is not None:
                judgments["screen"] = answer["noul"]
            if self.cfg.bug_lenses:
                for kind in LENS_KINDS:
                    answer = answers.get(f"{kind}_{span.id}")
                    if answer is not None:
                        judgments[kind] = answer["noul"]
            score = max(judgments.values()) if judgments else None
            group["scores"][span] = score
            self.observations.append({
                "view_id": group["id"], "window_id": span.id,
                "start_line": span.start, "end_line": span.end,
                "depth": depths.get(span, 0), "phase": phase,
                "score": str(score) if score is not None else None,
                "judgments": {k: str(v) for k, v in judgments.items()},
                "status": "scored" if score is not None else "unreviewed",
                "context": context, "state_sha256": digest(state)})
            if score is not None:
                # Report support comes from the general screen only; lens
                # signals route investigation but never create support.
                general = judgments.get("screen")
                if general is not None:
                    self.add_support(span, general, "screen", state,
                                     group["id"])
                if score >= self.cfg.drill_threshold:
                    self.action_targets.add(span)
        if "need_context" in answers:
            group["context_needed"] = answers["need_context"]["noul"]
        for qid, answer in answers.items():
            # Gate: the weaker of stated confidence and
            # the chosen option's own displayed probability executes.
            if qid.startswith("rank_") and min(
                    answer["confidence"],
                    answer["probabilities"][answer["choice"]],
            ) >= self.cfg.choice_confidence:
                selected = next((s for s in targets if s.id == answer["choice"]), None)
                if selected is not None:
                    group["priority"].extend(self.gated_candidates(
                        answer, targets, self.cfg.localization_beam_width))
        return group

    def gated_candidates(self, answer, candidates, limit):
        """Use the vector within one menu; every executed option keeps its gate."""
        if answer["choice"] not in {s.id for s in candidates}:
            return []
        eligible = [s for s in candidates if s.id in answer["probabilities"] and
                    min(answer["confidence"], answer["probabilities"][s.id])
                    >= self.cfg.choice_confidence]
        eligible.sort(key=lambda s: (-answer["probabilities"][s.id],
                                      s.id != answer["choice"], s.size, s.start))
        return eligible[:limit]

    def initial_scan(self):
        count = len(self.source.lines)
        if not count:
            return
        entire = Span(1, count)
        roots = windows(entire, self.cfg.width)
        if self.cfg.whole_file and entire not in roots:
            state = self.fitting_state(entire, 0, noul(self.pack, "screen", entire))
            if state is None:
                self.issue("whole_file_not_submitted_size", entire, "whole", False)
            else:
                self.view(entire, [entire], {entire: -1}, state, "whole")
        pending, seen = deque(roots), set(roots)
        while pending:
            region = pending.popleft()
            if self.expired(region, "screen"):
                break
            if self.window_count >= self.cfg.max_windows:
                self.issue("window_budget_exhausted", region, "screen")
                # The union coverage below also exposes every unplanned tail line.
                break
            state = self.fitting_state(region, self.cfg.context_lines,
                                       noul(self.pack, "screen", region))
            if state is None:
                if region.size == 1:
                    self.issue("single_line_too_large", region, "screen")
                else:
                    self.issue("broad_view_split_for_size", region, "screen", False)
                    for part in windows(region, (region.size + 1) // 2):
                        if part not in seen:
                            seen.add(part)
                            pending.append(part)
                continue
            tree, capped = multiscale(
                region, self.cfg.min_width, self.cfg.max_depth,
                self.cfg.max_windows - self.window_count)
            if capped:
                self.issue("window_budget_exhausted", region, "screen")
            self.view(region, [s for s, _ in tree], dict(tree), state, "screen")

    def context_pool(self, group):
        region = group["region"]
        all_packets = windows(Span(1, len(self.source.lines)), self.cfg.width)
        excerpts = group["state"]["excerpts"]
        pool = [p for p in all_packets if not any(
            e["start_line"] <= p.start and p.end <= e["end_line"] for e in excerpts)]
        cap = self.cfg.max_context_packets
        near = sorted(pool, key=lambda p: (min(abs(p.start - region.end),
                                                abs(p.end - region.start)), p.start))
        chosen = near[:cap // 2]
        slots = cap - len(chosen)
        for i in range(slots):
            if pool:
                candidate = pool[i * (len(pool) - 1) // max(1, slots - 1)]
                if candidate not in chosen:
                    chosen.append(candidate)
        for candidate in near:
            if len(chosen) >= cap:
                break
            if candidate not in chosen:
                chosen.append(candidate)
        return chosen, len(pool)

    def enrich(self, group):
        """One bounded retrieval round. Candidates are read, never hallucinated."""
        region = group["region"]
        pool, available = self.context_pool(group)
        relevant = []
        self.events.append({"phase": "context_pool", "view_id": group["id"],
                            "available_packets": available,
                            "examined_pool": [s.id for s in pool],
                            "pool_limited": len(pool) < available})
        # Fit several candidate packets into a shared state. The full text of
        # each option is visible. No candidate is represented only by a label.
        bins, current, current_state = [], [], None
        for packet in pool:
            probe = noul(self.pack, "relevance", region, packet=target_text(packet))
            state = self.fitting_state(region, self.cfg.context_lines,
                                       probe, (*current, packet))
            if state is None and current:
                bins.append((current, current_state))
                current = []
                state = self.fitting_state(region, self.cfg.context_lines, probe, (packet,))
            if state is None:
                self.issue("context_packet_too_large", region, "context")
                continue
            current.append(packet)
            current_state = state
        if current:
            bins.append((current, current_state))
        for packets, state in bins:
            questions = {"relevance_" + p.id: noul(self.pack, "relevance", region,
                         packet=target_text(p)) for p in packets}
            picker = choice(self.pack, "context_pick", region, packets)
            picker["criteria"]["none"] = "No supplied packet provides needed context."
            picker["criteria"]["unlocalized"] = "The needed context is not identified."
            questions["context_where"] = picker
            answers = self.ask(state, questions, region, "context")
            pick = answers.get("context_where")
            preferred = (pick["choice"] if pick is not None and
                         min(pick["confidence"],
                             pick["probabilities"][pick["choice"]],
                             ) >= self.cfg.choice_confidence else None)
            for packet in packets:
                answer = answers.get("relevance_" + packet.id)
                if answer is not None and answer["noul"] >= self.cfg.context_threshold:
                    relevant.append((answer["noul"], packet, packet.id == preferred))
        # Independent Noul relevance orders packets across batches. Choice only
        # breaks equal-relevance ties; its menu-dependent probabilities are not
        # compared with probabilities from another menu.
        relevant.sort(key=lambda item: (-item[0], not item[2], item[1]))
        # A small fixed evidence capacity, not a semantic model replacing Jev.
        selected = [p for _, p, _ in relevant[:2]]
        self.events.append({"phase": "context_selected", "view_id": group["id"],
                            "packets": [s.id for s in selected],
                            "additional_relevant_packets": max(0, len(relevant) - 2)})
        if not selected:
            self.issue("context_unresolved", region, "context")
            return
        state = self.fitting_state(region, self.cfg.context_lines,
                                   noul(self.pack, "screen", region), selected)
        if state is None:
            self.issue("selected_context_too_large", region, "context")
            return
        enriched = self.view(region, group["targets"], group["depths"], state, "enriched")
        if enriched is not None and (enriched["context_needed"] is None or
                enriched["context_needed"] >= self.cfg.context_threshold):
            self.issue("context_unresolved", region, "context")

    def verify(self, group, spans):
        if not spans:
            return
        questions = {"verify_" + span.id: noul(self.pack, "verify", span) for span in spans}
        answers = self.ask(group["state"], questions, group["region"], "verify")
        for span in spans:
            answer = answers.get("verify_" + span.id)
            value = answer["noul"] if answer is not None else None
            self.verify_outcomes.setdefault(span, []).append(value)
            self.rechecks.append({"target": [span.start, span.end],
                                  "view_id": group["id"],
                                  "state_sha256": digest(group["state"]),
                                  "score": str(value) if value is not None else None})
            if value is not None and value >= self.cfg.drill_threshold:
                self.action_targets.add(span)
            if value is not None and value >= self.cfg.report_threshold:
                self.verified[span] = group
                self.add_support(span, value, "direct_recheck", group["state"], group["id"])

    def localize(self, group, target):
        candidates = contiguous_candidates(target)
        examined = 0
        while examined < self.cfg.max_localizations:
            if self.expired(target, "localize"):
                return
            questions = {"exists": noul(self.pack, "screen", target),
                         "where": choice(self.pack, "localize", target, candidates)}
            answers = self.ask(group["state"], questions, target, "localize")
            if set(answers) != {"exists", "where"}:
                return
            where = answers["where"]
            event = {"phase": "localize", "target": target.id,
                     "view_id": group["id"], "choice": where["choice"],
                     "confidence": str(where["confidence"]),
                     "exists": str(answers["exists"]["noul"])}
            self.events.append(event)
            self.existence_checks.append({
                "target": [target.start, target.end], "view_id": group["id"],
                "score": str(answers["exists"]["noul"])})
            if (answers["exists"]["noul"] < self.cfg.drill_threshold
                    or min(where["confidence"],
                           where["probabilities"][where["choice"]],
                           ) < self.cfg.choice_confidence):
                return
            selected = self.gated_candidates(where, candidates, min(
                self.cfg.localization_beam_width, self.cfg.max_localizations - examined))
            if not selected:  # Explicit none/unlocalized, not a clean verdict.
                return
            self.verify(group, selected)
            examined += len(selected)
            for span in selected:
                candidates.remove(span)  # Exact options only; source unchanged.
        self.events.append({"phase": "localization_cap", "view_id": group["id"],
                            "target": target.id, "limit": self.cfg.max_localizations})

    def detailed_search(self):
        for group in self.groups:
            if self.expired(group["region"], "verify"):
                break
            scored = [s for s, score in group["scores"].items()
                      if score is not None and score >= self.cfg.drill_threshold]
            order = list(dict.fromkeys(group["priority"] + scored))
            # Every independently hot interval survives; Choice does not prune it.
            self.verify(group, order)
            for span in order:
                if span.size <= min(20, self.cfg.min_width) and self.cfg.max_localizations:
                    self.localize(group, span)


    @staticmethod
    def _spread(items, limit):
        if len(items) <= limit:
            return list(items)
        if limit == 1:
            return [items[0]]
        return [items[i * (len(items) - 1) // (limit - 1)]
                for i in range(limit)]

    def _same_file_evidence_roots(self, target):
        """Mechanical whole-file evidence candidates outside the target."""
        count = len(self.source.lines)
        width = max(self.cfg.evidence_min_width, self.cfg.width * 2)
        refs = []
        for lo, hi in ((1, target.start - 1), (target.end + 1, count)):
            if lo > hi:
                continue
            region = Span(lo, hi)
            for span in windows(region, min(width, region.size)):
                refs.append(EvidenceRef(self.source, self.source.name, span, "source"))
        return refs

    def _project_evidence_roots(self, target):
        if self.project is None:
            return []
        refs = []
        width = max(self.cfg.evidence_min_width, self.cfg.width * 2)
        for entry in self.project.entries:
            if not entry.source.lines:
                continue
            entire = Span(1, len(entry.source.lines))
            for span in windows(entire, min(width, entire.size)):
                refs.append(EvidenceRef(entry.source, entry.relpath, span, "project"))
        total = len(refs)
        limited = total > self.cfg.max_project_candidates
        refs = self._spread(refs, self.cfg.max_project_candidates)
        if limited:
            self.issue("project_candidate_pool_limited", target, "evidence-project")
            self.events.append({"phase": "project_candidate_pool",
                                "available": total,
                                "examined": len(refs),
                                "limited": True})
        return refs

    def _evaluate_evidence(self, target, ref, phase):
        self.evidence_refs[ref.id] = ref
        label = evidence_label(ref)
        question = evidence_noul(self.pack, "relevance", target, label)
        state = self.fitting_pair(target, ref, question)
        if state is None:
            self.evidence_searches.append({"target": [target.start, target.end],
                "phase": phase, "candidate": ref.as_dict(), "score": None,
                "state_sha256": None, "status": "unsubmitted-payload-limit"})
            return None, None
        qid = "evidence_" + ref.id
        answer = self.ask(state, {qid: question}, target, phase).get(qid)
        score = answer["noul"] if answer is not None else None
        self.evidence_searches.append({
            "target": [target.start, target.end], "phase": phase,
            "candidate": ref.as_dict(),
            "score": str(score) if score is not None else None,
            "state_sha256": digest(state),
            "status": "assessed" if score is not None else "unknown"})
        return score, state

    def hierarchical_evidence_search(self, target, kind, group):
        """Jev-led broad-to-narrow evidence search over file or repository.

        Python supplies immutable passages and line coordinates. Jev alone decides
        semantic relevance. Multiple branches survive through a bounded beam.
        """
        if kind == "source":
            current = self._same_file_evidence_roots(target)
        elif kind == "project":
            current = self._project_evidence_roots(target)
        else:
            raise ValueError("unknown_evidence_search_kind")
        if not current:
            return []
        found = {}
        examined = 0
        seen = set()
        for depth in range(self.cfg.evidence_max_depth + 1):
            if self.expired(target, f"evidence-{kind}"):
                break
            if not current:
                break
            unique = []
            for ref in current:
                key = (ref.label, ref.source.sha256, ref.span.start, ref.span.end, ref.kind)
                if key not in seen:
                    seen.add(key)
                    unique.append(ref)
            current = unique
            remaining = self.cfg.max_evidence_candidates - examined
            if remaining <= 0:
                self.issue("evidence_search_limited", target, f"evidence-{kind}")
                self.events.append({"phase": "evidence_search_limit",
                                    "target": target.id, "kind": kind})
                break
            if len(current) > remaining:
                current = self._spread(current, remaining)
                self.issue("evidence_search_limited", target, f"evidence-{kind}")
                self.events.append({"phase": "evidence_search_limit",
                                    "target": target.id, "kind": kind})
            scored = []
            oversize_children = []
            for ref in current:
                if self.expired(target, f"evidence-{kind}"):
                    break
                examined += 1
                score, state = self._evaluate_evidence(
                    target, ref, f"evidence-{kind}-d{depth}")
                if score is None:
                    if state is not None:
                        # A failed judgment is not a size failure or permission
                        # to retry it on several smaller versions of the input.
                        continue
                    # If a broad candidate cannot fit, smaller children can still fit.
                    if (ref.span.size > self.cfg.evidence_min_width
                            and depth < self.cfg.evidence_max_depth):
                        width = max(self.cfg.evidence_min_width,
                                    math.ceil(ref.span.size / 2))
                        for child in windows(ref.span, width):
                            oversize_children.append(EvidenceRef(
                                ref.source, ref.label, child, ref.kind))
                    else:
                        self.issue("evidence_payload_too_large", target,
                                   f"evidence-{kind}")
                    continue
                if score >= self.cfg.context_threshold:
                    record = {"ref": ref, "score": score, "state": state,
                              "depth": depth}
                    found[(ref.kind, ref.label, ref.source.sha256, ref.span.start, ref.span.end)] = record
                    scored.append(record)
            scored.sort(key=lambda r: (-r["score"], r["ref"].span.size,
                                       r["ref"].label, r["ref"].span.start))
            selected = scored[:self.cfg.evidence_beam_width]
            children = []
            for record in selected:
                ref = record["ref"]
                if (depth >= self.cfg.evidence_max_depth or
                        ref.span.size <= self.cfg.evidence_min_width):
                    continue
                width = max(self.cfg.evidence_min_width,
                            math.ceil(ref.span.size / 2))
                for child in windows(ref.span, width):
                    children.append(EvidenceRef(ref.source, ref.label, child, ref.kind))
            current = oversize_children + children
        records = list(found.values())
        records.sort(key=lambda r: (-r["depth"], -r["score"],
                                    r["ref"].span.size, r["ref"].label,
                                    r["ref"].span.start))
        bucket = self.evidence_by_target.setdefault(target, {})
        for record in records:
            ref = record["ref"]
            key = (ref.kind, ref.label, ref.source.sha256, ref.span.start, ref.span.end)
            previous = bucket.get(key)
            if previous is None or record["score"] > previous["score"]:
                bucket[key] = record
        self.events.append({"phase": "hierarchical_evidence_search",
                            "target": target.id, "kind": kind,
                            "examined": examined, "qualified": len(records),
                            "selected": [r["ref"].as_dict() for r in
                                         records[:self.cfg.evidence_beam_width]]})
        return records

    def _relationship_group(self, target, state, value, ref):
        group = {"id": f"XR{len(self.groups) + 1}", "region": target,
                 "targets": [target], "depths": {target: 0}, "state": state,
                 "phase": "relationship", "scores": {target: value},
                 "priority": [], "context_needed": None}
        self.groups.append(group)
        return group

    def _relationship_candidates(self, target):
        """Prefer semantically strong but spatially distinct evidence passages.

        Hierarchical search intentionally retains parents and descendants. Testing
        only the top-N raw scores can therefore spend every relationship call on
        nested versions of one passage. This deterministic selector first keeps
        non-overlapping passages within each source/file, then fills any remaining
        capacity from the original ranking. It does not judge semantic relevance.
        """
        if self.cfg.max_relationships <= 0:
            return []
        records = list(self.evidence_by_target.get(target, {}).values())
        records.sort(key=lambda r: (-r["score"], r["ref"].span.size,
                                    r["ref"].label, r["ref"].span.start))
        selected, deferred = [], []
        occupied = {}
        for record in records:
            ref = record["ref"]
            source_key = (ref.kind, ref.label, ref.source.sha256)
            spans = occupied.setdefault(source_key, [])
            if any(s.start <= ref.span.end and ref.span.start <= s.end for s in spans):
                deferred.append(record)
                continue
            if len(selected) >= self.cfg.max_relationships:
                return selected
            selected.append(record)
            spans.append(ref.span)
        for record in deferred:
            if len(selected) >= self.cfg.max_relationships:
                break
            selected.append(record)
        return selected

    def test_relationships(self, target):
        """Ask Jev whether target/evidence relationships support a bug suspicion."""
        for record in self._relationship_candidates(target):
            if self.expired(target, "relationship"):
                break
            ref = record["ref"]
            self.evidence_refs[ref.id] = ref
            label = evidence_label(ref)
            relation_q = evidence_noul(self.pack, "relation", target, label)
            state = self.fitting_pair(target, ref, relation_q)
            if state is None:
                self.issue("relationship_payload_too_large", target, "relationship")
                continue
            qid = "relation_" + ref.id
            vqid = "verify_evidence_" + ref.id
            verify_q = evidence_noul(self.pack, "verify_with_evidence", target, label)
            # These propositions share fixed evidence; neither consumes the
            # other's answer. They are validated atomically when one batch fits.
            answers = self.ask(state, {qid: relation_q, vqid: verify_q}, target,
                               "relationship")
            relation = answers.get(qid)
            relation_value = relation["noul"] if relation is not None else None
            row = {"target": [target.start, target.end],
                   "evidence": ref.as_dict(),
                   "relation_score": (str(relation_value)
                                      if relation_value is not None else None),
                   "verify_score": None, "state_sha256": digest(state)}
            if (relation_value is not None and
                    relation_value >= self.cfg.relation_threshold):
                answer = answers.get(vqid)
                value = answer["noul"] if answer is not None else None
                row["verify_score"] = str(value) if value is not None else None
                # Relationship rechecks join the same-span history used by
                # direct rechecks; a failed check stays unknown, never a
                # negative.
                self.verify_outcomes.setdefault(target, []).append(value)
                self.rechecks.append({"target": [target.start, target.end],
                                      "view_id": "relationship:" + ref.id,
                                      "state_sha256": digest(state),
                                      "score": str(value) if value is not None else None,
                                      "evidence": ref.as_dict()})
                # Promotion is deferred (finalize_relationship_promotions):
                # order-independent, and only when no completed same-span
                # recheck contradicts the candidate.
                self.relationship_promotions.append((target, value, state, ref))
                # A newly supported relationship may ignite an initially cold
                # target. Route it now; report promotion still waits for every
                # same-span recheck and its contrary-evidence veto.
                if value is not None and value >= self.cfg.drill_threshold:
                    self.action_targets.add(target)
            self.relationships.append(row)

    def project_relationship_pass(self):
        """Project scope: related evidence may ignite a cold target.

        Cross-file defects are invisible in isolation (the smoke proved it:
        a contract mismatch screens cold until the sibling file is read).
        For maximal regions (warm or cold), one bounded project evidence
        search plus relationship testing runs before detailed_search; hot
        relation and verification create support through the normal
        promotion path. Cost is bounded by the existing evidence and
        relationship caps.
        """
        if (self.context_scope != "project" or self.project is None
                or not self.project.entries):
            return
        regions = {}
        for group in self.groups:
            region = group["region"]
            regions.setdefault((region.start, region.end), {
                "region": region, "group": group})
        for key, entry in regions.items():
            start, end = key
            if self.expired(entry["region"], "evidence-project"):
                break
            if any(s <= start and end <= e and (e - s) > (end - start)
                   for s, e in regions):
                continue
            self.events.append({"phase": "project_relationship_pass",
                                "target": [start, end]})
            group = entry["group"]
            # Attach relationships to the region's hottest target so rows
            # key to the refined span; fall back to the region itself for
            # cold ignition. One span per maximal region bounds the cost.
            hot = [s for s, v in group.get("scores", {}).items()
                   if v is not None and v >= self.cfg.drill_threshold]
            span = (min(hot, key=lambda s: (-(group["scores"][s]), s.size))
                    if hot else entry["region"])
            self.hierarchical_evidence_search(span, "project", group)
            self.test_relationships(span)

    def reassess_context_with_evidence(self):
        """Resolve a prior context limitation only with a fresh, validated Noul.

        Evidence can arrive after the same-file context phase. A relationship
        score does not itself establish that all missing context was supplied.
        Keep the original issue and an auditable resolution event; unrelated
        failures and contrary rechecks are never cleared here.
        """
        for issue in list(self.issues):
            if issue["code"] != "context_unresolved" or not issue["affects_completion"]:
                continue
            region = Span(*issue["range"])
            groups = [g for g in self.groups if g["region"] == region
                      and g["phase"] in {"whole", "screen", "enriched"}]
            if not groups:
                continue
            records = {}
            for target, bucket in self.evidence_by_target.items():
                if region.start <= target.start and target.end <= region.end:
                    for record in bucket.values():
                        ref = record["ref"]
                        if ref.id not in records or record["score"] > records[ref.id]["score"]:
                            records[ref.id] = record
            if not records:
                continue
            state = deepcopy(groups[-1]["state"])
            question = noul(self.pack, "need_context", region)
            state.setdefault("related_sources", [])
            supplied = []
            for record in sorted(records.values(), key=lambda r: (
                    -r["score"], r["ref"].span.size, r["ref"].id)):
                if len(supplied) >= self.cfg.evidence_beam_width:
                    break
                ref = record["ref"]
                if (ref.source is self.source and any(
                        e["start_line"] <= ref.span.start and ref.span.end <= e["end_line"]
                        for e in state["excerpts"])):
                    continue
                if any(e.get("id") == ref.id for e in state["related_sources"]):
                    continue
                state["related_sources"].append(excerpt(ref))
                try:
                    encode_request(state, {"need_context": question})
                except JevError as exc:
                    state["related_sources"].pop()
                    self.issue("context_packet_too_large" if exc.code == "request_too_large"
                               else exc.code, region, "context-reassessment")
                    continue
                supplied.append(ref.id)
            if not supplied:
                continue  # No new facts; do not ask for a different opinion.
            state_hash = digest(state)
            identity = (region.id, state_hash)
            if identity in self.context_reassessed_states:
                continue  # Includes failed asks: no retry on unchanged input.
            self.context_reassessed_states.add(identity)
            answers = self.ask(state, {"need_context": question}, region, "context-reassessment")
            answer = answers.get("need_context")
            value = answer["noul"] if answer is not None else None
            resolved = value is not None and value < self.cfg.context_threshold
            self.events.append({"phase": "context_reassessed", "target": region.id,
                                "state_sha256": state_hash, "evidence_ids": supplied,
                                "score": str(value) if value is not None else None,
                                "resolved": resolved})
            if resolved:
                issue["affects_completion"] = False

    def requirement_action_search(self, target, group):
        """Optional Jev-selected requirement search during the action loop."""
        if self.spec_source is None or not self.spec_source.lines:
            return []
        from .handoff import passages, capped
        pool = list(passages(
            self.spec_source.lines, Span(1, len(self.spec_source.lines))))
        candidates = capped(pool, self.cfg.max_handoff_candidates)
        limited = len(candidates) < len(pool)
        if limited:
            self.issue("reference_pool_limited", target, "action-requirement")
        selected = []
        answered = 0
        failed = limited
        questions = {}
        for candidate in candidates:
            # Untrusted spec text stays in structured state only; the
            # question references it by immutable coordinates.
            body = (f"SPEC:R{candidate.start}-R{candidate.end} (inclusive "
                    f"specification lines {candidate.start} through "
                    f"{candidate.end}) supplied in state")
            question = noul(self.pack, "requirement", target, passage=body)
            qid = f"action_requirement_R{candidate.start}-{candidate.end}"
            questions[qid] = question
        answers = self.ask(group["state"], questions, target, "action-requirement")
        for candidate in candidates:
            qid = f"action_requirement_R{candidate.start}-{candidate.end}"
            answer = answers.get(qid)
            if answer is None:
                failed = True
                continue
            answered += 1
            if answer["noul"] >= self.cfg.context_threshold:
                selected.append((answer["noul"], candidate))
        selected.sort(key=lambda x: (-x[0], x[1].size, x[1].start))
        self.action_requirement_evidence[target] = [s for _, s in selected]
        # Failed or unassessed asks must
        # not read as "no requirement". Counts are answered, not qualified.
        self.action_requirement_stats[target] = {
            "answered": answered, "assessed": answered, "attempted": len(candidates),
            "available": len(pool), "pool_limited": limited, "failed": failed}
        return selected

    def action_group(self, target):
        group = self.verified.get(target)
        if group is not None:
            return group
        for candidate in reversed(self.groups):
            if target in candidate.get("scores", {}):
                return candidate
        return None

    def action_options(self, target, performed):
        options = {
            "retain": "Retain the current suspicion at its present scope.",
            "finish": "Finish semantic investigation of this target for this run.",
        }
        if "search_file" not in performed and len(self.source.lines) > target.size:
            options["search_file"] = "Search elsewhere in this file for related evidence using hierarchical relevance checks."
        if ("test_relationship" not in performed and self.cfg.max_relationships
                and self.evidence_by_target.get(target)):
            options["test_relationship"] = "Test whether relationships between the target and selected evidence support a bug suspicion."
        if "search_requirement" not in performed and self.spec_source is not None:
            options["search_requirement"] = "Search supplied requirement passages for evidence relevant to this target."
        if "localize" not in performed and target.size <= 20 and self.cfg.max_localizations:
            options["localize"] = "Search exact smaller contiguous intervals inside this target."
        return options

    def investigation_group(self, target, base, performed, outcomes, options):
        """Supply new evidence and factual operations, never prior scores as proof."""
        state = deepcopy(base["state"])
        state["context_scope"] = self.context_scope
        state["investigation"] = {"completed_actions": sorted(performed),
            "outcomes": deepcopy(outcomes), "available_actions": list(options),
            "omitted_evidence_count": 0}
        records = list(self.evidence_by_target.get(target, {}).values())
        records.sort(key=lambda r: (-r["score"], r["ref"].span.size, r["ref"].id))
        state["investigation_evidence"] = []
        for record in records[:self.cfg.evidence_beam_width]:
            supplied = excerpt(record["ref"])
            state["investigation_evidence"].append(supplied)
            try:
                encode_request(state, {"probe": noul(self.pack, "continue_investigation", target)})
            except JevError as exc:
                if exc.code != "request_too_large":
                    raise
                state["investigation_evidence"].pop()
                state["investigation"]["omitted_evidence_count"] += 1
                self.issue("action_evidence_payload_limited", target, "action")
        group = {**base, "id": f"A{len(self.groups) + 1}", "region": target,
                 "targets": [target], "state": state, "phase": "action"}
        self.groups.append(group)
        return group

    def jev_action_loop(self):
        """Jev chooses bounded semantic investigation actions; Python executes them."""
        targets = sorted(self.action_targets, key=lambda s: (s.start, s.end))
        if not self.cfg.max_action_steps or not self.cfg.max_action_targets:
            self.events.append({"phase": "action_disabled", "available_targets": len(targets)})
            return
        if len(targets) > self.cfg.max_action_targets:
            self.events.append({"phase": "action_target_limit",
                                "available": len(targets),
                                "examined": self.cfg.max_action_targets})
            self.issue("action_target_limit",
                       Span(targets[0].start, targets[-1].end),
                       "action")
            targets = self._spread(targets, self.cfg.max_action_targets)
        for target in targets:
            if self.expired(target, "action"):
                break
            group = self.action_group(target)
            if group is None:
                continue
            # Project search is caller-declared scope, never a Jev decision
            # (project-scope fix): Jev cannot see the directory and must not
            # decide whether related files exist. One bounded search runs.
            if (self.context_scope == "project" and self.project is not None
                    and self.project.entries):
                self.hierarchical_evidence_search(target, "project", group)
            performed = set()
            outcomes = []
            terminal = "step-limit"
            for step in range(self.cfg.max_action_steps):
                options = self.action_options(target, performed)
                if set(options) == {"retain", "finish"}:
                    terminal = "available-actions-exhausted"
                    break
                action_group = self.investigation_group(target, group, performed, outcomes, options)
                continue_q = noul(self.pack, "continue_investigation", target)
                action_q = action_choice(self.pack, target, options)
                answers = self.ask(action_group["state"],
                                   {"continue": continue_q, "action": action_q},
                                   target, "action-select")
                if set(answers) != {"continue", "action"}:
                    terminal = "evaluation-failed"
                    break
                choice_answer = answers["action"]
                action = choice_answer["choice"]
                row = {"target": [target.start, target.end], "step": step + 1,
                       "continue": str(answers["continue"]["noul"]),
                       "action": action,
                       "confidence": str(choice_answer["confidence"]),
                       "executed": False, "outcome": None}
                self.action_trace.append(row)
                row["phase"] = "jev_action"
                self.events.append(row)
                if (answers["continue"]["noul"] < self.cfg.drill_threshold or
                        min(choice_answer["confidence"],
                            choice_answer["probabilities"][choice_answer["choice"]],
                            ) < self.cfg.choice_confidence):
                    terminal = "judge-stopped" if answers["continue"]["noul"] < self.cfg.drill_threshold else "uncertain-choice"
                    break
                if action in ("retain", "finish"):
                    row["executed"] = True
                    terminal = action
                    break
                performed.add(action)
                row["executed"] = True
                issue_count = len(self.issues)
                outcome = {"action": action, "status": "completed"}
                if action == "localize":
                    self.localize(action_group, target)
                elif action == "search_file":
                    records = self.hierarchical_evidence_search(target, "source", action_group)
                    outcome["evidence_candidates_found"] = len(records)
                elif action == "test_relationship":
                    previous = len(self.relationships)
                    self.test_relationships(target)
                    outcome["relationships_assessed"] = len(self.relationships) - previous
                elif action == "search_requirement":
                    self.requirement_action_search(target, action_group)
                    outcome["requirements_assessed"] = self.action_requirement_stats[target]["answered"]
                if any(i["affects_completion"] for i in self.issues[issue_count:]):
                    outcome["status"] = "incomplete"
                outcomes.append(outcome)
                row["outcome"] = outcome
            if terminal == "step-limit":
                remaining = set(self.action_options(target, performed)) - {"retain", "finish"}
                if remaining:
                    self.issue("action_step_limit", target, "action")
                else:
                    terminal = "available-actions-exhausted"
            self.events.append({"phase": "action_terminal", "target": target.id,
                                "reason": terminal, "completed_actions": sorted(performed)})

    def blocked(self, span):
        return any(item["affects_completion"] and
                   item["range"][0] <= span.end and span.start <= item["range"][1]
                   for item in self.issues)

    def reconcile(self):
        """Only Jev may justify replacing a broad suspicion with smaller ones.

        This is a model-localization heuristic, never a proof of bug coverage.
        Disagreement, errors, or absent finer support retain the broader range.
        """
        selected = set(self.support)
        for parent in sorted(self.support, key=lambda s: (s.size, s.start)):
            if parent not in self.verified or self.blocked(parent):
                continue
            if any(v is None or v < self.cfg.report_threshold
                   for v in self.verify_outcomes.get(parent, [])):
                continue
            children = sorted(s for s in selected if s in self.verified
                              and parent.start <= s.start and s.end <= parent.end
                              and s != parent)
            if not children:
                continue
            if any(self.blocked(child) or any(
                    value is None or value < self.cfg.report_threshold
                    for value in self.verify_outcomes.get(child, [])) for child in children):
                continue
            if len(children) > 200:
                self.events.append({"phase": "reconcile_limit", "target": parent.id})
                continue
            group = self.verified[parent]
            question = noul(self.pack, "covered", parent,
                            candidates=", ".join(target_text(s) for s in children))
            # Very many intervals can exceed the application instruction limit.
            if len(question["instructions"].encode()) > 12000:
                self.issue("reconciliation_too_large", parent, "reconcile")
                continue
            answers = self.ask(group["state"], {"covered": question}, parent, "reconcile")
            if answers and answers["covered"]["noul"] >= self.cfg.report_threshold:
                selected.remove(parent)
                self.events.append({"phase": "reconciled", "target": parent.id,
                                    "represented_by": [s.id for s in children],
                                    "score": str(answers["covered"]["noul"])})
        return selected

    def finalize_relationship_promotions(self):
        """Order-independent relationship promotion (promotion invariant).

        A relationship candidate is promoted only when its own verification
        reaches the report threshold and no completed same-span recheck,
        direct or relationship, contradicts it. Failed rechecks remain
        unknown and never count as negatives.
        """
        for target, value, state, ref in self.relationship_promotions:
            history = self.verify_outcomes.get(target, [])
            contrary = any(v is not None and v < self.cfg.report_threshold
                           for v in history)
            if (value is not None and value >= self.cfg.report_threshold
                    and not contrary):
                group = self._relationship_group(target, state, value, ref)
                self.verified[target] = group
                self.add_support(target, value, "relationship_recheck",
                                 state, group["id"])

    def run(self):
        ready = True
        if self.source.lines:
            probe = Span(1, 1)
            base = self.state_for(probe)
            base["excerpts"] = []
            try:
                encode_request(base, {"probe": noul(self.pack, "screen", probe)})
            except JevError as exc:
                self.issue(exc.code, Span(1, len(self.source.lines)), "preflight")
                ready = False
        if ready:
            self.initial_scan()
        # A bounded single context round; enriched views are not recursively
        # enriched. Whole-file views already contain all same-file context.
        for group in list(self.groups):
            if self.expired(group["region"], "context"):
                break
            need = group["context_needed"]
            if need is not None and need >= self.cfg.context_threshold:
                self.enrich(group)
        self.project_relationship_pass()
        self.reassess_context_with_evidence()
        self.detailed_search()
        self.jev_action_loop()
        self.reassess_context_with_evidence()
        self.finalize_relationship_promotions()
        selected = self.reconcile()
        assessed = [Span(o["start_line"], o["end_line"])
                    for o in self.observations if o["score"] is not None]
        coverage = _coverage(assessed, len(self.source.lines))
        complete = coverage["all_lines_assessed"] and not any(
            issue["affects_completion"] for issue in self.issues)
        scan_status = "complete" if complete else "incomplete"
        from .handoff import annotate_findings, compact_report
        annotations = annotate_findings(self, selected)
        complete = complete and not any(i["affects_completion"] for i in self.issues)
        findings = [{"window_id": s.id, "start_line": s.start, "end_line": s.end,
                     "resolution": ("directly_rechecked" if s in self.verified
                                    else "unresolved_initial_suspicion"),
                     "support": self.support[s], **annotations[s]}
                    for s in sorted(selected)]
        result = {"schema_version": 5, "tool_version": __version__, "model": MODEL,
                "question_pack_sha256": self.pack_hash,
                "source": {"name": self.source.name,
                           "sha256": self.source.sha256,
                           "line_count": len(self.source.lines),
                           "encoding": self.source.encoding},
                "configuration": self.cfg.as_dict(),
                "choice_rounding_places": getattr(self.gw, "rounding_places", None),
                "context_scope": self.context_scope,
                "specification_mode": "provided" if self.spec else "inferred",
                "specification_sha256": (hashlib.sha256(self.spec.encode()).hexdigest()
                                         if self.spec else None),
                "specification": ({"name": self.spec_source.name,
                    "sha256": self.spec_source.sha256,
                    "line_count": len(self.spec_source.lines),
                    "encoding": self.spec_source.encoding,
                    "hash_basis": ("original-bytes" if self.spec_source.path
                                   else "utf8-supplied-text")}
                    if self.spec_source else None),
                "scan_status": scan_status,
                "handoff_status": ("incomplete" if any(
                    a["handoff_incomplete"] for a in annotations.values()) else "complete"),
                "status": "complete" if complete else "incomplete",
                "calls_attempted": self.calls, "questions_attempted": self.questions,
                "usage": dict(self.usage),
                "questions_validated": self.validated_questions,
                "cache_hits": self.cache_hits, "windows_planned": self.window_count,
                "findings": findings, "coverage": coverage,
                "windows": self.observations, "requests": self.trace,
                "events": self.events, "issues": self.issues,
                "rechecks": self.rechecks, "existence_checks": self.existence_checks,
                "handoff_evidence": self.handoff_evidence,
                "evidence_searches": self.evidence_searches,
                "relationships": self.relationships,
                "jev_actions": self.action_trace,
                "project": ({"files_considered": self.project.files_considered,
                              "files_loaded": self.project.files_loaded,
                              "bytes_loaded": self.project.bytes_loaded,
                              "limited": self.project.limited,
                              "skipped_binary_or_invalid": self.project.skipped_binary_or_invalid,
                              "skipped_secret": self.project.skipped_secret,
                              "skipped_size": self.project.skipped_size,
                              "limited_file_count": self.project.limited_file_count,
                              "limited_total_bytes": self.project.limited_total_bytes,
                              "unreadable_files": self.project.unreadable_files,
                              "unreadable_directories": self.project.unreadable_directories,
                              "limited_line_count": self.project.limited_line_count,
                              "deadline_exceeded": self.project.deadline_exceeded}
                             if self.project is not None else None)}
        result["handoff"] = compact_report(result)
        return result
