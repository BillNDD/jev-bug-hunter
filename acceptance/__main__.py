"""Freeze witnesses, run the installed CLI with real Jev, and score outcomes.

python -m acceptance freeze --out PATH
python -m acceptance live --suite PATH --python VENV_PYTHON --run NAME --live
python -m acceptance pause --budget PATH/budget.json
python -m acceptance unpause --budget PATH/budget.json
python -m acceptance live --suite PATH --python VENV_PYTHON --run NAME --live --resume
No synthetic provider is used by live. Resume skips every previously started
case; it does not retry failed/interrupted attempts or clear a paused ledger.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import signal
import subprocess
import sys
import time
import uuid

from .catalog import CATALOG_REVISION, MIGRATION, cases, verify_witnesses
from .ledger import loads, new_budget, read, save, set_paused, transaction

OUTCOME_GATE_REVISION = "source-and-spec-citations-v2"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def freeze(root, call_limit, *, predecessor=None):
    migration = deepcopy(MIGRATION)
    if predecessor is not None:
        previous = load_suite(predecessor)
        migration["predecessor_manifest_sha256"] = sha((predecessor / "manifest.json").read_bytes())
        migration["predecessor_schema"] = previous["schema"]
    root.mkdir(parents=True, exist_ok=False)
    save(root / "migration.json", migration)
    manifest = {"schema": "jev-outcome-acceptance-v2", "catalog_revision": CATALOG_REVISION,
                "outcome_gate_revision": OUTCOME_GATE_REVISION,
                "migration_sha256": sha((root / "migration.json").read_bytes()),
                "partition": "public-development-regression",
                "rounding_places": 2, "relation_threshold": "0.70", "cases": []}
    for case in cases():
        witness = verify_witnesses(case)
        folder = root / "inputs" / case["id"]
        folder.mkdir(parents=True)
        files = {case["name"]: case["source"], "requirements.txt": case["requirement"], **case["helpers"]}
        hashes = {}
        for name, text in files.items():
            raw = text.encode("utf-8")
            if name == case["name"] and case["encoding"] == "utf-8-bom-crlf":
                raw = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")
            (folder / name).write_bytes(raw)
            hashes[name] = sha(raw)
        manifest["cases"].append({k: case[k] for k in
            ("id", "family", "expected", "bugs", "scope", "name", "citation", "max_calls")} |
            {"files_sha256": hashes, "line_count": len(case["source"].splitlines()),
             "requirement_line_count": len(case["requirement"].splitlines()), "witness": witness})
    save(root / "manifest.json", manifest)
    (root / "manifest.sha256").write_text(sha((root / "manifest.json").read_bytes()) + "\n")
    save(root / "budget.json", new_budget(call_limit))
    print(json.dumps({"frozen_cases": len(manifest["cases"]), "expected": dict(Counter(
        c["expected"] for c in manifest["cases"])), "max_new_calls": call_limit,
        "manifest_sha256": sha((root / "manifest.json").read_bytes()), "provider_calls": 0}))


def load_suite(root):
    raw = (root / "manifest.json").read_bytes()
    if sha(raw) != (root / "manifest.sha256").read_text().strip():
        raise ValueError("frozen_manifest_changed")
    manifest = loads(raw)
    if manifest.get("schema") not in {"jev-outcome-acceptance-v1", "jev-outcome-acceptance-v2"}:
        raise ValueError("unsupported_frozen_suite_schema")
    if type(manifest.get("rounding_places")) is not int or manifest["rounding_places"] != 2 or manifest.get("relation_threshold") != "0.70":
        raise ValueError("unsupported_frozen_judge_profile")
    if manifest["schema"] == "jev-outcome-acceptance-v2" and sha((root / "migration.json").read_bytes()) != manifest.get("migration_sha256"):
        raise ValueError("frozen_migration_changed")
    base = root.resolve()
    inputs = (base / "inputs").resolve()
    if inputs != base / "inputs":
        raise ValueError("frozen_suite_path_escape")
    seen = set()
    for case in manifest["cases"]:
        if (type(case) is not dict or type(case.get("id")) is not str
                or not re.fullmatch(r"C[0-9]{3}", case["id"]) or case["id"] in seen
                or case["scope"] not in {"standalone", "isolate", "project"}
                or case["expected"] not in {"clear", "defect", "unresolved"}
                or type(case["max_calls"]) is not int or not 1 <= case["max_calls"] <= 100000
                or type(case["line_count"]) is not int or case["line_count"] < 1
                or type(case["files_sha256"]) is not dict
                or case["name"] not in case["files_sha256"]
                or len({name.casefold() for name in case["files_sha256"]}) != len(case["files_sha256"])
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.endswith(".")
                       or PureWindowsPath(name).is_reserved()
                       or type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest)
                       for name, digest in case["files_sha256"].items())):
            raise ValueError("invalid_frozen_suite")
        seen.add(case["id"])
        folder = inputs / case["id"]
        if folder.resolve() != folder:
            raise ValueError("frozen_suite_path_escape")
        if {p.name for p in folder.iterdir()} != set(case["files_sha256"]):
            raise ValueError("unexpected_file_in_scanned_scope")
        for name, digest in case["files_sha256"].items():
            path = folder / name
            if path.resolve() != path or not path.is_file():
                raise ValueError("frozen_suite_path_escape")
            if sha(path.read_bytes()) != digest:
                raise ValueError("frozen_input_changed")
    return manifest


def judge_outcome(case, report, exit_code):
    """Check observable usefulness, not internal calls, scores, or branch names."""
    failures = []
    if report is None:
        return {"passed": False, "failures": ["missing_report"], "covered_defects": 0,
                "expected_defects": len(case["bugs"]), "status": "missing", "findings": []}
    source = report.get("source", {})
    if source.get("sha256") != case["files_sha256"][case["name"]] or source.get("line_count") != case["line_count"]:
        failures.append("wrong_source_snapshot")
    findings = report.get("findings", [])
    spans = [(f["start_line"], f["end_line"]) for f in findings]
    if any(not 1 <= a <= b <= case["line_count"] for a, b in spans):
        failures.append("invalid_source_citation")
    status = report.get("status")
    expected_exit = (1 if findings else 0) if status == "complete" else 2
    if exit_code != expected_exit:
        failures.append("exit_and_report_disagree")
    covered = 0
    handoff_document = report.get("handoff", {})
    handoff = handoff_document.get("findings", [])
    if {tuple(row["suspect"]) for row in handoff} != set(spans):
        failures.append("handoff_findings_disagree")
    handoff_source = handoff_document.get("source", {})
    if (handoff_source.get("sha256") != source.get("sha256")
            or handoff_source.get("line_count") != source.get("line_count")):
        failures.append("wrong_handoff_source_snapshot")
    specification_hash = case["files_sha256"]["requirements.txt"]
    requirement_lines = case.get("requirement_line_count", 1)
    for identity in (report.get("specification", {}), handoff_document.get("specification", {})):
        if (identity.get("sha256") != specification_hash or identity.get("line_count") != requirement_lines
                or identity.get("hash_basis") != "original-bytes"):
            failures.append("wrong_requirement_snapshot")

    def selected_requirement(row):
        entry = row.get("relevant_requirement", {})
        if entry.get("status") != "selected":
            return False
        # Compact real handoffs encode SPEC references as ranges; explicit refs
        # are also supported. Both bind to the shared original-bytes identity.
        refs = entry.get("refs", [{"file": "SPEC", "range": bounds} for bounds in entry.get("ranges", [])])
        if not refs:
            failures.append("missing_requirement_citation")
            return False
        valid = all(type(ref) is dict and ref.get("file") == "SPEC"
                    and ref.get("source_sha256", specification_hash) == specification_hash
                    and type(ref.get("range")) is list and len(ref["range"]) == 2
                    and all(type(value) is int for value in ref["range"])
                    and 1 <= ref["range"][0] <= ref["range"][1] <= requirement_lines
                    for ref in refs)
        if not valid:
            failures.append("invalid_requirement_citation")
        return valid

    selected = {id(row): selected_requirement(row) for row in handoff}
    for bug in case["bugs"]:
        lo, hi = bug["allowed_region"]
        matches = [row for row in handoff if lo <= row["suspect"][0] <= bug["line"] <= row["suspect"][1] <= hi]
        if matches:
            covered += 1
            if not any(selected[id(row)] for row in matches):
                failures.append("missing_requirement_citation")
    if case["expected"] == "defect":
        if covered != len(case["bugs"]):
            failures.append("missed_or_unhelpfully_broad_defect")
        if status != "complete":
            failures.append("defect_case_incomplete")
        if case["citation"]:
            refs = [r for row in handoff for r in row.get("read_with", {}).get("refs", [])]
            if not any(r.get("file") == case["citation"] and
                       r.get("source_sha256") == case["files_sha256"][case["citation"]] for r in refs):
                failures.append("missing_actual_helper_citation")
    elif case["expected"] == "clear":
        if findings:
            failures.append("false_positive_on_corrected_program")
        if status != "complete":
            failures.append("correct_program_unresolved")
    else:
        active_codes = {i["code"] for i in report.get("issues", []) if i["affects_completion"]}
        if status != "incomplete" or not active_codes & {"context_unresolved", "claim_insufficient_evidence",
                                                       "claim_inconsistent_evidence_assessment", "claim_supported_unresolved"}:
            failures.append("missing_evidence_not_expressed")
        if findings:
            failures.append("missing_information_reported_as_defect")
    return {"passed": not failures, "failures": sorted(set(failures)), "status": status,
            "covered_defects": covered, "expected_defects": len(case["bugs"]), "findings": spans}


def assert_gate_discriminates(manifest):
    """Reject trivial hunters before accepting any real-model test result."""
    for behavior in ("always_clear", "always_flag_whole_file", "always_incomplete"):
        outcomes = []
        for case in manifest["cases"]:
            spans = [[1, case["line_count"]]] if behavior == "always_flag_whole_file" else []
            status = "incomplete" if behavior == "always_incomplete" else "complete"
            fake = {"source": {"sha256": case["files_sha256"][case["name"]], "line_count": case["line_count"]},
                    "status": status, "findings": [{"start_line": a, "end_line": b} for a, b in spans],
                    "handoff": {"findings": [{"suspect": s, "relevant_requirement": {"status": "selected", "ranges": [[1, 1]]},
                        "read_with": {"refs": ([{"file": case["citation"], "range": [1, 1],
                            "source_sha256": case["files_sha256"][case["citation"]]}] if case["citation"] else [])}}
                        for s in spans]},
                    "issues": [{"code": "context_unresolved", "affects_completion": True}]}
            fake["specification"] = {"sha256": case["files_sha256"]["requirements.txt"],
                                     "line_count": case.get("requirement_line_count", 1), "hash_basis": "original-bytes"}
            fake["handoff"].update(source=fake["source"], specification=fake["specification"])
            outcomes.append(judge_outcome(case, fake, 2 if status == "incomplete" else int(bool(spans))))
        if all(r["passed"] for r in outcomes):
            raise AssertionError("acceptance gate permits trivial hunter")


def command(python, root, case, output, policy, *, use_package_default=False):
    folder = root / "inputs" / case["id"]
    args = [str(python), "-I", "-m", "bug_hunter", str(folder / case["name"]),
            "--scope", case["scope"], "--spec-file", str(folder / "requirements.txt"),
            "--search-policy", policy,
            "--max-calls", str(case["max_calls"]), "--max-questions", "2048",
            "--run-deadline-seconds", "180", "--output-dir", str(output)]
    if not use_package_default:
        args += ["--choice-rounding-places", "2"]
    if case["scope"] == "project":
        args += ["--project-root", str(folder)]
    return args


def runtime_identity(python):
    code = ("import bug_hunter,hashlib,json; from bug_hunter.__main__ import parser; from pathlib import Path; "
            "p=Path(bug_hunter.__file__).parent; "
            "a=parser(); print(json.dumps({'version':bug_hunter.__version__,"
            "'effective_choice_rounding_places':None if a.get_default('strict_choice_mass') else a.get_default('choice_rounding_places'),"
            "'module_sha256':"
            "{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.iterdir()) "
            "if f.is_file() and f.suffix in {'.py','.json'}}}))")
    try:
        completed = subprocess.run([str(python), "-I", "-c", code],
                                   capture_output=True, check=True, timeout=30)
        identity = json.loads(completed.stdout)
        if type(identity["version"]) is not str or not identity["module_sha256"]:
            raise ValueError
        return identity
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise ValueError("runtime_probe_failed") from None


@contextmanager
def boundary_pause():
    """A console interrupt asks to stop after the current isolated CLI exits."""
    requested = [False]
    previous = signal.getsignal(signal.SIGINT)
    installed = False
    try:
        try:
            signal.signal(signal.SIGINT, lambda *_: requested.__setitem__(0, True))
            installed = True
        except ValueError:  # Library invocation outside the main thread.
            pass
        yield requested
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


def summarize(out, plan, manifest, results, budget, hold=None):
    complete = len(results) == len(plan["cases"])
    selected_passed = complete and all(row["passed"] for row in results)
    summary = {**plan, "cases": results, "planned_cases": plan["cases"],
               "passed_cases": sum(row["passed"] for row in results),
               "completed_cases": len(results), "suite_cases": len(manifest["cases"]),
               "selection_passed": selected_passed,
               "release_gate_passed": selected_passed and len(results) == len(manifest["cases"]),
               "campaign_calls_charged": budget["charged"],
               "paused_by_user": budget.get("paused_by_user", False), "hold": hold}
    save(out / "summary.json", summary)
    if hold:
        print(json.dumps({"hold": hold}), flush=True)
    return summary


def observe_case(case, folder, process, failure, wall):
    """Retain imperfect attempts as failures, with conservative observed charges."""
    errors, receipts = [], []
    paths = sorted(folder.glob("artifacts/run-*/report.json"))
    report = None
    if len(paths) == 1:
        try:
            report = read(paths[0])
            if type(report) is not dict:
                report = None
        except (OSError, ValueError):
            errors.append("unreadable_report")
    receipt_paths = sorted(folder.glob("artifacts/run-*/receipts/call_*.json"))
    for path in receipt_paths:
        try:
            receipt = read(path)
            if type(receipt) is not dict:
                raise ValueError
            receipts.append(receipt)
        except (OSError, ValueError):
            errors.append("unreadable_receipt")
    charged = len(receipt_paths)
    report_calls = report.get("calls_attempted") if report else None
    if type(report_calls) is int and report_calls >= 0:
        charged = max(charged, report_calls)
    elif report is not None:
        errors.append("invalid_report_call_count")
    unknown = (failure in {"case_timeout", "case_interrupted", "case_process_error"} or report is None
               or bool(errors) or any(row.get("status") not in {"validated", "failed"} for row in receipts)
               or (type(report_calls) is int and report_calls > len(receipt_paths))
               or charged > case["max_calls"])
    if any(receipt.get("status") == "failed" for receipt in receipts):
        errors.append("provider_receipt_failed")
    if type(report_calls) is int and report_calls != len(receipt_paths):
        errors.append("report_receipt_call_count_mismatch")
    exit_code = process.returncode if process is not None else None
    try:
        outcome = judge_outcome(case, report, exit_code)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        outcome = {"passed": False, "failures": ["malformed_report"], "status": "unknown",
                   "covered_defects": 0, "expected_defects": len(case["bugs"]), "findings": []}
    if report is not None and process is not None:
        try:
            if (read(paths[0].with_name("handoff.json")) != report["handoff"] or
                    paths[0].with_name("findings.txt").read_text(encoding="utf-8") !=
                    process.stdout.decode("utf-8").replace("\r\n", "\n")):
                errors.append("user_visible_artifacts_disagree")
        except (OSError, ValueError, KeyError):
            errors.append("user_visible_artifacts_missing")
    if failure:
        errors.append(failure)
    if unknown:
        errors.append("attempt_accounting_unsettled")
    if charged > case["max_calls"]:
        errors.append("reserved_call_limit_exceeded")
    outcome["failures"] = sorted(set(outcome["failures"] + errors))
    outcome["passed"] = not outcome["failures"]
    row = {"id": case["id"], "family": case["family"], "expected": case["expected"],
           **outcome, "exit_code": exit_code, "calls": charged,
           "elapsed_seconds": round(wall, 3), "usage": report.get("usage") if report else None,
           "exit_status_observed": process is not None, "unsettled": unknown}
    service_hold = unknown or bool(failure) or report is None or bool(errors) or "malformed_report" in outcome["failures"] or any(
        receipt.get("status") == "failed" for receipt in receipts)
    return row, service_hold


def settle_attempt(ledger, plan, case, attempt_id, row, pause_requested):
    with transaction(ledger) as budget:
        reservation = budget.get("reservation")
        if (budget.get("ledger_id") != plan["ledger_id"] or not reservation
                or reservation.get("attempt_id") != attempt_id):
            raise ValueError("campaign_reservation_changed")
        if any(attempt.get("attempt_id") == attempt_id for attempt in budget["attempts"]):
            raise ValueError("campaign_attempt_already_settled")
        budget["charged"] += row["calls"]
        budget["attempts"].append({"run": plan["run"], "run_id": plan["run_id"],
                                   "manifest_sha256": plan["manifest_sha256"], "case": case["id"],
                                   "attempt_id": attempt_id, "charged_calls": row["calls"],
                                   "unsettled": row["unsettled"], "result": row})
        if not row["unsettled"]:
            budget["reservation"] = None
        else:
            budget["reservation"]["already_charged_calls"] = row["calls"]
        if pause_requested:
            budget["paused_by_user"] = True
    return budget


def run_live(args):
    root = args.suite.resolve()
    manifest = load_suite(root)
    assert_gate_discriminates(manifest)
    if not args.live or not os.environ.get("TYPESAFE_API_KEY"):
        raise ValueError("explicit_live_flag_and_authorized_environment_key_required")
    if not args.run.isidentifier():
        raise ValueError("invalid_run_name")
    ledger = args.budget.resolve() if getattr(args, "budget", None) else root / "budget.json"
    with transaction(ledger) as budget:
        if budget.get("paused_by_user", False):
            print(json.dumps({"hold": "campaign_paused_by_user"}), flush=True)
            return 1
        if budget.get("reservation"):
            raise ValueError("unsettled_prior_campaign_reservation")
        if not budget.get("ledger_id"):
            raise ValueError("campaign_ledger_identity_required")
    out = root / "runs" / args.run
    if not out.resolve().is_relative_to(root / "runs"):
        raise ValueError("campaign_output_path_escape")
    resumed = getattr(args, "resume", False)
    prior_plan = read(out / "plan.json") if resumed else None
    ids = args.case if args.case else prior_plan["cases"] if prior_plan else None
    selected = [case for case in manifest["cases"] if ids is None or case["id"] in ids]
    if not selected or set(ids or []) - {case["id"] for case in selected}:
        raise ValueError("unknown_case")
    policy = args.policy or (prior_plan["policy"] if prior_plan else "scoped-v2-beta")
    identity = runtime_identity(args.python)
    use_default = getattr(args, "use_package_default", False)
    if use_default and identity.get("effective_choice_rounding_places") != manifest["rounding_places"]:
        raise ValueError("package_default_profile_does_not_match_frozen_suite")
    profile_mode = "package-default" if use_default else "explicit-two-places"
    manifest_hash = sha((root / "manifest.json").read_bytes())
    if resumed:
        plan = prior_plan
        if (plan.get("schema") != "jev-outcome-run-v2" or plan["manifest_sha256"] != manifest_hash
                or plan.get("outcome_gate_revision") != OUTCOME_GATE_REVISION
                or plan["runtime"] != identity or plan["policy"] != policy
                or plan.get("choice_profile_mode") != profile_mode
                or plan["cases"] != [case["id"] for case in selected]
                or plan["ledger_id"] != budget["ledger_id"]):
            raise ValueError("resume_plan_or_runtime_changed")
        previous = read(out / "summary.json") if (out / "summary.json").exists() else {}
        if previous.get("hold") == "service_receipt_or_report_failure" and not getattr(args, "continue_after_service_failure", False):
            raise ValueError("explicit_continuation_after_service_failure_required")
    else:
        out.mkdir(parents=True, exist_ok=False)
        plan = {"schema": "jev-outcome-run-v2", "run": args.run, "run_id": uuid.uuid4().hex,
                "outcome_gate_revision": OUTCOME_GATE_REVISION,
                "manifest_sha256": manifest_hash, "mode": "live", "runtime": identity,
                "package_version": identity["version"], "policy": policy,
                "choice_profile_mode": profile_mode,
                "cases": [case["id"] for case in selected], "ledger_id": budget["ledger_id"],
                "prior_campaign_calls": budget["charged"], "campaign_call_limit": budget["limit"]}
        save(out / "plan.json", plan)
    results = []
    attempts = [attempt for attempt in budget["attempts"] if attempt.get("run_id") == plan["run_id"]]
    journal = {attempt["case"]: attempt for attempt in attempts}
    if len(journal) != len(attempts):
        raise ValueError("duplicate_recorded_case")
    for case in selected:
        if case["id"] in journal:
            # The ledger atomically retains outcome + charge. A crash between
            # that write and the derived per-case result never causes a retry.
            row = journal[case["id"]].get("result")
            if type(row) is not dict:
                raise ValueError("recorded_attempt_needs_manual_audit")
            results.append(row)
            saved = out / case["id"] / "result.json"
            if saved.exists():
                if read(saved) != row:
                    raise ValueError("recorded_case_result_changed")
            else:
                save(saved, row)
        elif (out / case["id"]).exists():
            raise ValueError("unaccounted_existing_case")
    summarize(out, plan, manifest, results, budget)
    with boundary_pause() as requested:
        for case in selected:
            if case["id"] in journal:
                continue
            load_suite(root)
            hold = None
            with transaction(ledger) as budget:
                if requested[0]:
                    budget["paused_by_user"] = True
                if budget.get("paused_by_user", False):
                    hold = "campaign_paused_by_user"
                elif budget.get("ledger_id") != plan["ledger_id"]:
                    hold = "campaign_ledger_changed"
                elif budget.get("reservation"):
                    hold = "unsettled_prior_campaign_reservation"
                elif budget["charged"] + case["max_calls"] > budget["limit"]:
                    hold = "campaign_call_budget"
                else:
                    folder = out / case["id"]
                    folder.mkdir()
                    attempt_id = uuid.uuid4().hex
                    budget["reservation"] = {"run": args.run, "run_id": plan["run_id"],
                                              "attempt_id": attempt_id, "case": case["id"],
                                              "manifest_sha256": manifest_hash,
                                              "maximum_calls": case["max_calls"]}
            if hold:
                summarize(out, plan, manifest, results, budget, hold)
                return 1
            process, failure, stdout, stderr = None, None, b"", b""
            started = time.monotonic()
            isolation = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                         else {"start_new_session": True})
            try:
                process = subprocess.run(command(args.python, root, case, folder / "artifacts", policy,
                                                  use_package_default=use_default),
                                         cwd=folder, stdin=subprocess.DEVNULL, capture_output=True,
                                         timeout=240, **isolation)
                stdout, stderr = process.stdout, process.stderr
            except subprocess.TimeoutExpired as exc:
                failure, stdout, stderr = "case_timeout", exc.stdout or b"", exc.stderr or b""
            except KeyboardInterrupt:
                failure, requested[0] = "case_interrupted", True
            except OSError:
                # run() can fail during communication after a successful launch.
                # Without an observed exit, preserve the reservation for audit.
                failure = "case_process_error"
            row, service_hold = observe_case(case, folder, process, failure, time.monotonic() - started)
            try:
                load_suite(root)
            except (OSError, ValueError):
                row["passed"] = False
                row["failures"].append("frozen_fixture_changed_during_scan")
                service_hold = True
            budget = settle_attempt(ledger, plan, case, attempt_id, row, requested[0])
            (folder / "stdout.txt").write_bytes(stdout)
            (folder / "stderr.txt").write_bytes(stderr)
            save(folder / "result.json", row)
            results.append(row)
            hold = ("service_receipt_or_report_failure" if service_hold else
                    "campaign_paused_by_user" if budget.get("paused_by_user", False) else None)
            summary = summarize(out, plan, manifest, results, budget, hold)
            print(json.dumps(row), flush=True)
            if hold:
                return 1
    summary = summarize(out, plan, manifest, results, budget)
    return 0 if summary["selection_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--call-limit", type=int, default=446)
    prepare.add_argument("--predecessor", type=Path,
                         help="Record a prior frozen manifest without modifying or importing its results.")
    live = commands.add_parser("live")
    live.add_argument("--suite", type=Path, required=True)
    live.add_argument("--python", type=Path, required=True)
    live.add_argument("--run", required=True)
    live.add_argument("--policy", choices=("legacy-v1", "scoped-v2-beta"))
    live.add_argument("--case", action="append")
    live.add_argument("--live", action="store_true")
    live.add_argument("--use-package-default", action="store_true",
                      help="Omit the rounding override; require the installed CLI default to match the frozen profile.")
    live.add_argument("--budget", type=Path,
                      help="Shared charged-call ledger; defaults to the frozen suite's budget.json.")
    live.add_argument("--resume", action="store_true", help="Continue unstarted cases in the same frozen run.")
    live.add_argument("--continue-after-service-failure", action="store_true",
                      help="Explicitly continue to unstarted cases after a settled failure; never retries a case.")
    for action in ("pause", "unpause"):
        control = commands.add_parser(action)
        control.add_argument("--budget", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            if not 1 <= args.call_limit <= 10000:
                parser.error("call limit must be 1 through 10000")
            freeze(args.out.resolve(), args.call_limit,
                   predecessor=args.predecessor.resolve() if args.predecessor else None)
            return 0
        if args.command in {"pause", "unpause"}:
            print(json.dumps(set_paused(args.budget.resolve(), args.command == "pause")))
            return 0
        return run_live(args)
    except ValueError as exc:
        code = str(exc) if re.fullmatch(r"[a-z][a-z0-9_]+", str(exc)) else "campaign_record_invalid"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2
    except (OSError, KeyError, TypeError, AttributeError):
        print(json.dumps({"error": "campaign_io_or_record_error"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
