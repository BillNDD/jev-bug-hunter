"""Compare local CLI overhead with identical finite, no-network judgments.

Run from any directory (stdlib only)::

    python tools/benchmark_runtime.py --baseline BASE --candidate CANDIDATE \
        --output RESULTS --samples 5 --warm-runs 2

Both paths are source checkouts. The driver alternates fresh worker processes;
each worker reports its first CLI invocation and subsequent warm invocations.
"Cold" means process-cold, not flushed filesystem or operating-system caches.
The real HostedJev, canonical encoding, validation, durable receipt writes,
source indexing and final artifacts run. Only the private transport exchange
is replaced with deterministic responses (a subprocess-result fallback supports
older checkouts). This measures local execution, not Jev accuracy,
HTTP/TLS performance, or absolute production E2E latency. Optional isolated
jev.py startup probes perform no request. Profiling is a separate untimed run.

Optional --live-evidence points to the developer smoke layout (plan.json,
CASE/unit.py, CASE/requirements.txt, CASE/runs/run-*/{report.json,receipts/}).
Those exact request hashes and recorded answers are replayed through HostedJev;
the benchmark output contains hashes/metadata only, never fixture source,
specification, host paths or credentials. No live call is possible here.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import cProfile
from decimal import Decimal
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pstats
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch


SYNTHETIC_KEY = "offline-runtime-benchmark-not-a-provider-credential"
DEFAULT_CASES = ("small-clean", "wide-clean", "hot-local", "scoped-hot",
                 "project-clean", "privacy-large")
REPORT_VOLATILE = {"run_id", "started_at_utc", "finished_at_utc", "tool_version"}
RECEIPT_VOLATILE = {"elapsed_seconds", "transport_elapsed_seconds",
                    "transport_timeout_seconds", "validation_elapsed_seconds",
                    "receipt_elapsed_seconds", "receipt_write_count"}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def normalized_report(report):
    return {key: value for key, value in report.items() if key not in REPORT_VOLATILE}


def numeric_json(value):
    """Serialize recorded Decimal tokens without binary-float conversion."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite recorded probability")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + numeric_json(v)
                              for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(numeric_json, value)) + "]"
    return json.dumps(value, allow_nan=False)


def recorded_response(receipt, model):
    answers = json.loads(json.dumps(receipt["answers"]))
    for answer in answers.values():
        for key in ("noul", "confidence"):
            if key in answer:
                answer[key] = Decimal(answer[key])
        if "probabilities" in answer:
            answer["probabilities"] = {key: Decimal(value)
                                       for key, value in answer["probabilities"].items()}
    return numeric_json({"model": model, "answers": answers,
                         "usage": receipt["usage"]}).encode()


def spans(text):
    return [tuple(map(int, item)) for item in re.findall(r"L(\d+)-(\d+)", text)]


