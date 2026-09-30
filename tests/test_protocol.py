"""Batched Noul/Choice contract and rounding-profile regression tests."""
from decimal import Decimal, localcontext
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bug_hunter import jev
from bug_hunter.core import Span, Config
from bug_hunter.questions import load_pack, noul, choice
from tests.support import KEY, FixtureProvider, source, ranges, scan, legacy_exchange


def body(questions, probabilities=None, chosen="a", confidence=0.9):
    answers = {}
    for qid, q in questions.items():
        if q["type"] == "noul":
            answers[qid] = {"type":"noul","noul":0.9}
        else:
            answers[qid] = {"type":"choice","choice":chosen,
                           "confidence":confidence, "probabilities":probabilities}
    return json.dumps({"model":jev.MODEL,"answers":answers,
                       "usage":{"input_tokens":1,"output_tokens":1}}).encode()


class BatchContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(legacy_exchange())
        pack,_=load_pack()
        self.qs={"exists":noul(pack,"screen",Span(1,12)),
                 "where":{"type":"choice","instructions":"Choose an option.",
                          "criteria":{"a":"First","b":"Second","c":"Third"}}}

    def test_mixed_batch_parses_all_and_keeps_decimal_types(self):
        answers,usage=jev.validate_answers(body(self.qs,{"a":0.6,"b":0.3,"c":0.1}),self.qs)
        self.assertEqual(set(answers),set(self.qs))
        self.assertEqual(answers["where"]["choice"],"a")
        self.assertIs(type(answers["exists"]["noul"]),Decimal)
        self.assertIs(type(answers["where"]["probabilities"]["a"]),Decimal)

    def test_batch_answer_omission_rejects_entire_batch(self):
        raw=json.loads(body(self.qs,{"a":1,"b":0,"c":0}))
        del raw["answers"]["where"]
        with self.assertRaises(jev.JevError):
            jev.validate_answers(json.dumps(raw).encode(),self.qs)

    def test_one_invalid_choice_rejects_otherwise_valid_noul(self):
        raw=body(self.qs,{"a":0.2,"b":0.5,"c":0.3},chosen="a")
        with self.assertRaises(jev.JevError) as caught:
            jev.validate_answers(raw,self.qs)
        self.assertEqual(caught.exception.code,"choice_argmax_mismatch")
        self.assertIsNone(caught.exception.__context__)

    def test_distribution_key_drift_and_invalid_value_types(self):
        for probs in ({"a":1,"b":0}, {"a":1,"b":0,"c":0,"extra":0},
                      {"a":True,"b":0,"c":0}, {"a":"1","b":0,"c":0},
                      {"a":1.01,"b":0,"c":0}, {"a":1,"b":None,"c":0}):
            with self.subTest(probs=probs),self.assertRaises(jev.JevError):
                jev.validate_answers(body(self.qs,probs),self.qs)

    def test_confidence_is_bounded_and_fields_are_exact(self):
        for confidence in (-0.1,1.1,True,"0.9",None):
            with self.subTest(confidence=confidence),self.assertRaises(jev.JevError):
                jev.validate_answers(body(self.qs,{"a":1,"b":0,"c":0},
                                          confidence=confidence),self.qs)
        raw=json.loads(body(self.qs,{"a":1,"b":0,"c":0}))
        raw["answers"]["where"]["reason"]="not permitted"
        with self.assertRaises(jev.JevError):
            jev.validate_answers(json.dumps(raw).encode(),self.qs)

    def test_explicit_strict_rejects_but_default_accepts_two_decimal_sum_099(self):
        raw=body(self.qs,{"a":0.33,"b":0.33,"c":0.33})
        with self.assertRaises(jev.JevError):
            jev.validate_answers(raw,self.qs,rounding_places=None)
        answers,_=jev.validate_answers(raw,self.qs)
        self.assertEqual(sum(answers["where"]["probabilities"].values()),Decimal("0.99"))

    def test_strict_allows_only_documented_serialization_slack(self):
        raw=body(self.qs,{"a":0.3333333333333333,"b":0.3333333333333333,
                          "c":0.3333333333333333})
        jev.validate_answers(raw,self.qs,rounding_places=None)
        with self.assertRaises(jev.JevError):
            jev.validate_answers(body(self.qs,{"a":0.333333,"b":0.333333,"c":0.333333}),self.qs,rounding_places=None)

    def test_rounded_distribution_infeasible_102_counterexample(self):
        qs={"where":{"type":"choice","instructions":"Choose.",
                     "criteria":{k:k for k in "abcd"}}}
        with self.assertRaises(jev.JevError):
            jev.validate_answers(body(qs,{"a":1,"b":0.01,"c":0.01,"d":0}),
                                 qs,rounding_places=2)

    def test_profile_parameter_validation_unchanged(self):
        raw=body(self.qs,{"a":0.334,"b":0.333,"c":0.333})
        jev.validate_answers(raw,self.qs)
        for places in (True,1,9,2.0,"2"):
            with self.subTest(places=places),self.assertRaises(jev.JevError):
                jev.validate_answers(raw,self.qs,rounding_places=places)

    def test_off_grid_values_accepted_under_rounding_tolerance(self):
        # Fix c (2026-09-25, owner): values are exact assertions; the
        # quantum is a comparison tolerance, not a format contract. Live
        # evidence: 14/14 rejected batches were off-grid-only.
        answers,_=jev.validate_answers(
            body(self.qs,{"a":0.334,"b":0.333,"c":0.333}),
            self.qs,rounding_places=2)
        self.assertIsNotNone(answers)

    def test_rounding_profile_still_enforces_mass_window(self):
        with self.assertRaises(jev.JevError):
            jev.validate_answers(body(self.qs,{"a":0.5,"b":0.3,"c":0.1}),
                                 self.qs,rounding_places=2)

    def test_choice_argmax_displayed_tie_accepted(self):
        # A displayed tie (0.43/0.43) is the only near-tie that monotone
        # rounding can produce; exact equality accepts it in both
        # profiles.
        jev.validate_answers(body(self.qs,{"a":0.43,"b":0.43,"c":0.14}),
                             self.qs,rounding_places=2)
        jev.validate_answers(body(self.qs,{"a":0.43,"b":0.43,"c":0.14}),
                             self.qs,rounding_places=None)

    def test_choice_argmax_mismatch_rejected_with_own_code(self):
        # Provider inconsistency, not a rounding artifact: monotone
        # rounding cannot reverse order, so even a one-quantum gap is
        # rejected and reported under its own code.
        for places in (2, None):
            for probs in ({"a":0.42,"b":0.43,"c":0.15},
                          {"a":0.41,"b":0.43,"c":0.16}):
                with self.subTest(places=places, probs=probs):
                    with self.assertRaises(jev.JevError) as ctx:
                        jev.validate_answers(body(self.qs,probs),
                                             self.qs,rounding_places=places)
                    self.assertEqual(ctx.exception.code,
                                     "choice_argmax_mismatch")

    def test_255_rounded_zero_options_are_feasible_but_uninformative(self):
        qs={"where":{"type":"choice","instructions":"Choose.",
                     "criteria":{f"a{i}":str(i) for i in range(255)}}}
        probs={k:0 for k in qs["where"]["criteria"]}
        jev.validate_answers(body(qs,probs,chosen="a0",confidence=0),qs,rounding_places=2)
        with self.assertRaises(jev.JevError):
            jev.validate_answers(body(qs,probs,chosen="a0",confidence=0),qs,rounding_places=None)

    def test_fraction_validation_does_not_depend_on_decimal_precision(self):
        raw=body(self.qs,{"a":0.33,"b":0.33,"c":0.33})
        with localcontext() as ctx:
            ctx.prec=2
            jev.validate_answers(raw,self.qs,rounding_places=2)

    def test_question_shapes_counts_types_and_option_caps(self):
        for qs in ({}, {str(i):self.qs["exists"] for i in range(65)},
                   {"bad id":self.qs["exists"]}, {"x":{"type":"score",
                    "instructions":"Rate","criteria":["low","high"]}},
                   {"x":{"type":"choice","instructions":"Choose","criteria":{"only":"One"}}}):
            with self.subTest(qs=list(qs)[:2]), self.assertRaises(jev.JevError):
                jev.encode_request({},qs)
        qs={"x":{"type":"choice","instructions":"Choose.",
                 "criteria":{f"o{i}":str(i) for i in range(256)}}}
        with self.assertRaises(jev.JevError):
            jev.encode_request({},qs)

    def test_unknown_pin_envelope_diagnostics_and_extra_answers_rejected(self):
        for change in ("pin","diagnostics","extra_answer","envelope"):
            raw=json.loads(body(self.qs,{"a":1,"b":0,"c":0}))
            if change=="pin":raw["model"]="jev-latest"
            if change=="diagnostics":raw["diagnostics"]={"truncated":1}
            if change=="extra_answer":raw["answers"]["extra"]={"type":"noul","noul":1}
            if change=="envelope":raw["message"]="changed"
            with self.subTest(change=change),self.assertRaises(jev.JevError):
                jev.validate_answers(json.dumps(raw).encode(),self.qs)

    def test_sensitive_criterion_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"TYPESAFE_API_KEY":KEY}):
            gateway=jev.HostedJev(td)
            self.qs["where"]["criteria"]["a"]=KEY
            with patch.object(jev.subprocess,"run") as run:
                with self.assertRaises(jev.JevError):
                    gateway.evaluate({},self.qs,metadata={})
                run.assert_not_called()
            self.assertNotIn(KEY,Path(gateway.last_receipt).read_text())

    def test_mutated_question_map_uses_immutable_sent_snapshot(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"TYPESAFE_API_KEY":KEY}):
            gateway=jev.HostedJev(td)
            def send(*args,**kwargs):
                sent=json.loads(kwargs["input"])
                self.qs.clear()
                return subprocess.CompletedProcess([],0,body(sent["questions"],
                    {"a":1,"b":0,"c":0}),b"")
            with patch.object(jev.subprocess,"run",side_effect=send):
                answer=gateway.evaluate({},self.qs,metadata={})
            self.assertEqual(set(answer),{"exists","where"})
            receipt=json.loads(Path(gateway.last_receipt).read_text())
            self.assertEqual(receipt["question_count"],2)
            self.assertEqual(receipt["rounding_places"],2)
            self.assertEqual(set(receipt["answers"]),{"exists","where"})


class SyntheticSweepTests(unittest.TestCase):
    def test_96_single_line_and_95_boundary_pair_positions(self):
        for width in (1,2):
            for lo in range(1,98-width):
                bug=(lo,lo+width-1)
                result=scan(source(96),FixtureProvider([bug]),Config(whole_file=False))
                self.assertIn(bug,ranges(result),(bug,ranges(result)))
                self.assertEqual(result["status"],"complete",bug)
                self.assertTrue(all(1<=a<=b<=96 for a,b in ranges(result)))

    def test_large_unlocalizable_relation_keeps_broad_interval(self):
        result=scan(source(48),FixtureProvider([(8,32)]),Config(whole_file=False))
        self.assertIn((1,48),ranges(result))
        self.assertFalse(any(b-a+1<25 for a,b in ranges(result)))

if __name__=="__main__":
    unittest.main()
