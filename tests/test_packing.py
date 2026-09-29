"""Wire-equivalence oracle: original growing-batch algorithm versus fragments."""
import random
import unittest

from bug_hunter.jev import JevError, encode_request, MAX_QUESTIONS
from bug_hunter.questions import pack_questions


def reference_pack(state, questions):
    batch = {}
    for qid, question in questions.items():
        candidate = {**batch, qid: question}
        try:
            if len(candidate) > MAX_QUESTIONS:
                raise JevError("request_too_large")
            encode_request(state, candidate)
        except JevError as error:
            if error.code != "request_too_large":
                raise
            if batch:
                yield batch, None
                batch = {}
            try:
                encode_request(state, {qid: question})
            except JevError as inner:
                if inner.code != "request_too_large":
                    raise
                yield {}, qid
                continue
            batch = {qid: question}
        else:
            batch = candidate
    if batch:
        yield batch, None


def question(text="Judge this."):
    return {"type": "noul", "instructions": text, "criteria": {"true": "Yes", "false": "No"}}


class PackingTests(unittest.TestCase):
    def assert_same(self, state, questions):
        expected = list(reference_pack(state, questions))
        actual = list(pack_questions(state, questions))
        self.assertEqual(actual, expected)
        self.assertEqual([encode_request(state, b) if b else failure for b, failure in actual],
                         [encode_request(state, b) if b else failure for b, failure in expected])
        self.assertEqual([key for batch, failure in actual for key in (batch if batch else [failure])],
                         list(questions))

    def test_randomized_unicode_escaping_counts_and_byte_boundaries(self):
        rng = random.Random(20260929)
        for _ in range(80):
            state = {"evidence": rng.choice(("字", "\\\"\n", "text")) * rng.randrange(0, 5500)}
            qs = {f"q{i}": question(rng.choice(("word", "雪", "\\\"")) * rng.randrange(1, 400))
                  for i in range(rng.randrange(1, 140))}
            self.assert_same(state, qs)

    def test_exact_question_cap_and_oversized_singleton(self):
        for count in (1, 63, 64, 65, 128):
            self.assert_same({}, {f"q{i}": question() for i in range(count)})
        self.assert_same({"text": "a" * 15000}, {"too_large": question("b" * 11000), "fits": question()})
        self.assert_same({"text": "a" * 17000}, {"a": question(), "b": question()})

    def test_maximum_choice_menu_and_invalid_question_remain_validated(self):
        choice = {"type": "choice", "instructions": "Select.",
                  "criteria": {f"i{i}": "option " + str(i) for i in range(255)}}
        self.assert_same({"text": "a" * 10000}, {"menu": choice, "follow": question()})
        for q in ({"a": question("x" * 12001)}, {"bad id": question()}):
            for packer in (reference_pack, pack_questions):
                with self.assertRaises(JevError) as caught:
                    list(packer({}, q))
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_output_owns_question_snapshot(self):
        q = {"x": question()}
        batches = list(pack_questions({}, q))
        q["x"]["criteria"]["true"] = "mutated"
        self.assertEqual(batches[0][0]["x"]["criteria"]["true"], "Yes")


if __name__ == "__main__":
    unittest.main()
