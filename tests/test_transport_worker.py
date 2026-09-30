"""Real isolated subprocess lifecycle smoke; all HTTP is synthetic in the child."""
from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bug_hunter import jev, transport


KEY = "offline-worker-sentinel-credential"
WIRE = jev.encode_request({"text": "synthetic source"})


class OfflineWorker(transport.KillableWorkerTransport):
    def __init__(self, mode="http", **options):
        self.mode = mode
        super().__init__(KEY, **options)

    def _command(self):
        return [transport._worker_interpreter(), "-I", "-S", str(Path(__file__).with_name("worker_fixture.py")),
                str(Path(transport.__file__).resolve()), self.mode]


class WorkerLifecycleTests(unittest.TestCase):
    def assert_error(self, code, function, *args):
        with self.assertRaises(jev.JevError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__context__)

    def test_support_check_is_read_only_and_unsupported_runtime_is_explicit(self):
        with patch.object(transport.subprocess, "Popen") as launch:
            with patch.object(transport, "_worker_interpreter", return_value="known-python"):
                self.assertTrue(transport.supports_worker())
            with patch.object(transport, "_worker_interpreter", side_effect=RuntimeError()):
                self.assertFalse(transport.supports_worker())
            launch.assert_not_called()

    def test_worker_reuses_protocol_and_context_but_fresh_http_then_retires(self):
        with OfflineWorker(max_requests=2) as worker:
            first = worker.exchange(WIRE, 5)
            second = worker.exchange(WIRE, 5)
            child = worker._child
            self.assertEqual(child.ready_pid, child.process.pid)
            third = worker.exchange(WIRE, 5)
            self.assertEqual(jev.validate_response(first.raw)[0], Decimal("0.13"))
            self.assertEqual(jev.validate_response(second.raw)[1],
                             {"input_tokens": 2, "output_tokens": 2})
            self.assertEqual((first.sequence, second.sequence, third.sequence), (1, 2, 1))
            self.assertEqual((first.reused, second.reused, third.reused), (False, True, False))
            self.assertEqual(first.request_sha256, second.request_sha256)
            self.assertGreater(third.worker_generation, second.worker_generation)
            self.assertIsNotNone(child.process.poll())

    def test_clean_connection_close_reconnects_without_replaying_request(self):
        with OfflineWorker("clean_close") as worker:
            worker.exchange(WIRE, 5)
            second = worker.exchange(WIRE, 5)
            self.assertEqual(jev.validate_response(second.raw)[1],
                             {"input_tokens": 2, "output_tokens": 2})
            self.assertTrue(second.reused)

    def test_ambiguous_disconnect_is_not_retried_and_replacement_is_new_attempt(self):
        with OfflineWorker("drop_second") as worker:
            first = worker.exchange(WIRE, 5)
            child = worker._child
            self.assert_error("transport_failed", worker.exchange, WIRE, 5)
            self.assertIsNone(worker._child)
            self.assertIsNotNone(child.process.poll())
            third = worker.exchange(WIRE, 5)
            self.assertGreater(third.worker_generation, first.worker_generation)
            self.assertEqual(jev.validate_response(third.raw)[1]["output_tokens"], 1)

    def test_real_child_retains_canonical_request_and_http_validation(self):
        with OfflineWorker() as worker:
            self.assert_error("invalid_request", worker.exchange, b"{}", 5)
        with OfflineWorker() as worker:
            self.assert_error("sensitive_input", worker.exchange,
                              jev.encode_request({"text": KEY}), 5)
        with OfflineWorker("bad_http") as worker:
            self.assert_error("response_framing", worker.exchange, WIRE, 5)
        with OfflineWorker("empty_http") as worker:
            reply = worker.exchange(WIRE, 5)
            self.assertEqual(reply.raw, b"")
            self.assert_error("response_invalid_json", jev.validate_response, reply.raw)

    def test_stale_hash_nonce_oversized_and_truncated_frames_fail_closed(self):
        for mode in ("stale", "wrong_hash", "wrong_nonce", "oversized", "truncated"):
            with self.subTest(mode=mode), OfflineWorker(mode) as worker:
                code = "transport_failed" if mode == "truncated" else "response_framing"
                self.assert_error(code, worker.exchange, WIRE, 5)
                self.assertIsNone(worker._child)

    def test_pipe_write_and_read_are_both_within_parent_deadline(self):
        large = jev.encode_request({"text": "x" * 15000})
        for mode in ("block_write", "block_read"):
            with self.subTest(mode=mode), OfflineWorker(mode) as worker:
                # Launch without submitting, then let interpreter startup finish
                # so the measured deadline exercises a blocked pipe operation.
                worker._child = worker._spawn()
                time.sleep(0.7)
                child = worker._child
                started = time.monotonic()
                self.assert_error("deadline_exceeded", worker.exchange, large, 0.15)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertIsNotNone(child.process.poll())
                self.assertFalse(child.io_thread.is_alive())
                self.assertEqual(child.ready_pid, child.process.pid)

    def test_close_cancels_inflight_and_concurrent_exchange_is_rejected(self):
        worker = OfflineWorker("block_read")
        errors = []

        def exchange():
            try:
                worker.exchange(WIRE, 5)
            except jev.JevError as exc:
                errors.append(exc.code)

        caller = threading.Thread(target=exchange)
        caller.start()
        deadline = time.monotonic() + 3
        while worker._child is None and time.monotonic() < deadline:
            time.sleep(0.005)
        child = worker._child
        self.assertIsNotNone(child)
        self.assert_error("invalid_request", worker.exchange, WIRE, 1)
        worker.close()
        caller.join(timeout=2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, ["transport_failed"])
        self.assertIsNotNone(child.process.poll())

    def test_idle_worker_is_reaped_and_empty_closed_worker_never_launches(self):
        with OfflineWorker(idle_seconds=0.05) as worker:
            worker.exchange(WIRE, 5)
            child = worker._child
            deadline = time.monotonic() + 2
            while child.process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(child.process.poll())
            self.assertIsNone(worker._child)
        with patch.object(transport.subprocess, "Popen") as launch:
            with OfflineWorker():
                pass
            launch.assert_not_called()

    def test_stderr_cannot_fill_a_pipe_and_child_environment_is_private(self):
        original = transport.subprocess.Popen
        with patch.dict(os.environ, {"UNRELATED_SECRET": "do-not-inherit"}):
            with patch.object(transport.subprocess, "Popen", wraps=original) as launch:
                with OfflineWorker("noisy") as worker:
                    self.assertEqual(worker.exchange(WIRE, 5).raw, b"{}")
                environment = launch.call_args.kwargs["env"]
                self.assertNotIn("UNRELATED_SECRET", environment)
                self.assertEqual(set(environment) - {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"},
                    {"TYPESAFE_API_KEY", "JEV_WORKER_MAX_REQUESTS", "JEV_WORKER_LIFETIME_SECONDS"})

    @patch.object(transport, "_wait_os_process", return_value=True)
    def test_failed_kill_wait_or_join_poison_and_retain_ownership(self, _os_wait):
        for stage in ("kill", "wait", "join"):
            with self.subTest(stage=stage):
                worker = OfflineWorker()
                process = Mock()
                process.poll.return_value = None if stage in {"kill", "wait"} else 0
                process.wait.return_value = 0
                thread = Mock()
                thread.is_alive.return_value = False
                child = transport._Process(process, b"0" * 16, 1, time.monotonic(),
                                           io_thread=thread)
                worker._child = child
                getattr(process if stage != "join" else thread, stage).side_effect = OSError()
                if stage == "kill":
                    process.wait.side_effect = subprocess.TimeoutExpired([], 2)
                self.assert_error("transport_failed", worker.invalidate)
                self.assertTrue(worker.cleanup_failed)
                self.assertFalse(child.stopped)
                self.assertIs(worker._child, child)
                self.assertIn(child, worker._quarantined)
                with patch.object(worker, "_spawn") as spawn:
                    self.assert_error("transport_failed", worker.exchange, WIRE, 1)
                    spawn.assert_not_called()
                process.kill.side_effect = process.wait.side_effect = thread.join.side_effect = None
                process.poll.return_value = 0
                worker.close()
                self.assertTrue(child.stopped)
                self.assertIsNone(worker._child)

    @patch.object(transport, "_wait_os_process", return_value=True)
    def test_repeated_cleanup_interruptions_never_mark_cleanup_done(self, _os_wait):
        worker = OfflineWorker()
        process = Mock()
        process.poll.return_value = None
        process.kill.side_effect = KeyboardInterrupt
        child = transport._Process(process, b"0" * 16, 1, time.monotonic())
        worker._child = child
        for operation in (worker.invalidate, worker.close):
            with self.assertRaises(KeyboardInterrupt):
                operation()
            self.assertFalse(child.stopped)
            self.assertTrue(worker.cleanup_failed)
            self.assertIs(worker._child, child)
        with patch.object(worker, "_spawn") as spawn:
            self.assert_error("transport_failed", worker.exchange, WIRE, 1)
            spawn.assert_not_called()
        process.kill.side_effect = None
        process.poll.return_value = 0
        worker.close()

    @patch.object(transport, "_wait_os_process", return_value=True)
    def test_idle_cleanup_failure_poison_is_visible_and_launches_nothing(self, _os_wait):
        worker = OfflineWorker()
        process = Mock()
        process.poll.return_value = None
        process.kill.side_effect = OSError()
        process.wait.side_effect = subprocess.TimeoutExpired([], 2)
        child = transport._Process(process, b"0" * 16, 1, time.monotonic())
        worker._child = child
        worker._expire(child, worker._timer_version)
        self.assertTrue(worker.cleanup_failed)
        self.assertIs(worker._child, child)
        with patch.object(worker, "_spawn") as spawn:
            self.assert_error("transport_failed", worker.exchange, WIRE, 1)
            spawn.assert_not_called()
        process.kill.side_effect = None
        process.wait.side_effect = None
        process.poll.return_value = 0
        worker.close()

    def test_idle_retirement_race_waits_without_rejecting_legitimate_caller(self):
        worker = OfflineWorker()
        first = worker.exchange(WIRE, 5)
        child = worker._child
        entered, release = threading.Event(), threading.Event()
        original = worker._stop
        results, errors = [], []

        def retire(target):
            if target is child:
                entered.set()
                release.wait(2)
            return original(target)

        def call():
            try:
                results.append(worker.exchange(WIRE, 5))
            except jev.JevError as exc:
                errors.append(exc.code)

        with patch.object(worker, "_stop", side_effect=retire):
            reaper = threading.Thread(target=worker._expire, args=(child, worker._timer_version))
            reaper.start()
            self.assertTrue(entered.wait(1))
            caller = threading.Thread(target=call)
            caller.start()
            time.sleep(0.02)
            self.assertEqual(errors, [])
            release.set()
            reaper.join(timeout=2)
            caller.join(timeout=5)
        self.assertFalse(caller.is_alive())
        self.assertFalse(reaper.is_alive())
        self.assertEqual(errors, [])
        self.assertGreater(results[0].worker_generation, first.worker_generation)
        worker.close()

    def test_nonce_allocation_failure_cannot_leave_a_launched_process(self):
        with OfflineWorker() as worker:
            with patch.object(transport.os, "urandom", side_effect=OSError()):
                with patch.object(transport.subprocess, "Popen") as launch:
                    self.assert_error("transport_failed", worker.exchange, WIRE, 1)
                    launch.assert_not_called()

    def test_cached_exit_code_without_os_exit_proof_stays_quarantined(self):
        worker = OfflineWorker()
        process = Mock()
        process.poll.return_value = 2
        process.wait.return_value = 2
        child = transport._Process(process, b"0" * 16, 1, time.monotonic())
        worker._child = child
        with patch.object(transport, "_wait_os_process", return_value=False):
            self.assert_error("transport_failed", worker.invalidate)
            self.assertFalse(child.stopped)
            self.assertTrue(worker.cleanup_failed)
            self.assertIs(worker._child, child)
            with patch.object(worker, "_spawn") as spawn:
                self.assert_error("transport_failed", worker.exchange, WIRE, 1)
                spawn.assert_not_called()
        with patch.object(transport, "_wait_os_process", return_value=True):
            worker.close()
        self.assertTrue(child.stopped)

    def test_interrupted_postlaunch_registration_reaps_exact_process(self):
        for failure in (KeyboardInterrupt, OSError):
            with self.subTest(failure=failure.__name__), OfflineWorker() as worker:
                children = []

                def interrupted(child):
                    children.append(child)
                    raise failure()

                with patch.object(worker, "_register_child", side_effect=interrupted):
                    if failure is KeyboardInterrupt:
                        with self.assertRaises(KeyboardInterrupt):
                            worker.exchange(WIRE, 5)
                    else:
                        self.assert_error("transport_failed", worker.exchange, WIRE, 5)
                self.assertEqual(len(children), 1)
                self.assertTrue(children[0].stopped)
                self.assertIsNotNone(children[0].process.poll())
                self.assertIsNone(worker._child)

    def test_native_thread_start_failure_reaps_process_and_closes_pipes(self):
        with OfflineWorker() as worker:
            children = []
            original = worker._register_child

            def remember(child):
                children.append(child)
                original(child)

            with patch.object(worker, "_register_child", side_effect=remember):
                with patch.object(transport.threading.Thread, "start", side_effect=RuntimeError()):
                    self.assert_error("transport_failed", worker.exchange, WIRE, 5)
            self.assertEqual(len(children), 1)
            child = children[0]
            self.assertTrue(child.stopped)
            self.assertIsNotNone(child.process.poll())
            self.assertTrue(child.process.stdin.closed)
            self.assertTrue(child.process.stdout.closed)
            self.assertIsNone(worker._child)

    def test_idle_worker_exits_after_abrupt_parent_death(self):
        controller = subprocess.Popen([transport._worker_interpreter(), "-I", "-S",
            str(Path(__file__).with_name("worker_parent_fixture.py")),
            str(Path(__file__).resolve().parents[1])], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        lines = []
        reader = threading.Thread(target=lambda: lines.append(controller.stdout.readline()), daemon=True)
        reader.start()
        try:
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            pid = json.loads(lines[0])["worker_pid"]
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                handle = kernel.OpenProcess(0x100000, False, pid)
                self.assertTrue(handle)
                try:
                    controller.kill()
                    controller.wait(timeout=2)
                    self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
                finally:
                    kernel.CloseHandle(handle)
            else:
                controller.kill()
                controller.wait(timeout=2)
                gone = False
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        gone = True
                        break
                    status = Path(f"/proc/{pid}/stat")
                    try:
                        if status.exists() and ") Z " in status.read_text():
                            gone = True  # Exited; the reparented process awaits OS reap.
                            break
                    except FileNotFoundError:
                        gone = True
                        break
                    time.sleep(0.01)
                self.assertTrue(gone)
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.wait(timeout=2)
            reader.join(timeout=2)
            controller.stdin.close()
            controller.stdout.close()


if __name__ == "__main__":
    unittest.main()