class FiniteProvider:
    """Preassigned routing judgments; never a substitute for a bug detector."""

    def __init__(self, model, bugs=(), recorded=()):
        self.model, self.bugs = model, tuple(bugs)
        self.recorded = list(recorded)
        self.requests = []
        self.elapsed = 0.0
        self.transport_flags = []

    def contains(self, target):
        return any(target[0] <= start <= end <= target[1] for start, end in self.bugs)

    def answer(self, qid, question):
        parsed = spans(question["instructions"])
        target = parsed[0] if parsed else (0, 0)
        hot = self.contains(target)
        if question["type"] == "choice":
            options = [key for key in question["criteria"]
                       if spans(key) and self.contains(spans(key)[0])]
            if qid == "where":
                options = [key for key in options if spans(key)[0] in self.bugs]
            chosen = ("finish" if qid == "action" else
                      min(options, key=lambda key: (spans(key)[0][1] - spans(key)[0][0], key))
                      if options else "none")
            if chosen not in question["criteria"]:
                raise AssertionError("fixture has no valid abstention")
            return {"type": "choice", "choice": chosen, "confidence": 1,
                    "probabilities": {key: int(key == chosen) for key in question["criteria"]}}
        if qid == "supports_failure":
            value = 0.95 if hot else 0.05
        elif qid == "demonstrates_refutation":
            value = 0.05 if hot else 0.95
        elif qid == "missing_material_evidence":
            value = 0.05
        elif qid == "covered":
            relevant = [b for b in self.bugs if target[0] <= b[0] <= b[1] <= target[1]]
            value = 0.95 if relevant and all(any(c[0] <= b[0] <= b[1] <= c[1]
                            for c in parsed[1:]) for b in relevant) else 0.05
        elif qid in {"continue", "need_context"} or qid.startswith((
                "read_with_", "requirement_", "action_requirement_", "evidence_",
                "relation_", "verify_evidence_", "relevance_")):
            value = 0.05
        else:
            value = 0.95 if hot else 0.05
        return {"type": "noul", "noul": value}

    def process(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            if "--transport-child" not in args[0]:
                raise AssertionError("unexpected child invocation")
            flags = [item for item in args[0] if item.startswith("-")]
            if flags not in self.transport_flags:
                self.transport_flags.append(flags)
            wire = kwargs["input"]
            key = hashlib.sha256(wire).hexdigest()
            index = len(self.requests)
            self.requests.append(key)
            request = json.loads(wire)
            if self.recorded:
                if index >= len(self.recorded) or key != self.recorded[index]["request_sha256"]:
                    raise AssertionError("recorded request sequence changed")
                raw = recorded_response(self.recorded[index], self.model)
            else:
                raw = numeric_json({"model": self.model,
                    "answers": {qid: self.answer(qid, q) for qid, q in request["questions"].items()},
                    "usage": {"input_tokens": 100, "output_tokens": len(request["questions"])}}).encode()
            return subprocess.CompletedProcess(args[0], 0, raw, b"")
        finally:
            self.elapsed += time.perf_counter() - started


def synthetic_case(folder, case):
    n = {"small-clean": 12, "wide-clean": 192, "hot-local": 96,
         "scoped-hot": 12, "project-clean": 48, "privacy-large": 50000}[case]
    width = 318 if case == "privacy-large" else 40
    text = "\n".join(f"Synthetic fixture {line:06d} ".ljust(width, "x") for line in range(1, n + 1)) + "\n"
    (folder / "unit.txt").write_bytes(text.encode("utf-8"))
    (folder / "requirements.txt").write_bytes(b"Assess only the supplied synthetic text.\n")
    bugs = ((17, 18), (64, 65)) if case == "hot-local" else ((9, 9),) if case == "scoped-hot" else ()
    scope = "project" if case == "project-clean" else "standalone"
    if scope == "project":
        for index in range(64):
            (folder / f"helper_{index:03d}.txt").write_bytes(b"Synthetic helper evidence.\n" * 24)
    args = [str(folder / "unit.txt"), "--scope", scope, "--no-whole-file",
            "--spec-file", str(folder / "requirements.txt"), "--max-calls", "256",
            "--max-questions", "8192", "--run-deadline-seconds", "3600"]
    if case in {"scoped-hot", "project-clean"}:
        args += ["--search-policy", "scoped-v2-beta"]
    if scope == "project":
        args += ["--project-root", str(folder)]
    if case == "privacy-large":
        args += ["--max-windows", "8", "--max-calls", "1"]
    return args, bugs, [], None


def replay_case(folder, case, evidence):
    case_id = case.removeprefix("live-")
    plan = json.loads((evidence / "plan.json").read_text(encoding="utf-8"))
    entry = next(item for item in plan["cases"] if item["id"] == case_id)
    original_folder = evidence / case_id
    reports = list(original_folder.glob("runs/run-*/report.json"))
    if len(reports) != 1:
        raise AssertionError("expected one original report")
    original = json.loads(reports[0].read_text(encoding="utf-8"))
    by_key = {}
    for path in reports[0].parent.joinpath("receipts").glob("call_*.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt["status"] != "validated" or receipt["request_sha256"] in by_key:
            raise AssertionError("replay requires unique settled receipts")
        by_key[receipt["request_sha256"]] = receipt
    recorded = [by_key[row["request_sha256"]] for row in original["requests"]]
    if len(recorded) != len(by_key):
        raise AssertionError("receipt/request cardinality mismatch")
    for name in ("unit.py", "requirements.txt", "prices.py"):
        if (original_folder / name).is_file():
            shutil.copyfile(original_folder / name, folder / name)
    args = [str(folder / "unit.py"), "--scope", entry["scope"],
            "--spec-file", str(folder / "requirements.txt"), "--search-policy", entry["policy"],
            "--choice-rounding-places", "2", "--max-calls", "24", "--max-questions", "512",
            "--run-deadline-seconds", "120"]
    if entry["scope"] == "project":
        args += ["--project-root", str(folder)]
    return args, (), recorded, original


def profile_category(filename, name):
    path = filename.replace("\\", "/")
    base = path.rsplit("/", 1)[-1]
    if name == "_write_receipt" or "fsync" in name or name == "<built-in method nt.replace>":
        return "receipt_write_and_durability"
    if "/json/" in path or "_json" in name or "json.loads" in name:
        return "json_serialization_and_parsing"
    if base == "copy.py" or name in {"_snapshot", "_snapshot_value"}:
        return "copies_and_snapshots"
    if base == "fractions.py" or (base == "jev.py" and (
            name.startswith("validate_") or name in {"_probability", "_distribution", "_decimal"})):
        return "validation_and_exact_probability_math"
    if base == "repository.py" or (base == "core.py" and name in {"read", "physical_lines"}):
        return "source_read_and_project_inventory"
    if base in {"questions.py", "evidence.py"}:
        return "evidence_rendering_and_questions"
    if base == "handoff.py":
        return "handoff"
    if base in {"engine.py", "claim_search.py", "claims.py", "core.py"}:
        return "search_claims_and_geometry"
    if base == "benchmark_runtime.py":
        return "benchmark_and_synthetic_provider"
    if base == "jev.py" and name in {"_secret_in", "_key", "check_sensitive"}:
        return "privacy_checks"
    return "runtime_and_other"


def summarize_profile(profiler, repo):
    stats = pstats.Stats(profiler)
    categories = defaultdict(float)
    top = []
    for (filename, line, name), (primitive, total, self_time, cumulative, callers) in stats.stats.items():
        categories[profile_category(filename, name)] += self_time
        path = Path(filename)
        try:
            label = path.relative_to(repo).as_posix()
        except ValueError:
            label = path.name
        top.append({"function": f"{label}:{line}:{name}", "calls": total,
                    "self_seconds": self_time, "inclusive_seconds": cumulative})
    return {"nonoverlapping_self_seconds": dict(sorted(categories.items())),
            "top_by_self_time": sorted(top, key=lambda item: -item["self_seconds"])[:30],
            "top_by_inclusive_time": sorted(top, key=lambda item: -item["inclusive_seconds"])[:30],
            "inclusive_rows_overlap": True}


def implementation_manifest(repo):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((repo / "bug_hunter").iterdir()) if path.is_file()}


def peak_resident_bytes():
    """Process-lifetime high water, including harness/imports; not CLI-only."""
    if os.name != "nt":
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == "darwin" else value * 1024
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = wintypes.HANDLE
    read = ctypes.windll.psapi.GetProcessMemoryInfo
    read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not read(process(), ctypes.byref(counters), counters.cb):
        return None
    return counters.PeakWorkingSetSize


def memory_stress():
    """Incremental peak allocations for preparation, separate from timings.

    Inputs already exist before tracing. A one-question budget exercises the
    actual eager planning path without paying for thousands of judgments.
    """
    from bug_hunter.core import Config, Source, Span, multiscale
    from bug_hunter.engine import Search
    from bug_hunter import jev, questions

    class FiniteGateway:
        def evaluate(self, state, batch, *, metadata):
            return {qid: {"type": "noul", "noul": Decimal("0.05")}
                    for qid in batch}

    source = Source("fixture.txt", hashlib.sha256(b"\n" * 4096).hexdigest(),
                    tuple("" for _ in range(4096)), "utf-8")
    pack, _ = questions.load_pack()
    lo, hi = 1, 4096
    while lo < hi:
        middle = (lo + hi + 1) // 2
        region = Span(1, middle)
        state = questions.state_for(source, region, 0, None, context_scope="declared_standalone")
        try:
            jev.encode_request(state, {"probe": questions.noul(pack, "screen", region)})
            lo = middle
        except jev.JevError:
            hi = middle - 1
    region = Span(1, lo)
    geometry, _ = multiscale(region, 1, 16, 200000)
    state = questions.state_for(source, region, 0, None, context_scope="declared_standalone")
    wide_questions = questions.screening(pack, region, [item[0] for item in geometry], state=state)
    library_questions = {f"q{index}": questions.noul(pack, "screen", Span(1, 1))
                         for index in range(10000)}
    result = []
    for name, supplied_state, supplied_questions in (
            ("largest_valid_empty_text_root", state, wide_questions),
            ("ten_thousand_library_questions", {"context_scope": "declared_standalone"}, library_questions)):
        wires = []
        total_wire_bytes = 0
        for batch, failure in questions.pack_questions(supplied_state, supplied_questions):
            if failure:
                wires.append("failed:" + failure)
            else:
                wire = jev.encode_request(supplied_state, batch)
                total_wire_bytes += len(wire)
                wires.append(hashlib.sha256(wire).hexdigest())
        search = Search(source, FiniteGateway(), Config(max_questions=1), None)
        gc.collect()
        tracemalloc.start()
        answer = search.ask(supplied_state, supplied_questions, region, "memory-benchmark")
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result.append({"case": name, "root_lines": lo if supplied_state is state else None,
            "questions": len(supplied_questions), "planned_batches": len(wires),
            "total_wire_bytes": total_wire_bytes, "ordered_wire_hashes_sha256": digest(wires),
            "peak_incremental_traced_bytes": peak, "retained_incremental_traced_bytes": retained,
            "result_sha256": digest({"answer": jev.plain(answer), "issues": search.issues,
                                      "trace": search.trace}), "new_provider_calls": 0})
    return result


def run_cli_case(cli, jev, repo, work, case, evidence, *, profile=False):
    folder = Path(tempfile.mkdtemp(prefix="fixture-", dir=work))
    argv, bugs, recorded, original = (replay_case(folder, case, evidence)
        if case.startswith("live-") else synthetic_case(folder, case))
    argv += ["--output-dir", str(folder / "runs")]
    provider = FiniteProvider(jev.MODEL, bugs, recorded)
    stdout, stderr = io.StringIO(), io.StringIO()
    receipt_samples, snapshots, artifact_samples = [], [], []
    write_receipt = jev._write_receipt
    write_new = cli.write_new

    def timed_receipt(path, receipt):
        start = time.perf_counter()
        try:
            return write_receipt(path, receipt)
        finally:
            receipt_samples.append(time.perf_counter() - start)
            snapshots.append(receipt.get("status"))

    def timed_artifact(path, text):
        start = time.perf_counter()
        try:
            return write_new(path, text)
        finally:
            artifact_samples.append(time.perf_counter() - start)

    def finite_exchange(_gateway, wire, timeout):
        # The current adapter can use a persistent child. Inject at its private
        # exchange boundary so a new default cannot bypass the finite provider.
        result = provider.process(["--transport-child"], input=wire, timeout=timeout)
        return result.stdout, ()

    profiler = cProfile.Profile() if profile else None
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"TYPESAFE_API_KEY": SYNTHETIC_KEY}))
        stack.enter_context(patch("socket.socket", side_effect=AssertionError("network forbidden")))
        stack.enter_context(patch.object(jev.subprocess, "run", provider.process))
        stack.enter_context(patch.object(jev.subprocess, "Popen",
                                        side_effect=AssertionError("unplanned child forbidden")))
        if hasattr(jev.HostedJev, "_exchange"):
            stack.enter_context(patch.object(jev.HostedJev, "_exchange", finite_exchange))
        stack.enter_context(patch.object(jev, "_write_receipt", timed_receipt))
        stack.enter_context(patch.object(cli, "write_new", timed_artifact))
        stack.enter_context(redirect_stdout(stdout))
        stack.enter_context(redirect_stderr(stderr))
        started, cpu_started = time.perf_counter(), time.process_time()
        if profiler:
            profiler.enable()
        code = cli.main(argv)
        if profiler:
            profiler.disable()
        cpu = time.process_time() - cpu_started
        elapsed = time.perf_counter() - started
    reports = list((folder / "runs").glob("run-*/report.json"))
    if len(reports) != 1:
        raise AssertionError("CLI failed to produce one report")
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    if recorded and (len(provider.requests) != len(recorded)
                     or normalized_report(report) != normalized_report(original)):
        raise AssertionError("live replay changed requests or report")
    if not recorded:
        expected_status = "incomplete" if case == "privacy-large" else "complete"
        expected_findings = ({(17, 18), (64, 65)} if case == "hot-local" else
                             {(1, 12), (9, 9)} if case == "scoped-hot" else set())
        actual_findings = {(row["start_line"], row["end_line"]) for row in report["findings"]}
        if report["status"] != expected_status or actual_findings != expected_findings:
            raise AssertionError("synthetic scenario did not reach its declared outcome")
    receipts = [json.loads(path.read_text(encoding="utf-8"))
                for path in reports[0].parent.joinpath("receipts").glob("call_*.json")]
    receipts.sort(key=lambda item: item["metadata"]["sequence"])
    if len(receipts) != len(provider.requests) or any(item["status"] != "validated" for item in receipts):
        raise AssertionError("benchmark requires all finite responses to validate")
    normalized_receipts = [{key: value for key, value in item.items() if key not in RECEIPT_VOLATILE}
                           for item in receipts]
    result = {"case": case, "exit_code": code, "status": report["status"],
              "calls": report["calls_attempted"], "questions": report["questions_attempted"],
              "findings": [[f["start_line"], f["end_line"]] for f in report["findings"]],
              "unknown_usage_attempts": report["usage"]["attempts_with_unknown_usage"],
              "request_hashes": provider.requests,
              "mocked_transport_command_flags": provider.transport_flags,
              "transport_replacement": ("private-exchange-hook" if hasattr(jev.HostedJev, "_exchange")
                                        else "single-shot-subprocess-result"),
              "report_sha256": digest(normalized_report(report)),
              "stdout_sha256": hashlib.sha256(stdout.getvalue().encode()).hexdigest(),
              "stderr_sha256": hashlib.sha256(stderr.getvalue().replace(report["run_id"], "<RUN>").encode()).hexdigest(),
              "terminal_receipts_sha256": digest(normalized_receipts),
              "wall_seconds": elapsed, "parent_cpu_seconds": cpu,
              "fake_provider_seconds": provider.elapsed,
              "wall_excluding_fake_provider_seconds": max(0.0, elapsed - provider.elapsed),
              "durable_receipt_seconds": sum(receipt_samples),
              "durable_receipt_writes": len(receipt_samples),
              "terminal_artifact_write_seconds": sum(artifact_samples),
              "terminal_artifact_bytes": sum(path.stat().st_size for path in reports[0].parent.iterdir()
                                              if path.is_file()),
              "receipt_states_written": {state: snapshots.count(state) for state in sorted(set(snapshots))},
              "source_bytes": (folder / ("unit.py" if case.startswith("live-") else "unit.txt")).stat().st_size,
              "project_files": report["project"]["files_loaded"] if report["project"] else 0}
    if profiler:
        result["profile"] = summarize_profile(profiler, repo)
    if folder.resolve() == work.resolve() or not folder.resolve().is_relative_to(work.resolve()):
        raise AssertionError("temporary fixture escaped benchmark workspace")
    shutil.rmtree(folder)
    return result


