"""Real CLI regressions for rounded Choice mass; finite replies, no live judge.

These establish that a valid localization answer reaches the reported narrow
line. They do not measure Jev's ability to identify that line independently.
"""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bug_hunter import __main__ as cli, jev
from bug_hunter.claims import ROLES
from bug_hunter.core import Config, Source, Span, scan
from tests.support import FixtureProvider, KEY, legacy_exchange


class RoundedLocalizationProvider(FixtureProvider):
    def __init__(self, fault=None):
        super().__init__(bugs=[Span(7, 7)])
        self.fault = fault

    def answer(self, qid, question, state):
        if qid in ROLES:
            target = state["scoped_proposition"]["target"]
            hot = target["start_line"] <= 7 <= target["end_line"]
            values = dict(zip(ROLES, (.95, .05, .05) if hot else (.05, .95, .05)))
            return {"type": "noul", "noul": values[qid]}
        if qid == "where" and "L7-7" in question["criteria"]:
            probabilities = dict.fromkeys(question["criteria"], 0)
            probabilities["L7-7"] = .99
            other = next(k for k in probabilities if k != "L7-7")
            if self.fault == "impossible_mass":
                probabilities["L7-7"] = probabilities[other] = .9
            elif self.fault == "wrong_argmax":
                probabilities["L7-7"], probabilities[other] = .49, .50
            return {"type": "choice", "choice": "L7-7",
                    "confidence": .99, "probabilities": probabilities}
        if qid.startswith("requirement_"):
            return {"type": "noul", "noul": .95}
        return super().answer(qid, question, state)


class RoundedLocalizationTests(unittest.TestCase):
    def run_cli(self, policy, *, strict=False, fault=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target, spec, output = root / "subject.py", root / "requirements.txt", root / "runs"
            lines = [f"marker_{i} = {i}" for i in range(1, 13)]
            lines[5:7] = ["def total(count):", "    return count * 21"]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            spec.write_text("total returns exactly 20 times its integer count.\n", encoding="utf-8")
            provider = RoundedLocalizationProvider(fault)
            args = [str(target), "--scope", "standalone", "--spec-file", str(spec),
                    "--output-dir", str(output), "--search-policy", policy,
                    "--width", "12", "--min-width", "12", "--max-depth", "0",
                    "--no-whole-file", "--no-bug-lenses", "--max-localizations", "1",
                    "--max-action-steps", "0", "--max-calls", "32"]
            if strict:
                args.append("--strict-choice-mass")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY}), legacy_exchange(), \
                    patch.object(jev.subprocess, "run", provider.process), \
                    patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(args)
            reports = list(output.glob("run-*/report.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            receipts = [json.loads(p.read_text(encoding="utf-8"))
                        for p in reports[0].parent.joinpath("receipts").glob("call_*.json")]
            return code, report, receipts

    def test_default_cli_localizes_rounded_answer_below_twelve_lines(self):
        for policy in ("legacy-v1", "scoped-v2-beta"):
            with self.subTest(policy=policy):
                code, report, receipts = self.run_cli(policy)
                self.assertEqual(code, 1, report["issues"])
                self.assertEqual(report["status"], "complete")
                self.assertIn((7, 7), {(f["start_line"], f["end_line"]) for f in report["findings"]})
                self.assertEqual(report["questions_validated"], report["questions_attempted"])
                self.assertTrue(report["existence_checks"])
                self.assertTrue(all(r["rounding_places"] == 2 for r in receipts))
                localized = [r for r in receipts if r["metadata"]["phase"] == "localize"]
                self.assertTrue(localized)
                answer = localized[0]["answers"]["where"]
                self.assertEqual(sum(map(Decimal, answer["probabilities"].values())), Decimal(".99"))
                self.assertEqual(answer["probabilities"]["L7-7"], "0.99")
                self.assertEqual(answer["confidence"], "0.99")

    def test_explicit_strict_keeps_limitation_and_rejects_entire_pair(self):
        for policy in ("legacy-v1", "scoped-v2-beta"):
            with self.subTest(policy=policy):
                code, report, receipts = self.run_cli(policy, strict=True)
                self.assertEqual(code, 2)
                self.assertEqual(report["status"], "incomplete")
                self.assertNotIn((7, 7), {(f["start_line"], f["end_line"]) for f in report["findings"]})
                self.assertTrue(any(i["code"] == "response_distribution" and i["phase"] == "localize"
                                    for i in report["issues"]))
                self.assertFalse(report["existence_checks"])
                failed = [r for r in receipts if r["metadata"]["phase"] == "localize"]
                self.assertTrue(failed)
                self.assertTrue(all(r["answers"] is None and r["usage"] is not None
                                    and r["rounding_places"] is None for r in failed))

    def test_compatibility_default_still_rejects_impossible_mass_and_wrong_argmax(self):
        for fault, error in (("impossible_mass", "response_distribution"),
                             ("wrong_argmax", "choice_argmax_mismatch")):
            with self.subTest(fault=fault):
                code, report, receipts = self.run_cli("legacy-v1", fault=fault)
                self.assertEqual(code, 2)
                self.assertTrue(any(i["code"] == error and i["phase"] == "localize"
                                    for i in report["issues"]))
                self.assertFalse(report["existence_checks"])
                self.assertTrue(all(r["answers"] is None for r in receipts
                                    if r["metadata"]["phase"] == "localize"))

    def test_raw_object_and_implicit_custom_gateway_use_same_default(self):
        questions = {"where": {"type": "choice", "instructions": "Choose.",
                               "criteria": {"a": "A", "b": "B", "c": "C"}}}
        answers = {"where": {"type": "choice", "choice": "a", "confidence": .9,
                             "probabilities": {"a": .6, "b": .2, "c": .19}}}
        raw = json.dumps({"model": jev.MODEL, "answers": answers,
                          "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        hosted = jev.validate_answers(raw, questions)[0]
        self.assertEqual(hosted, jev.validate_answer_objects(answers, questions))
        self.assertEqual(sum(hosted["where"]["probabilities"].values()), Decimal(".99"))
        for validate in (lambda: jev.validate_answers(raw, questions, rounding_places=None),
                         lambda: jev.validate_answer_objects(answers, questions, rounding_places=None)):
            with self.assertRaises(jev.JevError) as caught:
                validate()
            self.assertEqual(caught.exception.code, "response_distribution")

        provider = RoundedLocalizationProvider()
        source = Source.from_text("\n".join(f"line {i}" for i in range(1, 13)))
        cfg = Config(width=12, min_width=12, max_depth=0, whole_file=False,
                     bug_lenses=False, max_localizations=1, max_action_steps=0)
        result = scan(source, provider, cfg, context_scope="declared_standalone")
        self.assertEqual(result["status"], "complete", result["issues"])
        self.assertIn((7, 7), {(f["start_line"], f["end_line"]) for f in result["findings"]})


if __name__ == "__main__":
    unittest.main()
