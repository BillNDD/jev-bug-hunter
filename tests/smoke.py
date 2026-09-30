"""Run the real CLI/scanner/validator/receipt path with synthetic HTTP output.

Run from the source root: python -m tests.smoke --out smoke-results
No API key or network is needed. This is NOT a live Jev accuracy test.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import tempfile
from unittest.mock import patch

from bug_hunter import __main__ as cli, jev, __version__
from bug_hunter.core import Span
from bug_hunter.handoff import format_handoff
from tests.support import FixtureProvider, KEY, legacy_exchange


def run_case(root, name, provider, *, n=96, extra=(), expected_exit=0,
             expected_ranges=(), required_ranges=(), process=None, long_line=False,
             source_text=None, spec_text=None, check=None, project_files=None):
    folder = root / name
    folder.mkdir()
    source = folder / "fixture.txt"
    text = ("\u5b57" * 10000 if long_line else
            "\n".join(f"Synthetic fixture line {i}; no live inference." for i in range(1,n+1)))
    if source_text is not None:
        text = source_text.rstrip("\n")
    source.write_text(text + "\n", encoding="utf-8")
    original = source.read_bytes()
    out, err = io.StringIO(), io.StringIO()
    argv = [str(source), "--no-whole-file", "--output-dir", str(folder / "runs"),
            "--scope", "project" if project_files else "standalone", *extra]
    spec_path = folder / "requirements.txt"
    if spec_text is not None:
        spec_path.write_bytes(b"\xef\xbb\xbf" + spec_text.replace("\n", "\r\n").encode())
        argv += ["--spec-file", str(spec_path)]
    if project_files:
        for relpath, body in project_files.items():
            item = folder / relpath
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text(body, encoding="utf-8")
        argv += ["--project-root", str(folder)]
    # A real network attempt fails this harness instead of silently proceeding.
    with legacy_exchange(), patch.dict(os.environ,{"TYPESAFE_API_KEY":KEY}), \
         patch.object(socket,"create_connection",side_effect=AssertionError("network forbidden")), \
         patch.object(jev.http.client,"HTTPSConnection",side_effect=AssertionError("network forbidden")), \
         patch.object(jev.subprocess,"run",side_effect=process or provider.process), \
         redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    if code != expected_exit:
        raise AssertionError((name,"exit",code,expected_exit,err.getvalue()))
    reports = list((folder / "runs").glob("run-*/report.json"))
    if len(reports) != 1:
        raise AssertionError((name,"missing report",err.getvalue()))
    report = json.loads(reports[0].read_text())
    found = {(f["start_line"],f["end_line"]) for f in report["findings"]}
    if expected_ranges is not None and found != set(expected_ranges):
        raise AssertionError((name,"findings",found,expected_ranges))
    if not set(required_ranges) <= found:
        raise AssertionError((name,"missing required ranges",found,required_ranges))
    wanted = format_handoff(report["handoff"])
    legacy = "".join(f"Bug suspected between lines {a} and {b}.\n" for a,b in sorted(found))
    if reports[0].with_name("ranges.txt").read_text() != legacy:
        raise AssertionError((name,"legacy output contract"))
    if json.loads(reports[0].with_name("handoff.json").read_text()) != report["handoff"]:
        raise AssertionError((name,"compact JSON contract"))
    if any(x in wanted for x in ("Synthetic fixture line", '"noul"', '"score"')):
        raise AssertionError((name,"verbose output leaked"))
    if out.getvalue() != wanted or reports[0].with_name("findings.txt").read_text() != wanted:
        raise AssertionError((name,"output contract"))
    if len(wanted.splitlines()) > 4 + max(2,4*len(found)):
        raise AssertionError((name,"compact line budget"))
    if check is not None:
        check(report, wanted)
    if source.read_bytes() != original:
        raise AssertionError((name,"source modified"))
    receipts = list(reports[0].parent.joinpath("receipts").glob("*.json"))
    if len(receipts) != report["calls_attempted"]:
        raise AssertionError((name,"receipt count"))
    for receipt_path in receipts:
        saved = receipt_path.read_text()
        if KEY in saved or "Synthetic fixture line" in saved:
            raise AssertionError((name,"receipt leaks raw content"))
        receipt = json.loads(saved)
        if receipt["status"] not in ("validated","failed"):
            raise AssertionError((name,"incomplete receipt"))
    (folder / "stdout.txt").write_text(out.getvalue(),encoding="utf-8")
    # Normalize the generated working-directory prefix for portable transcripts.
    stderr = err.getvalue().replace(str(root),"<SMOKE_ROOT>")
    (folder / "stderr.txt").write_text(stderr,encoding="utf-8")
    if provider.requests:
        first=provider.requests[0]
        (folder / "first_request.json").write_bytes(
            jev.encode_request(first["state"],first["questions"]))
    return {"name":name,"pass":True,"exit_code":code,"status":report["status"],
            "findings":[list(p) for p in sorted(found)],
            "requests_attempted":report["calls_attempted"],
            "questions_attempted":report["questions_attempted"],
            "receipt_count":len(receipts),"coverage":report["coverage"],
            "source_sha256":hashlib.sha256(original).hexdigest(),
            "synthetic_only":True, "stdout_lines":len(wanted.splitlines()),
            "handoff_status":report["handoff_status"],
            "scan_status":report["scan_status"]}


def run_cold_parent_case(out):
    def cold_parent(qid,q,state,target):
        if qid.startswith(("screen_", "lens_")) and target.size>12:
            return {"type":"noul","noul":0.05}
    def check(report, text):
        parent = next(w for w in report["windows"] if w["window_id"] == "L1-48")
        if parent["score"] != "0.05":
            raise AssertionError("cold-parent smoke fixture is not actually cold")
    return run_case(out,"cold_parent_hot_child",FixtureProvider([(17,18)],override=cold_parent),
                    n=48,expected_exit=1,expected_ranges=[(17,18)],check=check)


def run(out):
    out.mkdir(parents=True,exist_ok=False)
    cases=[]
    cases.append(run_case(out,"cold_multiscale",FixtureProvider(),n=48))
    cases.append(run_case(out,"two_disjoint_findings",FixtureProvider([(17,18),(64,65)]),
                          expected_exit=1,expected_ranges=[(17,18),(64,65)]))
    cases.append(run_cold_parent_case(out))
    cases.append(run_case(out,"offset_boundary",FixtureProvider([(48,49)]),
                          expected_exit=1,expected_ranges=[(48,49)]))
    cases.append(run_case(out,"context_retrieval",FixtureProvider([(5,5)],need_packet=100),
                          n=120,expected_exit=1,expected_ranges=[(5,5)]))
    cases.append(run_case(out,"call_budget",FixtureProvider([(17,18),(64,65)]),
                          extra=["--max-calls","1"],expected_exit=2,
                          expected_ranges=None,required_ranges=[(1,48)]))
    cases.append(run_case(out,"question_budget",FixtureProvider(),n=48,
                          extra=["--max-questions","12"],expected_exit=2))
    def malformed(*args,**kwargs):
        request=json.loads(kwargs["input"])
        provider=FixtureProvider()
        data=json.loads(provider.raw(request["state"],request["questions"]))
        data["diagnostics"]={"truncated":1}
        return subprocess.CompletedProcess(args[0],0,json.dumps(data).encode(),b"")
    cases.append(run_case(out,"malformed_response",FixtureProvider(),n=48,
                          process=malformed,expected_exit=2))
    def timeout(*args,**kwargs):
        raise subprocess.TimeoutExpired(args[0],kwargs["timeout"])
    cases.append(run_case(out,"transport_timeout",FixtureProvider(),n=48,
                          process=timeout,expected_exit=2))
    cases.append(run_case(out,"oversized_single_line",FixtureProvider(),n=1,
                          long_line=True,expected_exit=2))
    # New handoff cases use preassigned references, not a real semantic model.
    from tests.test_handoff import FIXTURE, SPEC, selected_references
    def exact_refs(report, text):
        row=report["handoff"]["findings"][0]
        assert row["read_with"]["ranges"] == [[1,2]]
        assert row["relevant_requirement"]["ranges"] == [[5,6]]
        assert "read-with: 1-2 | relevant-requirement: SPEC:5-6" in text
    cases.append(run_case(out,"compact_cited_handoff",
        FixtureProvider([(9,9)],override=selected_references),n=12,
        source_text=FIXTURE,spec_text=SPEC,expected_exit=1,
        expected_ranges=[(9,9)],check=exact_refs))
    def conflicting(qid,q,state,target):
        if qid.startswith("verify_") and target == Span(1,12):
            return {"type":"noul","noul":0.05}
    def conflict_check(report,text):
        rows={tuple(f["suspect"]):f for f in report["handoff"]["findings"]}
        assert rows[(1,12)]["assessment"] == "conflicting"
        assert rows[(9,9)]["unresolved_parents"] == [[1,12]]
    cases.append(run_case(out,"compact_conflicting_parent",
        FixtureProvider([(9,9)],override=conflicting),n=12,
        source_text=FIXTURE,expected_exit=1,
        expected_ranges=[(1,12),(9,9)],check=conflict_check))
    def bad_ref(qid,q,state,target):
        if qid.startswith(("read_with_","requirement_")):
            return {"type":"noul","noul":"0.95"}
    def incomplete_refs(report,text):
        assert report["scan_status"] == "complete"
        assert report["handoff_status"] == "incomplete"
        assert "read-with: unknown" in text
    cases.append(run_case(out,"compact_malformed_references",
        FixtureProvider([(9,9)],override=bad_ref),n=12,
        source_text=FIXTURE,spec_text=SPEC,expected_exit=2,
        expected_ranges=[(9,9)],check=incomplete_refs))
    def no_refs(report,text):
        row=report["handoff"]["findings"][0]
        assert row["relevant_requirement"]["status"] == "not-identified"
    cases.append(run_case(out,"compact_requirement_abstention",
        FixtureProvider([(9,9)]),n=12,source_text=FIXTURE,spec_text=SPEC,
        expected_exit=1,expected_ranges=[(9,9)],check=no_refs))
    def capped_refs(report,text):
        assert report["scan_status"] == "complete"
        assert report["handoff_status"] == "incomplete"
        assert "reference-pool-limited" in text
    cases.append(run_case(out,"compact_reference_pool_cap",
        FixtureProvider([(9,9)],override=selected_references),n=12,
        source_text=FIXTURE,spec_text=SPEC,extra=["--max-handoff-candidates","1"],
        expected_exit=2,expected_ranges=[(9,9)],check=capped_refs))

    # v0.4 Jev-maximizing cases: lenses, action selection, hierarchical evidence,
    # cross-region relationships, and repository-wide evidence supply.
    def lens_only(qid,q,state,target):
        if qid.startswith("screen_"):
            return {"type":"noul","noul":0.05}
    cases.append(run_case(out,"v04_bug_lens_signal",
        FixtureProvider([(9,9)],override=lens_only),n=12,
        expected_exit=1,expected_ranges=[(9,9)]))

    action_steps={}
    def source_relation(qid,q,state,target):
        if qid.startswith(("screen_","lens_")):
            return {"type":"noul","noul":0.65 if target==Span(1,12) else 0.05}
        if qid.startswith("verify_") and not qid.startswith("verify_evidence_"):
            return {"type":"noul","noul":0.05}
        if qid=="continue":
            return {"type":"noul","noul":0.95 if target==Span(1,12) else 0.05}
        if qid=="action":
            n=action_steps.get(target,0)
            wanted=["search_file","test_relationship","finish"]
            choice=wanted[min(n,2)] if target==Span(1,12) else "finish"
            action_steps[target]=n+1
            if choice not in q["criteria"]: choice="finish"
            return {"type":"choice","choice":choice,"confidence":1,
                    "probabilities":{k:int(k==choice) for k in q["criteria"]}}
        if qid.startswith("evidence_"):
            visible={row["line"] for e in state.get("excerpts",[]) for row in e["lines"]}
            return {"type":"noul","noul":0.95 if 100 in visible else 0.05}
        if qid.startswith(("relation_","verify_evidence_")):
            visible={row["line"] for e in state.get("excerpts",[]) for row in e["lines"]}
            return {"type":"noul","noul":0.95 if 100 in visible else 0.05}
    def source_relation_check(report,text):
        # The contrary direct recheck blocks relationship promotion: the
        # relationship is tested and recorded, but no suspicion is raised.
        assert any(r["verify_score"]=="0.95" for r in report["relationships"])
        assert any(a["action"]=="search_file" for a in report["jev_actions"])
        assert any(a["action"]=="test_relationship" for a in report["jev_actions"])
        assert "read-with:" not in text
    cases.append(run_case(out,"v04_hierarchical_relation",
        FixtureProvider(override=source_relation),n=120,
        extra=["--max-action-targets","4","--max-action-steps","3",
               "--max-evidence-candidates","40"],
        expected_exit=0,expected_ranges=[],check=source_relation_check))

    project_steps={}
    def project_relation(qid,q,state,target):
        if qid.startswith(("screen_","lens_")):
            return {"type":"noul","noul":0.65 if target==Span(1,12) else 0.05}
        if qid.startswith("verify_") and not qid.startswith("verify_evidence_"):
            return {"type":"noul","noul":0.95}
        if qid=="continue":
            return {"type":"noul","noul":0.95 if target==Span(1,12) else 0.05}
        if qid=="action":
            n=project_steps.get(target,0)
            wanted=["search_project","test_relationship","finish"]
            choice=wanted[min(n,2)] if target==Span(1,12) else "finish"
            project_steps[target]=n+1
            if choice not in q["criteria"]: choice="finish"
            return {"type":"choice","choice":choice,"confidence":1,
                    "probabilities":{k:int(k==choice) for k in q["criteria"]}}
        related=state.get("related_sources",[])
        hit=any(e["file"]=="helper.py" and e["start_line"]<=20<=e["end_line"] for e in related)
        if qid.startswith(("evidence_","relation_","verify_evidence_")):
            return {"type":"noul","noul":0.95 if hit else 0.05}
    def project_relation_check(report,text):
        row=next(f for f in report["handoff"]["findings"] if f["suspect"]==[1,12])
        assert any(ref.get("file")=="helper.py" for ref in row["read_with"].get("refs",[]))
        assert report["project"]["files_loaded"] >= 1
    helper="\n".join(f"helper evidence line {i}" for i in range(1,41))+"\n"
    cases.append(run_case(out,"v04_repository_relation",
        FixtureProvider(override=project_relation),n=24,
        project_files={"helper.py":helper},
        extra=["--max-action-targets","4","--max-action-steps","3",
               "--max-evidence-candidates","40"],
        expected_exit=1,expected_ranges=[(1,12)],check=project_relation_check))

    summary={"tool_version":__version__,"python":platform.python_version(),
             "platform":platform.system(),"live_api_calls":0,
             "synthetic_only":True,"cases_passed":len(cases),"cases":cases}
    (out / "summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path)
    args=parser.parse_args()
    if args.out is None:
        root=Path(tempfile.mkdtemp(prefix="jev-smoke-")) / "results"
    else:
        root=args.out.resolve()
    run(root)


if __name__=="__main__":
    main()
