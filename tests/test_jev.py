"""Offline contract/security tests; sockets and subprocesses are synthetic."""
from decimal import Decimal
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bug_hunter import jev


KEY = "offline-sentinel-credential-9e91"
STATE = {"file": "example.txt", "context": {"text": "sample"}}
META = {"window_id": "1:48", "depth": 0, "sequence": 1,
        "source_sha256": "a" * 64}


def response(token="0.82", **extra):
    data = {"model": jev.MODEL, "answers": {"q": {"type": "noul", "noul": 0}},
            "usage": {"input_tokens": 100, "output_tokens": 1}}
    data.update(extra)
    return json.dumps(data).replace('"noul": 0', '"noul": ' + token).encode()


class JsonContractTests(unittest.TestCase):
    def assert_code(self, code, function, *args):
        with self.assertRaises(jev.JevError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_fixed_envelope_and_byte_budget(self):
        wire = jev.encode_request(STATE)
        body = json.loads(wire)
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(body["questions"], {"q": jev.QUESTION})
        self.assertEqual(body["state"], STATE)
        self.assertNotIn(b'\n', wire)
        self.assert_code("request_too_large", jev.encode_request,
                         {"text": "\u6f22" * 6000})

    def test_non_json_and_nonfinite_request_rejected(self):
        circular = []
        circular.append(circular)
        cases = [[], {"bad": float("nan")}, {"bad": {1, 2}},
                 {1: "not a JSON key"}, {"bad": "\ud800"}, {"bad": circular}]
        for case in cases:
            with self.subTest(case=type(case)):
                self.assert_code("invalid_request", jev.encode_request, case)

    def test_integer_decimal_and_threshold_tokens_remain_exact(self):
        for token in ("0", "1", "1.0", "0.59999999999999999999", "1e-400"):
            score, usage = jev.validate_response(response(token))
            self.assertIs(type(score), Decimal)
            self.assertEqual(score, Decimal(token))
            self.assertEqual(usage["input_tokens"], 100)
        below, _ = jev.validate_response(response("0.59999999999999999999"))
        self.assertLess(below, Decimal("0.60"))

    def test_invalid_probability_tokens_rejected(self):
        for token in ("true", "false", "null", '"0.82"', "[]", "{}",
                      "-1e-400", "1.00000000000000000001", "2", "-1",
                      "1e-1001", "0." + "1" * 128):
            with self.subTest(token=token[:32]):
                self.assert_code("response_probability", jev.validate_response,
                                 response(token))
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assert_code("response_invalid_json", jev.validate_response,
                             response(token))

    def test_strict_diagnostics(self):
        jev.validate_response(response(diagnostics={}))
        jev.validate_response(response(diagnostics={k: False for k in jev.DIAGNOSTICS}))
        for name in jev.DIAGNOSTICS:
            self.assert_code("response_truncated", jev.validate_response,
                             response(diagnostics={name: True}))
            for value in (1, "true", None, [], {}):
                self.assert_code("response_schema", jev.validate_response,
                                 response(diagnostics={name: value}))
        self.assert_code("response_schema", jev.validate_response,
                         response(diagnostics={"future_truncation": False}))

    def test_schema_model_and_usage_changes_rejected(self):
        for model in ("jev-1.13.1", "jev-latest", "jev-\u0661.\u0661\u0663.\u0660", 1):
            self.assert_code("response_model", jev.validate_response,
                             response(model=model))
        for usage in ({"input_tokens": True, "output_tokens": 1},
                      {"input_tokens": -1, "output_tokens": 1},
                      {"input_tokens": 1.0, "output_tokens": 1},
                      {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}):
            self.assert_code("response_usage", jev.validate_response,
                             response(usage=usage))
        for answers in ({}, {"another": {"type": "noul", "noul": 1}},
                        {"q": {"type": "choice", "choice": "yes"}},
                        {"q": {"type": "noul", "noul": 1, "why": "text"}}):
            self.assert_code("response_schema", jev.validate_response,
                             response(answers=answers))
        self.assert_code("response_schema", jev.validate_response,
                         response(new_envelope_field=1))

    def test_malformed_duplicate_and_sensitive_responses_do_not_chain(self):
        for raw in (b'{"secret":', b'\xff', b'{}{}', b'{"x":1,"x":2}'):
            self.assert_code("response_invalid_json", jev.validate_response, raw)
        raw = response(diagnostics={"unexpected": KEY})
        self.assert_code("response_sensitive", jev.validate_response, raw, KEY)
        escaped = raw.replace(KEY.encode(), rb'\u006f' + KEY[1:].encode())
        self.assert_code("response_sensitive", jev.validate_response, escaped, KEY)
        self.assert_code("response_too_large", jev.validate_response,
                         b" " * (jev.MAX_RESPONSE_BYTES + 1))


class SocketTests(unittest.TestCase):
    def exchange(self, headers, body=b"{}", status=200):
        wire = (f"HTTP/1.1 {status} Test\r\n" + headers + "\r\n\r\n").encode()
        sock = MagicMock()
        sock.makefile.return_value = io.BytesIO(wire + body)
        parsed = http.client.HTTPResponse(sock)
        parsed.begin()
        connection = MagicMock()
        connection.getresponse.return_value = parsed
        try:
            with patch.object(jev.http.client, "HTTPSConnection", return_value=connection):
                result = jev._http_exchange(b'{"request":1}', KEY)
            return result, connection
        finally:
            # The synthetic connection does not own/close this response. Close
            # it explicitly before BytesIO teardown (including Python 3.13).
            parsed.close()

    def test_fixed_origin_path_header_credential_and_valid_framing(self):
        body, connection = self.exchange(
            "Content-Type: application/json; charset=utf-8\r\nContent-Length: 2")
        self.assertEqual(body, b"{}")
        call = connection.request.call_args
        self.assertEqual(call.args, ("POST", "/v1/systemone"))
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + KEY)
        self.assertNotIn(KEY.encode(), call.kwargs["body"])
        connection.close.assert_called_once()
        body, _ = self.exchange("Content-Type: application/json\r\n"
                                "Transfer-Encoding: chunked", b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(body, b"{}")

    def test_ambiguous_and_unsupported_headers_rejected(self):
        common = "Content-Type: application/json\r\n"
        cases = ["Content-Length: 2\r\nContent-Length: 2",
                 "Content-Length: 2\r\nTransfer-Encoding: chunked",
                 "Transfer-Encoding: gzip", "Transfer-Encoding: chunked, gzip",
                 "Content-Length: +2", "Content-Length: 3",
                 "Content-Type: text/plain", "Content-Encoding: gzip",
                 "Content-Encoding: identity\r\nContent-Encoding: identity"]
        for headers in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(jev.JevError):
                    self.exchange(common + headers)
        with self.assertRaises(jev.JevError):
            self.exchange("Content-Type: application/json; charset=utf-8\r\n"
                          "Content-Type: text/plain")

    def test_redirect_and_status_never_follow_or_echo(self):
        for status, code in ((302, "http_redirect"), (401, "http_unauthorized"),
                             (429, "http_rate_limited"), (500, "http_rejected")):
            with self.assertRaises(jev.JevError) as caught:
                self.exchange("Location: https://elsewhere.invalid/" + KEY,
                              KEY.encode(), status)
            self.assertEqual(caught.exception.code, code)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.gateway = jev.HostedJev(Path(self.directory.name) / "receipts")

    def receipt(self):
        return json.loads(Path(self.gateway.last_receipt).read_text())

    def test_success_has_metadata_and_hashes_but_no_raw_payloads(self):
        completed = subprocess.CompletedProcess([], 0, response("1"), b"")
        with patch.object(jev.subprocess, "run", return_value=completed) as run:
            score = self.gateway.score(STATE, metadata={**META, "depth": -1})
        self.assertEqual(score, Decimal(1))
        self.assertEqual(self.receipt()["status"], "validated")
        self.assertTrue(self.receipt()["transport_attempted"])
        self.assertEqual(self.receipt()["score"], "1")
        self.assertEqual(len(self.receipt()["request_sha256"]), 64)
        self.assertEqual(len(self.receipt()["response_sha256"]), 64)
        files = list(self.gateway.receipt_dir.iterdir())
        self.assertEqual(len(files), 1)
        saved = files[0].read_text()
        self.assertNotIn(KEY, saved)
        self.assertNotIn("example.txt", saved)
        self.assertNotIn("sample", saved)
        command = run.call_args.args[0]
        self.assertEqual(command[1], "-I")
        self.assertEqual(Path(command[2]), Path(jev.__file__).resolve())
        self.assertNotIn(KEY, " ".join(command))
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)
        if os.name == "posix":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_preflight_rejection_is_recorded_without_network(self):
        with patch.object(jev.subprocess, "run") as run:
            with self.assertRaises(jev.JevError) as caught:
                self.gateway.score({"text": "x" * 20000}, metadata=META)
            self.assertEqual(caught.exception.code, "request_too_large")
            run.assert_not_called()
        record = self.receipt()
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["transport_attempted"])
        self.assertIsNone(record["request_sha256"])
        self.assertIsNone(record["response_sha256"])

    def test_secret_in_source_or_metadata_never_reaches_disk_or_transport(self):
        with patch.object(jev.subprocess, "run") as run:
            for state, meta in (({"text": KEY}, META), (STATE, {"window_id": KEY})):
                with self.assertRaises(jev.JevError) as caught:
                    self.gateway.score(state, metadata=meta)
                self.assertEqual(caught.exception.code, "sensitive_input")
                self.assertNotIn(KEY, Path(self.gateway.last_receipt).read_text())
            run.assert_not_called()
        for value in (KEY, KEY.encode(), {KEY: 1}, ("safe", KEY)):
            with self.assertRaises(jev.JevError):
                jev.check_sensitive(value)

    def test_timeout_and_launch_failure_leave_no_exception_context(self):
        failures = [subprocess.TimeoutExpired([KEY], 1, output=KEY.encode()),
                    FileNotFoundError(KEY)]
        for failure in failures:
            with patch.object(jev.subprocess, "run", side_effect=failure):
                with self.assertRaises(jev.JevError) as caught:
                    self.gateway.score(STATE, metadata=META)
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(KEY, Path(self.gateway.last_receipt).read_text())
            self.assertEqual(self.receipt()["status"], "failed")

    def test_reflected_key_and_child_error_are_sanitized(self):
        outputs = [subprocess.CompletedProcess([], 0, response(why=KEY), b""),
                   subprocess.CompletedProcess([], 2, KEY.encode(), KEY.encode())]
        for result in outputs:
            with patch.object(jev.subprocess, "run", return_value=result):
                with self.assertRaises(jev.JevError):
                    self.gateway.score(STATE, metadata=META)
            self.assertNotIn(KEY, Path(self.gateway.last_receipt).read_text())

    def test_changed_state_after_launch_does_not_change_request(self):
        state = {"text": "original"}

        def execute(*args, **kwargs):
            state["text"] = "changed after launch"
            self.assertEqual(json.loads(kwargs["input"])["state"]["text"], "original")
            return subprocess.CompletedProcess([], 0, response(), b"")

        with patch.object(jev.subprocess, "run", side_effect=execute):
            self.gateway.score(state, metadata=META)

    def test_receipt_failure_prevents_valid_score_from_escaping(self):
        original = jev._write_receipt
        count = 0

        def persist(path, record):
            nonlocal count
            count += 1
            if count == 4:
                raise jev.JevError("receipt_failed")
            original(path, record)

        with patch.object(jev, "_write_receipt", side_effect=persist):
            with patch.object(jev.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], 0, response(), b"")):
                with self.assertRaises(jev.JevError) as caught:
                    self.gateway.score(STATE, metadata=META)
        self.assertEqual(caught.exception.code, "receipt_failed")
        self.assertEqual(self.receipt()["status"], "dispatching")
        self.assertTrue(self.receipt()["transport_attempted"])


if __name__ == "__main__":
    unittest.main()
