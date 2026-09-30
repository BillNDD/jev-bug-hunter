"""Single-caller killable transport with bounded interpreter/context reuse.

The receipt owner must persist dispatch intent before exchange() and a terminal
receipt before releasing validated answers. A replacement worker never retries
an interrupted request. All request bodies and credentials remain in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import threading
import time

_MAX_REQUEST = 24 * 1024
_MAX_RESPONSE = 256 * 1024
_REQUEST = struct.Struct("!4s16sQId32s")
_RESPONSE = struct.Struct("!4s16sQBI32s")
_READY = struct.Struct("!4sQ")
_REQUEST_MAGIC, _RESPONSE_MAGIC = b"JVQ1", b"JVA1"


def _worker_interpreter():
    """Avoid Windows venv redirectors: kill must own the actual Python PID."""
    if os.name != "nt":
        return sys.executable
    import ctypes
    from ctypes import wintypes
    if sys.implementation.name != "cpython":
        raise RuntimeError("transport_failed")
    module_filename = ctypes.WinDLL("kernel32", use_last_error=True).GetModuleFileNameW
    module_filename.argtypes = (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
    module_filename.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    count = module_filename(None, buffer, len(buffer))
    base = getattr(sys, "_base_executable", None)
    if (not 0 < count < len(buffer) or type(base) is not str
            or not os.path.samefile(buffer.value, base)):
        raise RuntimeError("transport_failed")
    return buffer.value


def _wait_os_process(process, timeout):
    """Windows exit-code caching is not proof that its process handle signaled."""
    if os.name != "nt":
        return True  # POSIX Popen.wait already obtains waitpid's reap result.
    import _winapi
    return _winapi.WaitForSingleObject(
        process._handle, max(0, math.ceil(timeout * 1000))) == _winapi.WAIT_OBJECT_0


def supports_worker():
    """Read-only ownership preflight; no child or request is started here."""
    try:
        executable = _worker_interpreter()
        return type(executable) is str and bool(executable)
    except Exception:
        return False


def _adapter():
    # -I -S deliberately excludes site-packages; load only this installed sibling.
    if __package__:
        from . import jev
        return jev
    spec = importlib.util.spec_from_file_location(
        "_jev_worker_adapter", Path(__file__).with_name("jev.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_exact(stream, count):
    chunks = []
    while count:
        chunk = stream.read(count)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _write_all(stream, data):
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise OSError
        view = view[written:]
    stream.flush()


@dataclass(frozen=True)
class TransportReply:
    raw: bytes
    request_sha256: str
    worker_generation: int
    sequence: int
    reused: bool
    elapsed_seconds: float


@dataclass
class _Process:
    process: subprocess.Popen
    nonce: bytes
    generation: int
    created: float
    sequence: int = 0
    ready_pid: int | None = None
    io_thread: threading.Thread | None = None
    stop_lock: threading.Lock = field(default_factory=threading.Lock)
    stopped: bool = False


class KillableWorkerTransport:
    """One in-flight request, a bounded idle/lifetime, and explicit close().

    The entire pipe write and read run in a supervised thread. A deadline or
    cancellation kills and reaps the owning child before reuse is possible.
    Concurrent calls are rejected; callers must not share mutable adapter stats.
    """

    def __init__(self, api_key, *, max_requests=128, idle_seconds=30.0,
                 lifetime_seconds=300.0):
        self._jev = _adapter()
        if (type(api_key) is not str or not api_key or len(api_key) > 4096
                or not all("!" <= item <= "~" for item in api_key)):
            raise self._jev.JevError("missing_api_key")
        if (type(max_requests) is not int or not 1 <= max_requests <= 256
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       or not 0 < v <= 3600 for v in (idle_seconds, lifetime_seconds))):
            raise self._jev.JevError("invalid_deadline")
        self._key = api_key
        error = False
        try:
            self._executable = _worker_interpreter()
        except Exception:
            error = True
        if error:
            raise self._jev.JevError("transport_failed")
        self.max_requests = max_requests
        self.idle_seconds = float(idle_seconds)
        self.lifetime_seconds = float(lifetime_seconds)
        self._call_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._child = None
        self._timer = None
        self._timer_version = 0
        self._generation = 0
        self._closed = False
        self._quarantined = []
        self.cleanup_failed = False

    def _command(self):
        return [self._executable, "-I", "-S", str(Path(__file__).resolve()), "--worker"]

    def _spawn(self):
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
               if key in os.environ}
        env.update(TYPESAFE_API_KEY=self._key,
                   JEV_WORKER_MAX_REQUESTS=str(self.max_requests),
                   JEV_WORKER_LIFETIME_SECONDS=repr(self.lifetime_seconds))
        # Allocate everything which could fail before a live process exists.
        self._generation += 1
        child = _Process(None, os.urandom(16), self._generation, time.monotonic())
        try:
            child.process = subprocess.Popen(self._command(), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             env=env, bufsize=0)
            self._register_child(child)
        except BaseException:
            # Even an interrupted registration retains/reaps this exact process;
            # exchange() has not yet received the local child reference.
            if child.process is not None:
                self._discard(child)
            raise
        return child

    def _register_child(self, child):
        with self._state_lock:
            self._child = child

    @staticmethod
    def _stop(child):
        if child is None:
            return True
        with child.stop_lock:
            if child.stopped:
                return True
            process = child.process
            try:
                stop_deadline = time.monotonic() + 2
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        # A child may already be exiting between poll and kill
                        # (notably Windows access-denied on an exiting process).
                        # Only the bounded wait below can prove it was reaped.
                        pass
                process.wait(timeout=2)
                if not _wait_os_process(process, max(0.0, stop_deadline - time.monotonic())):
                    return False
                # The fixed child never creates descendants retaining pipes.
                # Unbuffered FileIO avoids buffered-I/O locks on cancellation.
                if child.io_thread is not None:
                    child.io_thread.join(timeout=2)
                    if child.io_thread.is_alive():
                        return False
                process.stdin.close()
                process.stdout.close()
            except Exception:
                return False
            child.stopped = True
            return True

    def _quarantine(self, child):
        with self._state_lock:
            self._closed = True
            self.cleanup_failed = True
            if child is not None and not any(item is child for item in self._quarantined):
                self._quarantined.append(child)
            if self._child is None:
                self._child = child

    def _discard(self, child):
        try:
            stopped = self._stop(child)
        except BaseException:
            self._quarantine(child)
            raise
        with self._state_lock:
            if stopped and self._child is child:
                self._child = None
            if stopped:
                self._quarantined = [item for item in self._quarantined if item is not child]
        if not stopped:
            self._quarantine(child)
        return stopped

    def _expire(self, child, version):
        with self._lifecycle_lock:
            with self._state_lock:
                current = self._child is child and self._timer_version == version
            if current:
                self._discard(child)

    def close(self):
        with self._state_lock:
            self._closed = True
            self._timer_version += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        with self._lifecycle_lock:
            with self._state_lock:
                children = list(self._quarantined)
                if self._child is not None and not any(item is self._child for item in children):
                    children.append(self._child)
            stopped = True
            for child in children:
                stopped = self._discard(child) and stopped
        if not stopped:
            raise self._jev.JevError("transport_failed")

    def invalidate(self):
        """Retire current state after failed validation/receipt; never retry it."""
        with self._lifecycle_lock:
            with self._state_lock:
                child = self._child
                self._timer_version += 1
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
            stopped = self._discard(child)
        if not stopped:
            raise self._jev.JevError("transport_failed")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def exchange(self, wire, timeout):
        """Return a bounded correlated raw response, without semantic acceptance."""
        started = time.monotonic()
        error, child, reply = None, None, None
        if type(wire) is not bytes or not 0 < len(wire) <= _MAX_REQUEST:
            raise self._jev.JevError("request_too_large")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 300):
            raise self._jev.JevError("invalid_deadline")
        if not self._call_lock.acquire(blocking=False):
            raise self._jev.JevError("invalid_request")
        deadline = started + timeout
        try:
            # An idle retirement is maintenance, not a second caller. Wait for
            # it within this request's deadline instead of rejecting the caller.
            if not self._lifecycle_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise self._jev.JevError("deadline_exceeded")
            try:
                with self._state_lock:
                    if self._closed:
                        raise self._jev.JevError("transport_failed")
                    if self._timer is not None:
                        self._timer.cancel()
                        self._timer = None
                    self._timer_version += 1
                    child = self._child
                if child is not None and (
                        child.process.poll() is not None or child.sequence >= self.max_requests
                        or time.monotonic() - child.created >= self.lifetime_seconds):
                    if not self._discard(child):
                        raise self._jev.JevError("transport_failed")
                    child = None
                if child is None:
                    child = self._spawn()
                    with self._state_lock:
                        if self._closed:
                            raise self._jev.JevError("transport_failed")
            finally:
                self._lifecycle_lock.release()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._jev.JevError("deadline_exceeded")
            child.sequence += 1
            sequence = child.sequence
            request_hash = hashlib.sha256(wire).digest()
            frame = _REQUEST.pack(_REQUEST_MAGIC, child.nonce, sequence, len(wire),
                                  remaining, request_hash) + wire
            done, outcome = threading.Event(), []

            def exchange_pipes():
                try:
                    if child.ready_pid is None:
                        magic, worker_pid = _READY.unpack(
                            _read_exact(child.process.stdout, _READY.size))
                        if magic != b"JVR1" or worker_pid != child.process.pid:
                            outcome.append(("transport_failed", None))
                            return
                        child.ready_pid = worker_pid
                    _write_all(child.process.stdin, frame)
                    header = _read_exact(child.process.stdout, _RESPONSE.size)
                    magic, nonce, seq, status, size, digest = _RESPONSE.unpack(header)
                    if (magic != _RESPONSE_MAGIC or nonce != child.nonce
                            or seq != sequence or digest != request_hash
                            or status not in (0, 1) or not 0 <= size <= _MAX_RESPONSE
                            or (status == 1 and not 0 < size <= 96)):
                        outcome.append(("response_framing", None))
                    else:
                        raw = _read_exact(child.process.stdout, size)
                        if status:
                            code = raw.decode("ascii", errors="ignore")
                            outcome.append((code if code in self._jev.ERROR_CODES
                                            else "transport_failed", None))
                        else:
                            outcome.append((None, raw))
                except Exception:
                    outcome.append(("transport_failed", None))
                finally:
                    done.set()

            with child.stop_lock:
                if child.stopped:
                    raise self._jev.JevError("transport_failed")
                child.io_thread = threading.Thread(target=exchange_pipes,
                                                   name="jev-pipe-exchange", daemon=True)
                try:
                    child.io_thread.start()
                except Exception:
                    # Thread.start() failures before native-thread creation
                    # must not leave a never-started object for join(). An
                    # interrupted startup stays owned/unknown instead.
                    if child.io_thread.ident is None:
                        child.io_thread = None
                    raise
            if not done.wait(max(0.0, deadline - time.monotonic())):
                raise self._jev.JevError("deadline_exceeded")
            child.io_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if child.io_thread.is_alive() or time.monotonic() > deadline:
                raise self._jev.JevError("deadline_exceeded")
            error, raw = outcome[0]
            if error:
                raise self._jev.JevError(error)
            reply = TransportReply(raw, request_hash.hex(), child.generation,
                                   sequence, sequence > 1, time.monotonic() - started)
            with self._state_lock:
                if self._closed or self._child is not child:
                    raise self._jev.JevError("transport_failed")
                delay = min(self.idle_seconds, max(0.0,
                    self.lifetime_seconds - (time.monotonic() - child.created)))
                self._timer_version += 1
                self._timer = threading.Timer(delay, self._expire,
                                              args=(child, self._timer_version))
                self._timer.daemon = True
                self._timer.start()
        except self._jev.JevError as exc:
            error = exc.code
        except (KeyboardInterrupt, SystemExit):
            self._discard(child)
            raise
        except Exception:
            error = "transport_failed"
        finally:
            try:
                if error:
                    if not self._discard(child):
                        error = "transport_failed"
            finally:
                self._call_lock.release()
        if error:
            raise self._jev.JevError(error)
        return reply


class _HTTPSession:
    """Cache trust setup; retain the original fresh-connection HTTP lifecycle."""

    def __init__(self, jev, key):
        self.jev, self.key = jev, key
        self.context = None

    def close(self):
        self.context = None

    def exchange(self, wire, timeout):
        if self.context is None:
            self.context = ssl.create_default_context()
        return self.jev._http_exchange(wire, self.key, timeout=timeout, context=self.context)


def _worker_main():
    jev = _adapter()
    session = None
    try:
        key = jev._key()
        maximum = int(os.environ.get("JEV_WORKER_MAX_REQUESTS", "128"))
        lifetime = float(os.environ.get("JEV_WORKER_LIFETIME_SECONDS", "300"))
        if not 1 <= maximum <= 256 or not math.isfinite(lifetime) or not 0 < lifetime <= 3600:
            return 2
        session = _HTTPSession(jev, key)
        nonce, started = None, time.monotonic()
        _write_all(sys.stdout.buffer, _READY.pack(b"JVR1", os.getpid()))
        for expected in range(1, maximum + 1):
            header = _read_exact(sys.stdin.buffer, _REQUEST.size)
            magic, current, sequence, size, timeout, digest = _REQUEST.unpack(header)
            if (magic != _REQUEST_MAGIC or sequence != expected
                    or (nonce is not None and nonce != current)
                    or not 0 < size <= _MAX_REQUEST or not math.isfinite(timeout)
                    or not 0 < timeout <= 300 or time.monotonic() - started >= lifetime):
                return 2
            nonce = current
            wire = _read_exact(sys.stdin.buffer, size)
            if hashlib.sha256(wire).digest() != digest:
                return 2
            error = None
            try:
                request = json.loads(wire.decode("utf-8"), object_pairs_hook=jev._pairs,
                                     parse_constant=jev._constant)
                if (type(request) is not dict or set(request) != {"model", "state", "questions"}
                        or request["model"] != jev.MODEL
                        or jev.encode_request(request["state"], request["questions"]) != wire):
                    raise jev.JevError("invalid_request")
                jev.check_sensitive(request["state"], request["questions"])
                raw = session.exchange(wire, timeout)
            except jev.JevError as exc:
                error = exc.code
            except TimeoutError:
                error = "deadline_exceeded"
            except Exception:
                error = "transport_failed"
            body = error.encode("ascii") if error else raw
            if len(body) > _MAX_RESPONSE:
                error, body = "response_too_large", b"response_too_large"
            _write_all(sys.stdout.buffer, _RESPONSE.pack(
                _RESPONSE_MAGIC, nonce, sequence, int(error is not None), len(body), digest) + body)
            if error:
                return 2
        return 0
    except Exception:
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
