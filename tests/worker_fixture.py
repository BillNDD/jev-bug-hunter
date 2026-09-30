"""Offline child fixture; its HTTP constructor never opens a socket."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time


def run(module_path, mode):
    if mode in {"oneshot", "benchmark_once"}:
        spec = importlib.util.spec_from_file_location(
            "offline_single_use", Path(module_path).with_name("jev.py"))
        adapter = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = adapter
        spec.loader.exec_module(adapter)

        class SingleUse:
            http, ssl = adapter.http, adapter.ssl

            @staticmethod
            def _adapter():
                return adapter

        worker = SingleUse
    else:
        spec = importlib.util.spec_from_file_location("offline_worker", module_path)
        worker = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = worker
        spec.loader.exec_module(worker)
    if mode in {"block_write", "block_read", "stale", "wrong_hash", "wrong_nonce",
                "oversized", "truncated", "noisy"}:
        worker._write_all(sys.stdout.buffer, worker._READY.pack(b"JVR1", os.getpid()))
        if mode == "block_write":
            time.sleep(60)
            return 0
        header = worker._read_exact(sys.stdin.buffer, worker._REQUEST.size)
        _, nonce, sequence, size, _timeout, digest = worker._REQUEST.unpack(header)
        worker._read_exact(sys.stdin.buffer, size)
        if mode == "block_read":
            sys.stdout.buffer.write(b"J")
            sys.stdout.buffer.flush()
            time.sleep(60)
            return 0
        if mode == "noisy":
            sys.stderr.buffer.write(b"synthetic stderr " * 100000)
            sys.stderr.buffer.flush()
        if mode == "stale":
            sequence -= 1
        elif mode == "wrong_hash":
            digest = hashlib.sha256(b"different synthetic request").digest()
        elif mode == "wrong_nonce":
            nonce = b"0" * 16
        length = worker._MAX_RESPONSE + 1 if mode == "oversized" else 2
        body = b"{" if mode == "truncated" else b"{}"
        worker._write_all(sys.stdout.buffer, worker._RESPONSE.pack(
            worker._RESPONSE_MAGIC, nonce, sequence, 0, length, digest) + body)
        return 0

    counters = {"connections": 0, "requests": 0, "contexts": 0}

    class Socket:
        def settimeout(self, _timeout):
            pass

    class Response:
        def __init__(self, request):
            self.status = 200
            self.will_close = mode == "clean_close"
            answers = {}
            for key, question in request["questions"].items():
                if question["type"] == "noul" or mode == "wrong_answer_type":
                    answers[key] = {"type": "noul", "noul": 0.13}
                else:
                    criteria = question["criteria"]
                    chosen = "none" if "none" in criteria else next(iter(criteria))
                    answers[key] = {"type": "choice", "choice": chosen,
                        "confidence": 1,
                        "probabilities": {option: int(option == chosen) for option in criteria}}
            self.body = json.dumps({"model": request["model"], "answers": answers,
                                   "usage": ({"input_tokens": 101, "output_tokens": 1}
                                             if mode.startswith("benchmark_") else
                                             {"input_tokens": counters["connections"],
                                              "output_tokens": counters["requests"]})}).encode()
            if mode == "empty_http":
                self.body = b""
            self.stream = io.BytesIO(self.body)

        def getheaders(self):
            headers = [("Content-Type", "application/json"),
                       ("Content-Length", str(len(self.body)))]
            if mode == "bad_http":
                headers.append(("Content-Length", str(len(self.body))))
            return headers

        def read(self, size):
            return self.stream.read(size)

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            assert host == "api.typesafe.ai" and port == 443
            counters["connections"] += 1
            self.sock, self.timeout, self.request_data = Socket(), timeout, None

        def request(self, method, path, *, body, headers):
            assert method == "POST" and path == "/v1/systemone"
            assert headers["Accept-Encoding"] == "identity"
            assert headers["Connection"] == "close"
            counters["requests"] += 1
            self.request_data = json.loads(body)

        def getresponse(self):
            if mode == "drop_second" and counters["requests"] == 2:
                raise worker.http.client.RemoteDisconnected()
            return Response(self.request_data)

        def close(self):
            self.sock = None

    worker.http.client.HTTPSConnection = Connection
    real_context = worker.ssl.create_default_context

    def context():
        counters["contexts"] += 1
        assert counters["contexts"] == 1
        return real_context() if mode.startswith("benchmark_") else object()

    worker.ssl.create_default_context = context
    if mode in {"oneshot", "benchmark_once"}:
        return worker._adapter()._transport_child()
    return worker._worker_main()


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], sys.argv[2]))
