"""Opt-in scoped verification integrated with the existing bounded executor."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

from .claims import (ClaimKey, SourceRef, EvidenceRevision, CONTRACT_VERSION,
                     ROLES, UNRESOLVED, identity, classify_evaluation,
                     active_evaluations, reconcile_claim, verification_questions)
from .jev import MODEL


class ScopedClaims:
    def __init__(self, search):
        self.search = search
        inventory = [(search.source.name, search.source.sha256, search.source.encoding)]
        if search.project:
            inventory += [(x.relpath, x.source.sha256, x.source.encoding)
                          for x in search.project.entries]
        self.scope_id = identity({"scope": search.context_scope, "inventory": sorted(inventory),
                                  "spec": search.spec_source.sha256 if search.spec_source else None})
        self.claims, self.evaluations, self.states, self.revisions = {}, {}, {}, {}
        self.dispositions = {}
        self.adjudicated = set()
        self.eligible = set()

    def reference(self, source, label, start, end):
        if end > len(source.lines):
            raise ValueError("reference_outside_snapshot")
        return SourceRef(self.scope_id, label, source.sha256, source.encoding, start, end)

    def key(self, target, ref):
        s = self.search
        return ClaimKey(self.reference(s.source, s.source.name, target.start, target.end),
                        self.scope_id, s.spec_source.sha256 if s.spec_source else None,
                        self.reference(ref.source, ref.label, ref.span.start, ref.span.end)
                        if ref else None,
                        "target-counterpart-failure-v1" if ref else "region-existence-v1")

    def evaluate(self, target, supplied, *, ref=None, origin="verify", group_id=None,
                 extra_questions=None, adjudication=False, supersedes=(), covered_revisions=()):
        from .engine import digest
        s = self.search
        key = self.key(target, ref)
        self.claims[key.id] = key
        state = deepcopy(supplied)
        state["context_scope"] = s.context_scope
        proposition = ("At least one substantive defect occurs in or involves the bound target "
                       "under the supplied specification, or conservatively inferred intent "
                       "when no specification is supplied. Exclude defects solely elsewhere, "
                       "formatting preferences and missing information alone.")
        if ref:
            proposition = ("The bound target contains or participates in a substantive failure "
                           "established by its interaction with the bound counterpart, judged "
                           "against the supplied specification or conservatively inferred intent. "
                           "The two passages may agree with each other while jointly violating "
                           "the specification. Consider all supplied guards and conditions. "
                           "Unrelated defects and missing information alone do not establish failure.")
        state["scoped_proposition"] = {**key.as_dict(), "statement": proposition,
                                       "contract_version": CONTRACT_VERSION}
        qs = verification_questions(target, CONTRACT_VERSION)
        if extra_questions:
            qs.update(extra_questions)
        answers = s.ask(state, qs, target, "claim-adjudication" if adjudication else "claim-verify")
        values = {role: answers[role]["noul"] if role in answers else None for role in ROLES}
        disposition = classify_evaluation(values, s.cfg.report_threshold,
                                          s.cfg.refute_threshold, s.cfg.context_threshold)
        # Reference identities describe exactly the supplied excerpts, not the
        # entire index. Ordering belongs to the rendered-state fingerprint.
        refs, supplied_ranges = [], []
        for item in state["excerpts"]:
            refs.append(self.reference(s.source, s.source.name,
                                      item["start_line"], item["end_line"]).id)
            supplied_ranges.append({"file": s.source.name, "source_sha256": s.source.sha256,
                                    "start_line": item["start_line"], "end_line": item["end_line"]})
        for item in state.get("related_sources", []) + state.get("investigation_evidence", []):
            refs.append(identity({k: item[k] for k in
                        ("file", "source_sha256", "start_line", "end_line")}))
            supplied_ranges.append({k: item[k] for k in
                                    ("file", "source_sha256", "start_line", "end_line")})
        revision = EvidenceRevision(self.scope_id, digest(state), tuple(refs))
        self.revisions[revision.id] = {"id": revision.id, **asdict(revision)}
        requests = s.request_origins_for(state, qs)
        row = {"claim_id": key.id, "evidence_revision_id": revision.id,
               "question_contract_sha256": identity(qs), "contract_version": CONTRACT_VERSION,
               "model": MODEL, "rounding_places": getattr(s.gw, "rounding_places", None),
               "roles": {k: str(v) if v is not None else None for k, v in values.items()},
               "disposition": disposition, "origin": origin, "origin_requests": requests,
               "group_id": group_id, "target": [target.start, target.end],
               "supplied_ranges": supplied_ranges,
               "route_qualified": (all(q in answers and answers[q]["noul"] >= s.cfg.relation_threshold
                                       for q in extra_questions) if extra_questions else True)}
        if adjudication and disposition in ("supported", "explicitly_refuted"):
            row.update(supersedes=list(supersedes), union_coverage_verified=True,
                       covered_evidence_revisions=list(covered_revisions))
        row["id"] = identity(row)
        history = self.evaluations.setdefault(key.id, {})
        history.setdefault(row["id"], row)
        self.states[row["id"]] = state
        merged = reconcile_claim(history.values())
        self.dispositions[key.id] = merged
        # Each counterpart has its own history. A low generic screen or another
        # counterpart's explicit refutation cannot overwrite this proposition.
        if merged == "conflicted" and not adjudication:
            self.adjudicate(target, key, ref)
            merged = self.dispositions[key.id]
        self.update_issues(target, key.id, row, merged)
        return row, answers

    def update_issues(self, target, claim_id, row, disposition):
        s = self.search
        if disposition in UNRESOLVED:
            item = {"id": identity([claim_id, row["evidence_revision_id"], disposition]),
                    "code": "claim_" + disposition, "range": [target.start, target.end],
                    "phase": "claim-verify", "affects_completion": True,
                    "claim_id": claim_id, "evidence_revision_id": row["evidence_revision_id"]}
            if not any(x.get("id") == item["id"] for x in s.issues):
                s.issues.append(item)
        elif disposition in ("supported", "explicitly_refuted") and row["disposition"] == disposition:
            for issue in s.issues:
                if issue.get("claim_id") == claim_id and issue["affects_completion"]:
                    issue["affects_completion"] = False
                    s.events.append({"phase": "claim_issue_resolution", "issue_id": issue["id"],
                                     "claim_id": claim_id, "evaluation_id": row["id"]})

    def adjudicate(self, target, key, ref):
        """One union assessment per actual set of evidence revisions; no retries."""
        from .engine import digest
        s = self.search
        history = self.evaluations[key.id]
        rows = sorted(history.values(), key=lambda x: x["id"])
        conflict_rows = [x for x in rows if x["disposition"] in
                         ("supported", "explicitly_refuted", "conflicted")]
        state_ids = tuple(sorted({x["evidence_revision_id"] for x in conflict_rows}))
        marker = (key.id, state_ids)
        if marker in self.adjudicated:
            return
        self.adjudicated.add(marker)
        if len(state_ids) < 2:
            return  # An internally contradictory answer is not retried unchanged.
        union = deepcopy(self.states[conflict_rows[0]["id"]])
        union.pop("scoped_proposition", None)
        for field in ("excerpts", "related_sources", "investigation_evidence", "evidence_coordinates"):
            passages = {}
            for row in conflict_rows:
                for passage in self.states[row["id"]].get(field, []):
                    passages[digest(passage)] = passage
            if passages:
                union[field] = [deepcopy(passages[k]) for k in sorted(passages)]
        def content_signature(view):
            return identity({field: sorted(digest(p) for p in view.get(field, []))
                             for field in ("excerpts", "related_sources", "investigation_evidence",
                                           "evidence_coordinates")})
        if any(content_signature(union) == content_signature(self.states[r["id"]])
               for r in conflict_rows):
            return  # Reordering or re-asking an existing view is not new evidence.
        # evaluate() adds the binding again; check its final size inside ask().
        # Union construction includes every original passage, never verdicts.
        # Non-conflicting missing/unknown views may contain passages absent
        # from this union. Keep them active unless a sufficient view covers them.
        covered_rows = {r["id"] for r in conflict_rows}
        extra = None
        if ref is not None:
            from .questions import evidence_noul
            from .evidence import evidence_label
            extra = {"relation_" + ref.id: evidence_noul(s.pack, "relation", target, evidence_label(ref))}
        row, _ = self.evaluate(target, union, ref=ref, origin="union-adjudication",
                               adjudication=True, supersedes=sorted(covered_rows),
                               covered_revisions=state_ids, extra_questions=extra)
        if row["disposition"] in ("supported", "explicitly_refuted"):
            self.dispositions[key.id] = reconcile_claim(history.values())
            self.update_issues(target, key.id, row, self.dispositions[key.id])
        else:
            self.update_issues(target, key.id, row, "conflicted")

    def promote(self):
        from .core import Span
        s = self.search
        for claim_id in sorted(self.eligible):
            disposition = self.dispositions[claim_id]
            # Disputed former support remains visible with a conflicting
            # assessment. Explicitly refuted propositions add no active support.
            if disposition not in ("supported", "conflicted", "supported_unresolved"):
                continue
            claim = self.claims[claim_id]
            target = Span(claim.target.start_line, claim.target.end_line)
            rows = [r for r in active_evaluations(self.evaluations[claim_id].values())
                    if r["disposition"] == "supported" and r["route_qualified"]]
            if not rows:
                continue
            row = sorted(rows, key=lambda r: r["id"])[0]
            state = self.states[row["id"]]
            value = Decimal(row["roles"]["supports_failure"])
            group = s._relationship_group(target, state, value, None)
            group["phase"] = "scoped-verification"
            s.verified[target] = group
            s.add_support(target, value, "scoped_verification", state, group["id"])
            s.support[target][-1].update(claim_id=claim_id, evaluation_id=row["id"],
                                          disposition=disposition)

    def assessment(self, target):
        ids = [key for key, claim in self.claims.items()
               if (claim.target.start_line, claim.target.end_line) == (target.start, target.end)]
        states = {self.dispositions[key] for key in ids}
        if "conflicted" in states:
            return "conflicting"
        if "supported_unresolved" in states:
            return "unknown"
        if "supported" in states:
            return "scoped-support"
        if states & UNRESOLVED:
            return "unknown"
        return "unresolved-initial-suspicion"

    def report(self):
        return {"scope_manifest_id": self.scope_id, "contract_version": CONTRACT_VERSION,
                "claims": [{**self.claims[k].as_dict(), "disposition": self.dispositions[k]}
                           for k in sorted(self.claims)],
                "evidence_revisions": [self.revisions[k] for k in sorted(self.revisions)],
                "evaluations": [row for k in sorted(self.evaluations)
                                for _, row in sorted(self.evaluations[k].items())]}