def worker(args):
    repo = Path(args.repo).resolve()
    manifest = implementation_manifest(repo)
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    # A fresh snapshot must not lose to a baseline with existing checkout pyc.
    # Force identical source-compilation policy for project-module imports.
    sys.pycache_prefix = str(work / "unused-bytecode-cache")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo))
    started = time.perf_counter()
    from bug_hunter import __main__ as cli, jev
    import_seconds = time.perf_counter() - started
    evidence = Path(args.live_evidence).resolve() if args.live_evidence else None
    cases = args.cases.split(",")
    rows = []
    started = time.perf_counter()
    for case in cases:
        for iteration in range(args.warm_runs + 1):
            row = run_cli_case(cli, jev, repo, work, case, evidence)
            row["iteration"] = iteration
            row["process_cold"] = not rows
            row["wall_including_initial_import_seconds"] = row["wall_seconds"] + (import_seconds if not rows else 0)
            rows.append(row)
    profiles = [run_cli_case(cli, jev, repo, work, case, evidence, profile=True)
                for case in cases] if args.profile else []
    memory = memory_stress() if args.memory else []
    # No arguments means jev.py exits before reading stdin or attempting HTTPS.
    # This includes real isolated interpreter/import startup, with no shim.
    startup = []
    for index in range(args.startup_probes + 1 if args.startup_probes else 0):
        child_start = time.perf_counter()
        completed = subprocess.run([sys.executable, "-I", str(repo / "bug_hunter" / "jev.py")],
                                   capture_output=True, check=False, timeout=30,
                                   env={key: value for key, value in os.environ.items()
                                        if key in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}})
        if index:
            startup.append(time.perf_counter() - child_start)
        if completed.returncode != 2 or completed.stdout or completed.stderr:
            raise AssertionError("isolated import probe changed")
    if manifest != implementation_manifest(repo):
        raise AssertionError("implementation changed during benchmark worker")
    print(json.dumps({"import_seconds": import_seconds,
                      "worker_seconds": time.perf_counter() - started + import_seconds,
                      "rows": rows, "profiles": profiles, "isolated_adapter_startup_seconds": startup,
                      "implementation_sha256": manifest, "peak_resident_bytes": peak_resident_bytes(),
                      "preparation_memory_stress": memory},
                     separators=(",", ":")))


