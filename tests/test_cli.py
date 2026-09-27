"""Offline CLI contract tests: no target execution and no live Jev calls."""

from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bug_hunter import __main__ as cli
from bug_hunter.jev import JevError
from bug_hunter.handoff import compact_report


SENTINEL = "offline-sentinel-credential-never-send"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "sample.any"
        self.source.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        self.output = self.root / "results"
        self.environment = patch.dict(os.environ, {"TYPESAFE_API_KEY": SENTINEL})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.gateway = Mock()
        self.gateway.check_sensitive.side_effect = cli.check_sensitive
        self.gateway_constructor = patch.object(
            cli, "HostedJev", return_value=self.gateway,
        )
        self.construct = self.gateway_constructor.start()
        self.addCleanup(self.gateway_constructor.stop)

    def report(self, *, found=False, status="complete"):
        findings = []
        if found:
            findings.append({
                "start_line": 1, "end_line": 3, "score": "0.90",
                "resolution": "minimum_width", "window_id": "w0001",
                "finding_id": "F1", "assessment": "recheck-supported",
                "read_with": {"status":"not-identified", "ranges":[], "additional_count":0},
                "relevant_requirement": {"status":"not-supplied", "ranges":[], "additional_count":0},
                "unresolved_parents": [], "limitations": [],
            })
        result = {
            "schema_version": 4, "source": {"name": "sample.any", "sha256": "a"*64},
            "specification": None, "scan_status": status, "handoff_status": "complete",
            "configuration": {}, "specification_mode": "inferred",
            "status": status, "calls_attempted": 1,
            "findings": findings, "windows": [],
            "coverage": {"assessed_lines": 3},
            "issues": [],
        }
        result["handoff"] = compact_report(result)
        return result

    def run_cli(self, *extra, report=None, error=None, scope="standalone"):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [str(self.source), "--output-dir", str(self.output),
                "--output-format", "ranges", "--scope", scope, *extra]
        with patch.object(cli, "scan") as scan:
            scan.return_value = self.report() if report is None else report
            scan.side_effect = error
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), scan

    def artifacts(self):
        return list(self.output.glob("run-*/report.json"))

    def test_complete_findings_have_exact_stdout_and_exit_one(self):
        code, out, err, _ = self.run_cli(report=self.report(found=True))
        self.assertEqual(code, 1)
        self.assertEqual(out, "Bug suspected between lines 1 and 3.\n")
        self.assertIn("Scan complete; 1 suspected region(s);", err)
        self.assertIn("1 call(s) attempted; 3/3 source lines assessed.", err)
        self.assertIn("Intent is inferred", err)
        self.assertNotIn("DONE", out + err)
        path = self.artifacts()[0]
        self.assertEqual(path.with_name("ranges.txt").read_text(), out)
        saved = json.loads(path.read_text())
        self.assertNotIn("reason", saved["findings"][0])
        self.assertNotIn("bug_type", saved["findings"][0])

    def test_complete_without_findings_has_empty_stdout_and_exit_zero(self):
        code, out, err, _ = self.run_cli()
        self.assertEqual((code, out), (0, ""))
        self.assertIn("0 suspected region(s)", err)
        path = self.artifacts()[0].with_name("ranges.txt")
        self.assertEqual(path.read_bytes(), b"")

    def test_incomplete_keeps_findings_but_exits_two(self):
        report = self.report(found=True, status="incomplete")
        code, out, err, _ = self.run_cli(report=report)
        self.assertEqual(code, 2)
        self.assertEqual(out, "Bug suspected between lines 1 and 3.\n")
        self.assertIn("Scan incomplete", err)

    def test_unique_runs_do_not_overwrite_source_or_reports(self):
        original = self.source.read_bytes()
        self.run_cli()
        first = self.artifacts()[0]
        first_bytes = first.read_bytes()
        self.run_cli(report=self.report(found=True))
        self.assertEqual(len(self.artifacts()), 2)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(self.source.read_bytes(), original)

    def test_optional_spec_is_normalized_and_passed_to_scan(self):
        spec = self.root / "spec.txt"
        spec.write_bytes(b"Accept text.\r\nRetain line numbers.\r\n")
        code, _, err, scan = self.run_cli("--spec-file", str(spec))
        self.assertEqual(code, 0)
        self.assertEqual(
            scan.call_args.kwargs["spec"].lines,
            ("Accept text.", "Retain line numbers."),
        )
        self.assertNotIn("Intent is inferred", err)

    def test_spec_has_an_explicit_byte_limit(self):
        spec = self.root / "spec.txt"
        spec.write_bytes(b"x" * 8193)
        code, out, _, scan = self.run_cli("--spec-file", str(spec))
        self.assertEqual((code, out), (2, ""))
        scan.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unicode_bom_and_source_lines_remain_available(self):
        self.source.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma\r\n")
        code, _, _, scan = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(scan.call_args.args[0].lines, ("alpha", "beta", "gamma"))

    def test_all_search_options_reach_config(self):
        code, _, _, scan = self.run_cli(
            "--width", "32", "--min-width", "8", "--max-depth", "3",
            "--context-lines", "6", "--max-calls", "120",
            "--drill-threshold", "0.65", "--report-threshold", "0.85",
            "--relation-threshold", "0.72", "--max-action-steps", "5",
            "--max-action-targets", "11", "--evidence-min-width", "6",
            "--evidence-beam-width", "2", "--evidence-max-depth", "3",
            "--max-evidence-candidates", "70", "--max-relationships", "3",
            "--no-bug-lenses", "--no-whole-file", "--deadline-seconds", "12.5",
        )
        self.assertEqual(code, 0)
        config = scan.call_args.kwargs["config"]
        self.assertEqual((config.width, config.min_width), (32, 8))
        self.assertEqual((config.max_depth, config.context_lines), (3, 6))
        self.assertEqual(config.max_calls, 120)
        self.assertEqual(config.drill_threshold, Decimal("0.65"))
        self.assertEqual(config.report_threshold, Decimal("0.85"))
        self.assertEqual(config.relation_threshold, Decimal("0.72"))
        self.assertEqual((config.max_action_steps, config.max_action_targets), (5, 11))
        self.assertEqual((config.evidence_min_width, config.evidence_beam_width,
                          config.evidence_max_depth), (6, 2, 3))
        self.assertEqual(config.max_evidence_candidates, 70)
        self.assertEqual(config.max_relationships, 3)
        self.assertFalse(config.bug_lenses)
        self.assertFalse(config.whole_file)
        self.assertEqual(self.construct.call_args.kwargs["deadline_seconds"], 12.5)

    def test_project_root_is_indexed_and_passed_to_scan(self):
        helper = self.root / "helper.py"
        helper.write_text("helper\n", encoding="utf-8")
        code, _, _, scan = self.run_cli("--project-root", str(self.root),
                                        "--max-project-files", "8",
                                        "--max-project-candidates", "9",
                                        scope="project")
        self.assertEqual(code, 0)
        project = scan.call_args.kwargs["project"]
        self.assertIsNotNone(project)
        self.assertIn("helper.py", [entry.relpath for entry in project.entries])
        self.assertEqual(scan.call_args.kwargs["config"].max_project_candidates, 9)

    def test_active_credential_anywhere_in_source_is_rejected_before_writes(self):
        self.source.write_text("safe\n" * 200 + SENTINEL, encoding="utf-8")
        code, out, err, scan = self.run_cli()
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(SENTINEL, err)
        self.assertIn("sensitive_input", err)
        scan.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_active_credential_in_spec_is_rejected_before_writes(self):
        spec = self.root / "spec.txt"
        spec.write_text(SENTINEL, encoding="utf-8")
        code, out, err, scan = self.run_cli("--spec-file", str(spec))
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(SENTINEL, err)
        scan.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_active_credential_in_output_path_is_not_printed_or_created(self):
        self.output = self.root / SENTINEL
        code, out, err, scan = self.run_cli()
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(SENTINEL, err)
        scan.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_reflected_credential_in_report_is_not_persisted(self):
        report = self.report()
        report["source"]["name"] = SENTINEL
        code, out, err, _ = self.run_cli(report=report)
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(SENTINEL, err)
        self.assertEqual(self.artifacts(), [])

    def test_gateway_failure_never_becomes_a_finding(self):
        code, out, err, _ = self.run_cli(error=JevError("transport_failed"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("transport_failed", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.artifacts(), [])

    def test_arbitrary_exception_text_is_not_exposed(self):
        code, out, err, _ = self.run_cli(error=RuntimeError(SENTINEL))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unexpected_error", err)
        self.assertNotIn(SENTINEL, err)

    def test_invalid_encoding_and_binary_content_fail_safely(self):
        for raw in (b"\xff\xfe\xfa", b"alpha\x00beta"):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                code, out, err, scan = self.run_cli()
                self.assertEqual((code, out), (2, ""))
                self.assertNotIn("Traceback", err)
                scan.assert_not_called()

    def test_missing_source_does_not_echo_a_secret_in_its_path(self):
        self.source = self.root / SENTINEL
        code, out, err, scan = self.run_cli()
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(SENTINEL, err)
        scan.assert_not_called()

    def test_missing_key_is_a_safe_constructor_failure(self):
        self.construct.side_effect = JevError("missing_api_key")
        code, out, err, scan = self.run_cli()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("missing_api_key", err)
        self.assertNotIn("Traceback", err)
        scan.assert_not_called()

    def test_failed_report_write_does_not_print_findings(self):
        with patch.object(cli, "write_new", side_effect=OSError(SENTINEL)):
            code, out, err, _ = self.run_cli(report=self.report(found=True))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("file_io_error", err)
        self.assertNotIn(SENTINEL, err)

    def test_bad_numeric_argument_does_not_echo_its_value(self):
        for flag, value in (("--width", SENTINEL),
                            ("--report-threshold", SENTINEL),
                            ("--report-threshold", "NaN")):
            with self.subTest(flag=flag, value=value):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as exit:
                    cli.main([str(self.source), flag, value])
                self.assertEqual(exit.exception.code, 2)
                self.assertNotIn(SENTINEL, err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())

    def test_import_does_not_construct_gateway_read_files_or_call_api(self):
        environment = dict(os.environ)
        environment.pop("TYPESAFE_API_KEY", None)
        environment["PYTHONPATH"] = str(Path(cli.__file__).resolve().parents[1])
        import_dir = self.root / "import-only"
        import_dir.mkdir()
        process = subprocess.run(
            [sys.executable, "-c", "import bug_hunter.__main__"],
            cwd=import_dir, env=environment, capture_output=True, timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertEqual((process.stdout, process.stderr), (b"", b""))
        self.assertEqual(list(import_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
