"""Whole-adapter durability smoke tests for the two-transition receipt journal.

The transport is finite synthetic data; receipt files, fsync, replacement and
abrupt subprocess exits are real. No provider calls or timing assertions.
"""
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bug_hunter import jev
from tests.support import legacy_exchange


KEY = "offline-receipt-fault-key"
STATE = {"source": "L1: def total(a, b):\nL2:     return a - b\n"}
META = {"window_id": "W1", "start_line": 1, "end_line": 2}


def response(answers=None):
    return json.dumps({"model": jev.MODEL,
        "answers": {"q": {"type": "noul", "noul": 0.82}}
        if answers is None else answers,
        "usage": {"input_tokens": 80, "output_tokens": 3}}).encode()


class ReceiptLifecycleSmokeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(legacy_exchange())
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY})
        environment.start()
        self.addCleanup(environment.stop)
        self.gateway = jev.HostedJev(Path(self.directory.name) / "receipts")
        self.completed = subprocess.CompletedProcess([], 0, response(), b"")

    def saved(self):
        return json.loads(Path(self.gateway.last_receipt).read_text())

    def assert_private(self):
        for path in self.gateway.receipt_dir.iterdir():
            text = path.read_text()
            self.assertNotIn(KEY, text)
            self.assertNotIn(STATE["source"], text)
            self.assertNotIn("def total", text)

    def test_success_orders_two_durable_transitions_around_transport(self):
        events = []
        real_sync, real_replace = jev.os.fsync, jev.os.replace

        def sync(fd):
            real_sync(fd)
            events.append("sync")

        def replace(source, destination):
            status = json.loads(Path(source).read_text())["status"]
            self.assertEqual(events[-1], "sync")
            real_replace(source, destination)
            events.append(status)

        def transport(*args, **kwargs):
            saved = self.saved()
            self.assertEqual(saved["status"], "dispatching")
            self.assertTrue(saved["transport_attempted"])
            self.assertEqual(saved["question_count"], 1)
            for field in ("request_sha256", "state_sha256", "question_sha256"):
                self.assertEqual(len(saved[field]), 64)
            self.assertEqual(saved["metadata"], META)
            self.assertEqual(events[-1], "sync" if os.name == "posix" else "dispatching")
            events.append("transport")
            return self.completed

        with patch.object(jev.os, "fsync", side_effect=sync), \
                patch.object(jev.os, "replace", side_effect=replace), \
                patch.object(jev.subprocess, "run", side_effect=transport):
            self.assertEqual(self.gateway.score(STATE, metadata=META), Decimal("0.82"))
        self.assertEqual([event for event in events if event != "sync"],
                         ["dispatching", "transport", "validated"])
        self.assertEqual(events.count("sync"), 4 if os.name == "posix" else 2)
        self.assertEqual(self.saved()["status"], "validated")
        self.assertEqual(len(list(self.gateway.receipt_dir.iterdir())), 1)
        self.assert_private()

    def test_rejected_preflight_has_one_terminal_receipt_and_no_transport(self):
        original = jev._write_receipt
        for state, metadata, code in (
                ({"source": "x" * 20000}, META, "request_too_large"),
                (STATE, {"unsafe": "field"}, "invalid_metadata"),
                ({"source": KEY}, META, "sensitive_input")):
            with self.subTest(code=code), \
                    patch.object(jev, "_write_receipt", wraps=original) as persist, \
                    patch.object(jev.subprocess, "run") as transport:
                with self.assertRaises(jev.JevError) as caught:
                    self.gateway.score(state, metadata=metadata)
                self.assertEqual(caught.exception.code, code)
                transport.assert_not_called()
                self.assertEqual(persist.call_count, 1)
                self.assertEqual(self.saved()["error"], code)
                self.assertFalse(self.saved()["transport_attempted"])
                self.assertIsNone(self.saved()["answers"])
                self.assert_private()

    def test_site_free_real_child_rejects_invalid_envelope_without_network(self):
        with patch.object(jev.subprocess, "run", return_value=self.completed) as transport:
            self.gateway.score(STATE, metadata=META)
        command = transport.call_args.args[0]
        self.assertEqual(command[1:3], ["-I", "-S"])
        completed = subprocess.run(command, input=b"[]", capture_output=True,
            env=transport.call_args.kwargs["env"], timeout=30, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr.strip(), b"invalid_request")

    def test_intent_faults_prevent_submission_and_record_failure_when_possible(self):
        for boundary in ("serialization", "fsync", "replace"):
            with self.subTest(boundary=boundary):
                original = jev._write_receipt

                def write(path, receipt):
                    if receipt["status"] == "dispatching":
                        target, attr = ((jev, "_json") if boundary == "serialization"
                            else (jev.os, boundary))
                        with patch.object(target, attr, side_effect=OSError("synthetic disk failure")):
                            original(path, receipt)
                    else:
                        original(path, receipt)

                with patch.object(jev, "_write_receipt", side_effect=write), \
                        patch.object(jev.subprocess, "run") as transport:
                    with self.assertRaises(jev.JevError) as caught:
                        self.gateway.score(STATE, metadata=META)
                self.assertEqual(caught.exception.code, "receipt_failed")
                transport.assert_not_called()
                self.assertEqual(self.saved()["status"], "failed")
                self.assertEqual(self.saved()["error"], "receipt_failed")
                self.assertIsNone(self.saved()["answers"])
                self.assert_private()

    def test_terminal_faults_never_release_valid_answers_or_clear_intent(self):
        for boundary in ("serialization", "fsync", "replace"):
            with self.subTest(boundary=boundary):
                original = jev._write_receipt

                def write(path, receipt):
                    if receipt["status"] == "validated":
                        target, attr = ((jev, "_json") if boundary == "serialization"
                            else (jev.os, boundary))
                        with patch.object(target, attr, side_effect=OSError("synthetic disk failure")):
                            original(path, receipt)
                    else:
                        original(path, receipt)

                with patch.object(jev, "_write_receipt", side_effect=write), \
                        patch.object(jev.subprocess, "run", return_value=self.completed) as transport:
                    with self.assertRaises(jev.JevError) as caught:
                        self.gateway.score(STATE, metadata=META)
                self.assertEqual(caught.exception.code, "receipt_failed")
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(self.saved()["status"], "dispatching")
                self.assertTrue(self.saved()["transport_attempted"])
                self.assertIsNone(self.saved()["answers"])
                self.assertTrue(self.gateway.last_call_stats["transport_attempted"])
                self.assertEqual(self.gateway.last_call_stats["usage"]["input_tokens"], 80)
                self.assert_private()

    def test_deadline_expiring_during_intent_sync_prevents_submission(self):
        self.gateway.set_run_deadline(time.monotonic() + 30)
        original = jev._write_receipt

        def write(path, receipt):
            original(path, receipt)
            if receipt["status"] == "dispatching":
                self.gateway.set_run_deadline(time.monotonic() - 1)

        with patch.object(jev, "_write_receipt", side_effect=write), \
                patch.object(jev.subprocess, "run") as transport:
            with self.assertRaises(jev.JevError) as caught:
                self.gateway.score(STATE, metadata=META)
        self.assertEqual(caught.exception.code, "run_deadline_exceeded")
        transport.assert_not_called()
        self.assertEqual(self.saved()["error"], "run_deadline_exceeded")
        self.assertTrue(self.saved()["transport_attempted"])

    def test_malformed_batch_retains_usage_and_fails_atomically(self):
        raw = response({"q": {"type": "noul", "noul": 0.9},
                        "other": {"type": "noul", "noul": 1.1}})
        completed = subprocess.CompletedProcess([], 0, raw, b"")
        with patch.object(jev.subprocess, "run", return_value=completed) as transport:
            with self.assertRaises(jev.JevError) as caught:
                self.gateway.evaluate(STATE, {"q": jev.QUESTION, "other": jev.QUESTION},
                                      metadata=META)
        self.assertEqual(caught.exception.code, "response_probability")
        self.assertEqual(transport.call_count, 1)
        saved = self.saved()
        self.assertEqual(saved["status"], "failed")
        self.assertIsNone(saved["answers"])
        self.assertEqual(saved["usage"], {"input_tokens": 80, "output_tokens": 3})

    def test_interruptions_are_settled_without_a_second_submission(self):
        original = jev._write_receipt
        for boundary in ("intent", "transport"):
            with self.subTest(boundary=boundary):
                def write(path, receipt):
                    original(path, receipt)
                    if receipt["status"] == "dispatching" and boundary == "intent":
                        raise KeyboardInterrupt

                with patch.object(jev, "_write_receipt", side_effect=write), \
                        patch.object(jev.subprocess, "run", side_effect=KeyboardInterrupt) as transport:
                    with self.assertRaises(KeyboardInterrupt):
                        self.gateway.score(STATE, metadata=META)
                self.assertEqual(transport.call_count, 0 if boundary == "intent" else 1)
                self.assertEqual(self.saved()["error"], "interrupted")
                self.assertTrue(self.saved()["transport_attempted"])

    @unittest.skipUnless(os.name == "posix", "directory durability uses POSIX fsync")
    def test_directory_sync_failure_never_releases_an_answer(self):
        real_sync = jev.os.fsync
        calls = 0

        def sync(fd):
            nonlocal calls
            calls += 1
            # Intent file+directory succeed; final file succeeds, directory fails.
            if calls == 4:
                raise OSError("synthetic directory sync failure")
            real_sync(fd)

        with patch.object(jev.os, "fsync", side_effect=sync), \
                patch.object(jev.subprocess, "run", return_value=self.completed):
            with self.assertRaises(jev.JevError) as caught:
                self.gateway.score(STATE, metadata=META)
        self.assertEqual(caught.exception.code, "receipt_failed")
        self.assertTrue(self.gateway.last_call_stats["transport_attempted"])

    def test_abrupt_process_exit_preserves_last_durable_outcome(self):
        script = r'''
import json, os, subprocess, sys
from pathlib import Path
from bug_hunter import jev
directory, boundary = sys.argv[1:]
gateway = jev.HostedJev(directory)
gateway._exchange = gateway._exchange_once
original = jev._write_receipt
def write(path, receipt):
    if receipt["status"] == "validated" and boundary == "terminal":
        os._exit(92)
    original(path, receipt)
    if receipt["status"] == "validated" and boundary == "after_terminal":
        os._exit(93)
def transport(*args, **kwargs):
    saved = json.loads(Path(gateway.last_receipt).read_text())
    assert saved["status"] == "dispatching" and saved["transport_attempted"]
    if boundary == "dispatch":
        os._exit(91)
    raw = json.dumps({"model": jev.MODEL,
        "answers": {"q": {"type": "noul", "noul": .82}},
        "usage": {"input_tokens": 80, "output_tokens": 3}}).encode()
    return subprocess.CompletedProcess([], 0, raw, b"")
jev._write_receipt = write
jev.subprocess.run = transport
gateway.score({"source": "synthetic crash evidence"}, metadata={"window_id": "W1"})
raise AssertionError("abrupt exit was not reached")
'''
        for boundary, code in (("dispatch", 91), ("terminal", 92), ("after_terminal", 93)):
            with self.subTest(boundary=boundary):
                directory = Path(self.directory.name) / boundary
                completed = subprocess.run([sys.executable, "-c", script,
                    str(directory), boundary], cwd=Path(jev.__file__).resolve().parent.parent,
                    capture_output=True, timeout=30, check=False)
                self.assertEqual(completed.returncode, code, completed.stderr)
                records = list(directory.glob("call_*.json"))
                self.assertEqual(len(records), 1)
                saved = json.loads(records[0].read_text())
                self.assertTrue(saved["transport_attempted"])
                if boundary == "after_terminal":
                    self.assertEqual(saved["status"], "validated")
                    self.assertEqual(saved["usage"]["input_tokens"], 80)
                    self.assertEqual(saved["answers"]["q"]["noul"], "0.82")
                    self.assertEqual(len(saved["response_sha256"]), 64)
                else:
                    self.assertEqual(saved["status"], "dispatching")
                    self.assertIsNone(saved["usage"])
                    self.assertIsNone(saved["answers"])
                    self.assertIsNone(saved["response_sha256"])


if __name__ == "__main__":
    unittest.main()