def summarize(samples):
    if not samples:
        return None
    return {"median": statistics.median(samples), "min": min(samples), "max": max(samples),
            "samples": samples}


def compare(baseline, candidate):
    required = ("exit_code", "status", "calls", "questions", "findings", "unknown_usage_attempts",
                "request_hashes", "report_sha256", "stdout_sha256", "stderr_sha256", "terminal_receipts_sha256")
    return {key: baseline[key] == candidate[key] for key in required}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--warm-runs", type=int, default=1)
    parser.add_argument("--cases", default=",".join(DEFAULT_CASES))
    parser.add_argument("--live-evidence")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--memory", action="store_true", help="Separate preparation-memory stress probe")
    parser.add_argument("--startup-probes", type=int, default=0,
                        help="Optional isolated-adapter startup samples per worker; each also has one discarded warmup")
    parser.add_argument("--start-order", choices=("baseline", "candidate"), default="baseline",
                        help="First variant in alternating paired order")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", help=argparse.SUPPRESS)
    parser.add_argument("--work", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return 0
    if (not args.baseline or not args.candidate or not args.output or args.samples < 1
            or args.warm_runs < 0 or args.startup_probes < 0):
        parser.error("baseline, candidate, output and positive sample count are required")
    args.output.mkdir(parents=True, exist_ok=False)
    results = {"baseline": [], "candidate": []}
    for trial in range(args.samples):
        names = (args.start_order, "candidate" if args.start_order == "baseline" else "baseline")
        if trial % 2:
            names = tuple(reversed(names))
        for case in args.cases.split(","):
            for name in names:
                command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker",
                           "--repo", str(Path(getattr(args, name)).resolve()),
                           "--work", str(args.output / "work"), "--cases", case,
                           "--warm-runs", str(args.warm_runs), "--startup-probes", str(args.startup_probes)]
                if args.live_evidence:
                    command += ["--live-evidence", str(Path(args.live_evidence).resolve())]
                if args.profile and trial == 0:
                    command += ["--profile"]
                if args.memory and trial == 0 and case == args.cases.split(",")[0]:
                    command += ["--memory"]
                started = time.perf_counter()
                completed = subprocess.run(command, capture_output=True, timeout=3600, check=False)
                elapsed = time.perf_counter() - started
                if completed.returncode != 0:
                    # Omit arbitrary paths or exception text from public output.
                    raise RuntimeError(f"benchmark worker failed: {name}, trial {trial}, case {case}")
                result = json.loads(completed.stdout)
                if results[name] and result["implementation_sha256"] != results[name][0]["implementation_sha256"]:
                    raise RuntimeError(f"implementation changed between benchmark workers: {name}")
                result["process_wall_seconds"] = elapsed
                result["trial"] = trial
                results[name].append(result)
                (args.output / f"{name}-{trial:02d}-{case}.json").write_bytes(canonical(result) + b"\n")
                print(json.dumps({"variant": name, "trial": trial, "case": case,
                                  "process_seconds": round(elapsed, 3)}), flush=True)
    comparisons = []
    all_equivalent = True
    for case in args.cases.split(","):
        for iteration in range(args.warm_runs + 1):
            rows = {name: [row for trial in trials for row in trial["rows"]
                           if row["case"] == case and row["iteration"] == iteration]
                    for name, trials in results.items()}
            equivalent = all(all(compare(rows["baseline"][0], row).values())
                             for group in rows.values() for row in group)
            mismatched = sorted({key for group in rows.values() for row in group
                                 for key, same in compare(rows["baseline"][0], row).items() if not same})
            all_equivalent &= equivalent
            metrics = {field: {name: summarize([row[field] for row in group])
                               for name, group in rows.items()}
                       for field in ("wall_seconds", "parent_cpu_seconds", "fake_provider_seconds",
                                     "wall_excluding_fake_provider_seconds", "durable_receipt_seconds",
                                     "terminal_artifact_write_seconds", "wall_including_initial_import_seconds")}
            old = metrics["wall_excluding_fake_provider_seconds"]["baseline"]["median"]
            new = metrics["wall_excluding_fake_provider_seconds"]["candidate"]["median"]
            comparisons.append({"case": case, "iteration": iteration,
                "identical_requests_reports_stdout_receipts": equivalent,
                "mismatched_fields": mismatched,
                "median_local_wall_speed_ratio": old / new, "metrics": metrics,
                "paired_local_wall_speed_ratios": [a["wall_excluding_fake_provider_seconds"] /
                    b["wall_excluding_fake_provider_seconds"] for a, b in zip(rows["baseline"], rows["candidate"])],
                "calls": rows["baseline"][0]["calls"], "questions": rows["baseline"][0]["questions"],
                "receipt_writes": {name: group[0]["durable_receipt_writes"] for name, group in rows.items()}})
    memory_rows = {name: [row for trial in trials for row in trial["preparation_memory_stress"]]
                   for name, trials in results.items()}
    if any(len(rows) != (2 if args.memory else 0) for rows in memory_rows.values()):
        raise AssertionError("missing or duplicate preparation-memory case")
    memory_comparisons = []
    for old, new in zip(memory_rows["baseline"], memory_rows["candidate"]):
        equivalent = all(old[key] == new[key] for key in (
            "case", "questions", "planned_batches", "total_wire_bytes",
            "ordered_wire_hashes_sha256", "result_sha256"))
        all_equivalent &= equivalent
        memory_comparisons.append({"case": old["case"], "equivalent": equivalent,
            "baseline_peak_bytes": old["peak_incremental_traced_bytes"],
            "candidate_peak_bytes": new["peak_incremental_traced_bytes"],
            "peak_delta_bytes": new["peak_incremental_traced_bytes"] - old["peak_incremental_traced_bytes"]})
    summary = {"kind": "paired-no-network-local-cli-overhead", "python": platform.python_version(),
               "platform": platform.system(), "samples": args.samples, "warm_runs": args.warm_runs,
               "provider_calls": 0, "durability_enabled": True,
               "cold_definition": "fresh Python process; OS and filesystem caches are uncontrolled",
               "project_import_bytecode_policy": "existing checkout pyc ignored; source compiled equally; no cache written",
               "import_timing_basis": "incremental package import after benchmark stdlib imports; not bare CLI startup",
               "process_wall_basis": "whole benchmark worker including fixtures, readback, cleanup, startup probes and optional profiles",
               "report_ignored_root_fields": sorted(REPORT_VOLATILE),
               "receipt_ignored_fields": sorted(RECEIPT_VOLATILE),
               "synthetic_provider_time_measured_separately": True,
               "profile_runs_excluded_from_speed_metrics": True,
               "all_equivalent": all_equivalent, "comparisons": comparisons,
               "implementation_sha256": {name: trials[0]["implementation_sha256"]
                                         for name, trials in results.items()},
               "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "peak_resident_bytes_including_harness": {name: [r["peak_resident_bytes"] for r in trials]
                                                        for name, trials in results.items()},
               "preparation_memory_stress": memory_rows,
               "preparation_memory_comparisons": memory_comparisons,
               "import_seconds": {name: summarize([r["import_seconds"] for r in trials])
                                  for name, trials in results.items()},
               "isolated_adapter_startup_seconds": {name: summarize([value for trial in trials
                   for value in trial["isolated_adapter_startup_seconds"]]) for name, trials in results.items()}}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"all_equivalent": all_equivalent,
                      "cases": [{key: row[key] for key in ("case", "iteration", "median_local_wall_speed_ratio")}
                                for row in comparisons]}), flush=True)
    return 0 if all_equivalent else 1


if __name__ == "__main__":
    raise SystemExit(main())
