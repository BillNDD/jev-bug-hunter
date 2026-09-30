"""Full parent adapter/CLI with real worker pipes and synthetic child HTTP."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bug_hunter import __main__ as cli, jev, transport
from bug_hunter.core import Config, Source, scan
from tests.test_transport_worker import KEY, OfflineWorker


STATE = {"evidence": "Synthetic evidence never submitted to the provider."}


class HostedWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.enterContext(patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY}))
        self.gateway = jev.HostedJev(self.root / "receipts")
        self.addCleanup(self.gateway.close)

    @contextmanager
    def workers(self, mode="http", *, cleanup_error=False):
        workers = []

        def construct(key):
            self.assertEqual(key, KEY)
            worker = OfflineWorker(mode)
            if cleanup_error:
                original = worker.close
                def fail_close():
                    original()  # This injected failure must not orphan a real test child.
                    raise jev.JevError("transport_failed")
                worker.close = fail_close
            workers.append(worker)
            return worker

        with patch.object(transport, "supports_worker", return_value=True), \
                patch.object(transport, "KillableWorkerTransport", side_effect=construct), \
                patch.object(jev.HostedJev, "_exchange_once",
                             side_effect=AssertionError("unexpected network fallback")) as fallback:
            try:
                yield workers
            finally:
                for worker in workers:
                    super(OfflineWorker, worker).close()
                fallback.assert_not_called()

    def receipt(self):
        return json.loads(Path(self.gateway.last_receipt).read_text())

    def test_default_parent_reuses_worker_with_exact_per_attempt_metadata(self):
        with self.workers() as workers:
            self.assertEqual(self.gateway.score(STATE, metadata={}), Decimal(".13"))
            first = self.receipt()
            self.assertEqual(self.gateway.score({"evidence": "second synthetic view"}, metadata={}),
                             Decimal(".13"))
            second = self.receipt()
            self.assertEqual(len(workers), 1)
            self.assertEqual((first["worker_sequence"], second["worker_sequence"]), (1, 2))
            self.assertEqual((first["worker_reused"], second["worker_reused"]), (False, True))
            self.assertNotEqual(first["request_sha256"], second["request_sha256"])
            self.assertEqual(second["usage"], {"input_tokens": 2, "output_tokens": 2})
            child = workers[0]._child
            self.gateway.close()
            self.assertTrue(transport._wait_os_process(child.process, 0))
        for file in (self.root / "receipts").iterdir():
            saved = file.read_text()
            self.assertNotIn(KEY, saved)
            self.assertNotIn(STATE["evidence"], saved)
            self.assertNotIn(str(self.root), saved)

    def test_semantic_batch_failure_retires_worker_and_preserves_usage(self):
        choice = {"type": "choice", "instructions": "Choose a category.",
                  "criteria": {"a": "A", "b": "B"}}
        with self.workers("wrong_answer_type") as workers:
            with self.assertRaises(jev.JevError) as caught:
                self.gateway.evaluate(STATE, {"q": jev.QUESTION, "other": choice}, metadata={})
            self.assertEqual(caught.exception.code, "response_schema")
            self.assertIsNone(workers[0]._child)
            self.assertIsNone(self.receipt()["answers"])
            self.assertEqual(self.receipt()["usage"], {"input_tokens": 1, "output_tokens": 1})
            self.gateway.score({"evidence": "a different requested view"}, metadata={})
            self.assertEqual(self.receipt()["worker_generation"], 2)
            self.assertEqual(self.receipt()["worker_sequence"], 1)

    def test_terminal_persistence_failure_retires_actual_worker_without_releasing_answer(self):
        original = jev._write_receipt
        children = []
        with self.workers() as workers:
            def persist(path, record):
                if record["status"] == "validated":
                    children.append(workers[0]._child)
                    raise jev.JevError("receipt_failed")
                original(path, record)
            with patch.object(jev, "_write_receipt", side_effect=persist):
                with self.assertRaises(jev.JevError) as caught:
                    self.gateway.score(STATE, metadata={})
            self.assertEqual(caught.exception.code, "receipt_failed")
            self.assertEqual(self.receipt()["status"], "dispatching")
            self.assertIsNone(self.receipt()["answers"])
            self.assertEqual(self.gateway.last_call_stats["usage"]["input_tokens"], 1)
            self.assertIsNone(workers[0]._child)
            self.assertTrue(transport._wait_os_process(children[0].process, 0))

    def test_empty_body_error_matches_legacy_and_retires_worker(self):
        with self.workers("empty_http") as workers:
            with self.assertRaises(jev.JevError) as caught:
                self.gateway.score(STATE, metadata={})
            self.assertEqual(caught.exception.code, "response_invalid_json")
            self.assertIsNone(workers[0]._child)
            self.assertEqual(self.receipt()["response_sha256"], hashlib.sha256(b"").hexdigest())
            self.assertIsNone(self.receipt()["usage"])

    def test_unsupported_runtime_selects_legacy_before_any_worker_and_never_switches(self):
        raw = json.dumps({"model": jev.MODEL, "answers": {"q": {"type": "noul", "noul": .13}},
                          "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        with patch.object(transport, "supports_worker", return_value=False) as supported, \
                patch.object(transport, "KillableWorkerTransport") as create, \
                patch.object(self.gateway, "_exchange_once", return_value=(raw, ())) as once:
            self.gateway.score(STATE, metadata={})
            supported.return_value = True
            self.gateway.score(STATE, metadata={})
            self.assertEqual(once.call_count, 2)
            self.assertEqual(supported.call_count, 1)
            create.assert_not_called()

    def test_concurrent_rejection_does_not_expose_another_callers_receipt_or_usage(self):
        entered, release = threading.Event(), threading.Event()
        completed = []
        raw = json.dumps({"model": jev.MODEL, "answers": {"q": {"type": "noul", "noul": .13}},
                          "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()

        def exchange(wire, timeout):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test caller did not release exchange")
            return raw, ()

        def caller():
            answer = self.gateway.score(STATE, metadata={})
            completed.append((answer, self.gateway.last_receipt, self.gateway.last_call_stats))

        with patch.object(self.gateway, "_exchange", side_effect=exchange):
            thread = threading.Thread(target=caller)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(jev.JevError) as caught:
                    self.gateway.score(STATE, metadata={})
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertIsNone(self.gateway.last_receipt)
                self.assertIsNone(self.gateway.last_call_stats)
            finally:
                release.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(completed[0][0], Decimal(".13"))
        self.assertIsNotNone(completed[0][1])
        self.assertEqual(completed[0][2]["usage"]["input_tokens"], 1)
        self.assertIsNone(self.gateway.last_call_stats)

    def test_library_scan_borrows_gateway_and_context_owns_cleanup(self):
        config = Config(width=2, min_width=1, whole_file=False, bug_lenses=False,
                        max_depth=0, max_action_steps=0, max_localizations=0)
        with self.workers() as workers:
            with self.gateway:
                for _ in range(2):
                    result = scan(Source.from_text("synthetic\n", "fixture.txt"), self.gateway,
                                  config=config, context_scope="declared_standalone")
                    self.assertEqual(result["status"], "complete")
                    self.assertEqual(result["issues"], [])
                    self.assertEqual(result["findings"], [])
                    self.assertTrue(result["coverage"]["all_lines_assessed"])
                    self.assertEqual(result["questions_validated"], result["questions_attempted"])
                    self.assertEqual(result["requests"][0]["answers"]["rank_0"]["type"], "choice")
                    self.assertFalse(self.gateway._closed)
                self.assertEqual(len(workers), 1)
                child = workers[0]._child
            self.assertTrue(self.gateway._closed)
            self.assertTrue(transport._wait_os_process(child.process, 0))

    def run_cli(self, *, cleanup_error=False, scan_error=None):
        target = self.root / "target.txt"
        target.write_text("synthetic\n", encoding="utf-8")
        output = self.root / "runs"
        stdout, stderr = io.StringIO(), io.StringIO()
        original = jev.HostedJev.close
        closed = []
        def close(gateway):
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(list(output.glob("run-*/report.json")))
            closed.append(gateway)
            return original(gateway)
        with self.workers(cleanup_error=cleanup_error) as workers, \
                patch.object(jev.HostedJev, "close", new=close), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            args = [str(target), "--scope", "standalone", "--output-dir", str(output),
                    "--no-whole-file", "--no-bug-lenses", "--width", "2", "--min-width", "1",
                    "--max-depth", "0", "--max-action-steps", "0", "--max-localizations", "0"]
            if scan_error is None:
                code = cli.main(args)
            else:
                with patch.object(cli, "scan", side_effect=scan_error):
                    code = cli.main(args)
            self.assertEqual(len(closed), 1)
            for worker in workers:
                self.assertIsNone(worker._child)
        reports = list(output.glob("run-*/report.json"))
        report = json.loads(reports[0].read_text()) if reports else None
        return code, stdout.getvalue(), stderr.getvalue(), report

    def test_real_cli_closes_actual_worker_before_complete_output(self):
        code, stdout, stderr, report = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("SCAN: complete", stdout)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["coverage"]["all_lines_assessed"])
        self.assertEqual(report["questions_validated"], report["questions_attempted"])
        self.assertEqual(report["usage"]["attempts_with_known_usage"], 1)
        self.assertNotIn(KEY, stdout + stderr + json.dumps(report))

    def test_cli_cleanup_failure_preserves_usage_and_reports_incomplete(self):
        code, stdout, stderr, report = self.run_cli(cleanup_error=True)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["handoff"]["status"], "incomplete")
        self.assertEqual(report["usage"]["input_tokens"], 1)
        self.assertEqual(report["usage"]["attempts_with_known_usage"], 1)
        self.assertEqual(report["questions_validated"], report["questions_attempted"])
        self.assertEqual({issue["phase"] for issue in report["issues"]}, {"transport-cleanup"})
        self.assertIn("Transport cleanup failed (transport_failed)", stderr)
        self.assertNotIn("SCAN: complete", stdout)
        self.assertNotIn(KEY, stdout + stderr + json.dumps(report))

    def test_cli_scan_exception_still_closes_owned_gateway(self):
        code, stdout, stderr, report = self.run_cli(scan_error=KeyboardInterrupt())
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("interrupted", stderr)
        self.assertIsNone(report)


if __name__ == "__main__":
    unittest.main()
