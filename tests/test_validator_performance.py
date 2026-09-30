"""Exact validation against rational arithmetic and independent wire encoding.

These exercise observable acceptance, rejection, atomicity and ownership; they
do not assert implementation timings on shared or unknown CI hardware.
"""
from decimal import (Decimal, Inexact, ROUND_DOWN, ROUND_UP, Rounded,
                     localcontext)
from fractions import Fraction
import json
import random
import unittest

from bug_hunter import jev


def choice_case(values, chosen=None):
    probabilities = {f"p{i}": value for i, value in enumerate(values)}
    if chosen is None:
        chosen = max(probabilities, key=probabilities.get)
    questions = {"where": {"type": "choice", "instructions": "Choose.",
                           "criteria": {key: key for key in probabilities}}}
    answers = {"where": {"type": "choice", "choice": chosen,
                         "probabilities": probabilities, "confidence": 1}}
    return questions, answers


def rational_mass(values, places):
    values = [Fraction(value) for value in values]
    if places is None:
        return abs(sum(values) - 1) <= Fraction(1, 10**12)
    half = Fraction(1, 2 * 10**places)
    return (sum(max(0, value - half) for value in values) <= 1
            <= sum(min(1, value + half) for value in values))


class ExactMassTests(unittest.TestCase):
    def assert_mass(self, values, places):
        questions, answers = choice_case(values)
        if rational_mass(values, places):
            result = jev.validate_answer_objects(answers, questions, places)
            self.assertEqual(list(result["where"]["probabilities"].values()), values)
        else:
            with self.assertRaises(jev.JevError) as caught:
                jev.validate_answer_objects(answers, questions, places)
            self.assertEqual(caught.exception.code, "response_distribution")

    def test_mass_boundaries_and_clipped_endpoints(self):
        cases = [
            [Decimal("0.5"), Decimal("0.500000000001")],
            [Decimal("0.5"), Decimal("0.50000000000100000000001")],
            [Decimal("0.5"), Decimal("0.499999999999")],
            [Decimal("0.5"), Decimal("0.49999999999899999999999")],
            [Decimal("0.33")] * 3,
            [Decimal("0.334"), Decimal("0.333"), Decimal("0.333")],
            [Decimal(1), Decimal("0.01"), Decimal("0.01"), Decimal(0)],
            [Decimal("0.995"), Decimal("0.005")],
            [Decimal("0.99"), Decimal(0)],
            [Decimal("0.99999999999999999999"), Decimal("1e-20")],
            [Decimal("1e-1000"), Decimal(1)],
            [Decimal("0E+1000"), Decimal("-0E-1000"), Decimal(1)],
            [Decimal(1)] + [Decimal("1e-1000")] * 254,
            [Decimal(0)] * 255,
            [Decimal(1)] * 255,
        ]
        for places in (None, *range(2, 9)):
            for values in cases:
                with self.subTest(places=places, width=len(values), first=str(values[0])):
                    self.assert_mass(values, places)

    def test_seeded_distributions_match_rational_oracle(self):
        rng = random.Random(923741)
        for index in range(240):
            width = rng.choice((2, 3, 6, 16, 64, 255))
            scale = rng.choice((2, 3, 8, 12, 25, 128, 1000))
            # Integer partitions yield exact unit mass without any Decimal
            # arithmetic or dependence on the test runner's context.
            total = 10 ** min(scale, 90)
            cuts = sorted(rng.randrange(total + 1) for _ in range(width - 1))
            pieces = [b - a for a, b in zip([0, *cuts], [*cuts, total])]
            values = [Decimal(f"{piece}e-{min(scale, 90)}") for piece in pieces]
            if index % 3 == 1:
                values[rng.randrange(width)] = Decimal(f"1e-{scale}")
            elif index % 3 == 2:
                values[rng.randrange(width)] = Decimal(rng.choice(("0", "1", "0.005")))
            places = rng.choice((None, *range(2, 9)))
            with self.subTest(index=index, places=places):
                self.assert_mass(values, places)

    def test_hostile_decimal_context_cannot_change_answers_or_flags(self):
        values = [Decimal(1)] + [Decimal("1e-1000")] * 254
        for rounding in (ROUND_DOWN, ROUND_UP):
            with localcontext() as context:
                context.prec = 1
                context.Emin, context.Emax = -1, 1
                context.rounding = rounding
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                context.clear_flags()
                before = dict(context.flags)
                for places in (None, 2, 8):
                    self.assert_mass(values, places)
                self.assertEqual(context.flags, before)

    def test_exact_argmax_precedes_mass_in_both_profiles(self):
        questions, answers = choice_case([Decimal("0.42"), Decimal("0.43")], "p0")
        for places in (None, 2, 8):
            with self.assertRaises(jev.JevError) as caught:
                jev.validate_answer_objects(answers, questions, places)
            self.assertEqual(caught.exception.code, "choice_argmax_mismatch")


