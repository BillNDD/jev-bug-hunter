"""Score saved reports against private, frozen labels; never call Jev.

Labels stay out of scanner state. This module reads reports, not target programs.
An incomplete/missing report is not a clean negative. Coverage of a labeled bug
means a suspect interval contains its entire inclusive labeled span; exact
localization is reported separately so broad flags cannot look precise.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re
import sys

from .jev import MODEL


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid_count")
    return value


def _ranges(values, line_count):
    if type(values) is not list:
        raise ValueError("invalid_ranges")
    result = []
    for pair in values:
        if (type(pair) is not list or len(pair) != 2
                or any(type(n) is not int for n in pair)
                or not 1 <= pair[0] <= pair[1] <= line_count):
            raise ValueError("invalid_range")
        result.append(tuple(pair))
    return result


def evaluate(suite, reports):
    """reports maps opaque case IDs to already saved scanner reports."""
    if (type(suite) is not dict or suite.get("schema_version") != 1
            or suite.get("partition") != "heldout" or type(suite.get("cases")) is not list
            or not suite["cases"] or type(reports) is not dict):
        raise ValueError("invalid_suite")
    totals = dict(cases=0, complete=0, incomplete=0, missing=0, expected_bugs=0,
                  covered_bugs=0, exact_localizations=0, complete_run_misses=0,
                  unresolved_expected_bugs=0, valid_cases=0, valid_cases_flagged=0,
                  valid_cases_cleared=0, valid_cases_unresolved=0,
                  known_input_tokens=0, known_output_tokens=0,
                  unknown_usage_attempts=0, reports_without_usage=0)
    rows, seen, packs = [], set(), set()
    for case in suite["cases"]:
        if type(case) is not dict or set(case) != {"id", "source_sha256", "line_count", "bug_ranges"}:
            raise ValueError("invalid_case")
        ident, sha = case["id"], case["source_sha256"]
        if (type(ident) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", ident)
                or ident in seen or type(sha) is not str or not re.fullmatch(r"[a-f0-9]{64}", sha)):
            raise ValueError("invalid_case_identity")
        seen.add(ident)
        count = _count(case["line_count"])
        bugs = _ranges(case["bug_ranges"], count)
        report = reports.get(ident)
        suspects, status = [], "missing"
        if report is not None:
            if (type(report) is not dict or report.get("model") != MODEL
                    or report.get("status") not in ("complete", "incomplete")
                    or type(report.get("source")) is not dict
                    or type(report.get("configuration")) is not dict
                    or type(report.get("findings")) is not list
                    or report.get("source", {}).get("sha256") != sha
                    or report.get("source", {}).get("line_count") != count):
                raise ValueError("report_identity_mismatch")
            # The owner-fixed relation threshold is never tuned by this scorer.
            if Decimal(str(report.get("configuration", {}).get("relation_threshold"))) != Decimal("0.70"):
                raise ValueError("relation_threshold_mismatch")
            pack = report.get("question_pack_sha256")
            if type(pack) is not str or not re.fullmatch(r"[a-f0-9]{64}", pack):
                raise ValueError("missing_question_pack_identity")
            packs.add(pack)
            status = report["status"]
            suspects = _ranges([[f["start_line"], f["end_line"]] for f in report["findings"]], count)
            usage = report.get("usage")
            if usage is None:
                totals["reports_without_usage"] += 1
            else:
                totals["known_input_tokens"] += _count(usage["input_tokens"])
                totals["known_output_tokens"] += _count(usage["output_tokens"])
                totals["unknown_usage_attempts"] += _count(usage["attempts_with_unknown_usage"])
        covered = sum(any(a <= lo and hi <= b for a, b in suspects) for lo, hi in bugs)
        exact = sum(bug in suspects for bug in bugs)
        totals["cases"] += 1
        totals[status] += 1
        totals["expected_bugs"] += len(bugs)
        totals["covered_bugs"] += covered
        totals["exact_localizations"] += exact
        totals["complete_run_misses" if status == "complete" else "unresolved_expected_bugs"] += len(bugs) - covered
        if not bugs:
            totals["valid_cases"] += 1
            field = "valid_cases_flagged" if suspects else "valid_cases_cleared" if status == "complete" else "valid_cases_unresolved"
            totals[field] += 1
        rows.append({"id": ident, "source_sha256": sha, "status": status,
                     "expected_bugs": len(bugs), "covered_bugs": covered,
                     "exact_localizations": exact, "suspect_ranges": suspects})
    if set(reports) - seen:
        raise ValueError("unmatched_report")
    return {"schema_version": 1, "basis": "saved-reports-against-frozen-labels",
            "live_api_calls": 0, "model": MODEL, "question_pack_hashes": sorted(packs),
            "counts": totals,
            "observed_bug_coverage": totals["covered_bugs"] / totals["expected_bugs"] if totals["expected_bugs"] else None,
            "cases": rows}


def _read(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("evaluation_input_too_large")
    return json.loads(raw)


def main(argv=None):
    from .__main__ import SafeParser
    parser = SafeParser(prog="python -m bug_hunter.evaluation", description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--report", action="append", default=[], metavar="CASE_ID=REPORT_JSON")
    args = parser.parse_args(argv)
    try:
        reports = {}
        for item in args.report:
            ident, path = item.split("=", 1)
            if ident in reports:
                raise ValueError("duplicate_report")
            reports[ident] = _read(path)
        print(json.dumps(evaluate(_read(args.suite), reports), ensure_ascii=True, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        print("Evaluation failed (invalid_or_unreadable_input).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