class BoundaryOwnershipTests(unittest.TestCase):
    def test_owned_wire_matches_independent_canonical_encoding(self):
        rng = random.Random(404)
        for index in range(60):
            state = {"text": "\u6f22\"\\\r\n\t" * rng.randrange(250),
                     "nested": [{"n": rng.randrange(10**20), "v": None},
                                [True, False, -0.0, 1e-100]]}
            questions = {"q": {**jev.QUESTION, "criteria": dict(jev.QUESTION["criteria"])}}
            expected = json.dumps({"model": jev.MODEL, "state": state,
                                   "questions": questions}, ensure_ascii=False,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8")
            wire = jev.encode_request(state, questions)
            self.assertEqual(wire, expected, index)
            state["nested"][0]["n"] = -1
            questions["q"]["criteria"]["true"] = "changed"
            self.assertEqual(wire, expected, index)

    def test_batch_failure_has_no_answers_but_keeps_validated_usage(self):
        questions, answers = choice_case([0.6, 0.4])
        questions["last"] = jev.QUESTION
        answers["last"] = {"type": "noul", "noul": "0.9"}
        raw = json.dumps({"model": jev.MODEL, "answers": answers,
                          "usage": {"input_tokens": 71, "output_tokens": 9}}).encode()
        usage = {}
        with self.assertRaises(jev.JevError) as caught:
            jev.validate_answers(raw, questions, usage_out=usage)
        self.assertEqual(caught.exception.code, "response_probability")
        self.assertEqual(usage, {"input_tokens": 71, "output_tokens": 9})
        self.assertIsNone(caught.exception.__context__)

    def test_answers_share_no_mutable_containers_with_caller(self):
        questions, answers = choice_case([Decimal("0.6"), Decimal("0.4")])
        result = jev.validate_answer_objects(answers, questions)
        answers["where"]["probabilities"]["p0"] = Decimal("0.9")
        questions["where"]["criteria"].clear()
        self.assertEqual(result["where"]["probabilities"]["p0"], Decimal("0.6"))
        result["where"]["probabilities"]["p1"] = Decimal("0.1")
        self.assertEqual(answers["where"]["probabilities"]["p1"], Decimal("0.4"))

    def test_hosted_and_object_gateway_profiles_have_same_results(self):
        for values in ([0.33] * 3, [0.334, 0.333, 0.333], [0.99, 0],
                       [1, 0.01, 0.01, 0], [0] * 255, [0.43, 0.43, 0.14]):
            questions, answers = choice_case(values)
            raw = json.dumps({"model": jev.MODEL, "answers": answers,
                              "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
            for places in (None, 2, 8):
                outcomes = []
                for function in (lambda: jev.validate_answer_objects(answers, questions, places),
                                 lambda: jev.validate_answers(raw, questions, rounding_places=places)[0]):
                    try:
                        outcomes.append(jev.plain(function()))
                    except jev.JevError as error:
                        outcomes.append(error.code)
                self.assertEqual(outcomes[0], outcomes[1])


if __name__ == "__main__":
    unittest.main()
