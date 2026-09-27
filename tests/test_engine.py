"""Adversarial search-mechanics tests using an explicit synthetic provider."""
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from bug_hunter.core import Config, Span, format_findings
from bug_hunter import jev
from bug_hunter.questions import load_pack, screening, state_for, pack_questions
from tests.support import FixtureProvider, source, ranges, scan


class EngineTests(unittest.TestCase):
    def run_case(self, n=48, bugs=(), provider=None, text=None, **options):
        provider = provider or FixtureProvider(bugs)
        result = scan(source(n, text), provider, Config(whole_file=False, **options))
        return result, provider

    def test_cold_root_still_examines_all_scales(self):
        result, provider = self.run_case()
        self.assertGreaterEqual(result["calls_attempted"], 2)
        # 11 targets x (general screen + five bug lenses), plus context + Choice.
        self.assertEqual(result["questions_attempted"], 68)
        self.assertEqual(len(result["windows"]), 11)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["findings"])
        self.assertEqual({w["end_line"]-w["start_line"]+1 for w in result["windows"]},
                         {48,24,12})
        self.assertEqual({q["type"] for req in provider.requests
                          for q in req["questions"].values()}, {"noul", "choice"})

    def test_low_parent_does_not_hide_high_child(self):
        def override(qid,q,state,target):
            if qid.startswith(("screen_", "lens_")) and target.size > 12:
                return {"type":"noul","noul":0.05}
        result, _ = self.run_case(bugs=[(17,18)],
            provider=FixtureProvider([(17,18)],override=override))
        self.assertIn((17,18), ranges(result))
        parent = next(w for w in result["windows"] if w["window_id"] == "L1-48")
        self.assertEqual(parent["score"], "0.05")
        self.assertEqual(result["status"],"complete")

    def test_multiple_disjoint_bugs_survive_choice(self):
        result, _ = self.run_case(96, [(17,18),(64,65)])
        self.assertEqual(ranges(result), {(17,18),(64,65)})
        self.assertEqual(result["status"],"complete")
        self.assertEqual(format_findings(result),
            "Bug suspected between lines 17 and 18.\nBug suspected between lines 64 and 65.\n")

    def test_multiple_bugs_inside_one_terminal_region(self):
        result, _ = self.run_case(12,[(2,2),(9,9)])
        self.assertEqual(ranges(result), {(2,2),(9,9)})

    def test_boundary_crossing_is_visible_through_offset(self):
        result, _ = self.run_case(96,[(48,49)])
        self.assertIn((48,49),ranges(result))
        self.assertTrue(any(w["start_line"]==25 and w["end_line"]==72
                            for w in result["windows"]))

    def test_parent_retained_when_only_parent_high(self):
        def override(qid,q,state,target):
            if q["type"]=="noul" and qid.startswith(("screen_","verify_")):
                return {"type":"noul","noul":0.95 if target.size==48 else 0.05}
        result,_=self.run_case(provider=FixtureProvider(override=override))
        self.assertEqual(ranges(result),{(1,48)})

    def test_winner_does_not_establish_bug_existence(self):
        def override(qid,q,state,target):
            if q["type"]=="choice":
                key=next(k for k in q["criteria"] if k not in {"none","unlocalized"})
                return {"type":"choice","choice":key,"confidence":1,
                        "probabilities":{k:int(k==key) for k in q["criteria"]}}
        result,_=self.run_case(provider=FixtureProvider(override=override))
        self.assertFalse(result["findings"])
        self.assertEqual(result["status"],"complete")

    def test_false_narrow_proposal_needs_direct_noul(self):
        def override(qid,q,state,target):
            if qid=="where":
                key=next(k for k in q["criteria"] if k.startswith("L"))
                return {"type":"choice","choice":key,"confidence":1,
                        "probabilities":{k:int(k==key) for k in q["criteria"]}}
        result,_=self.run_case(12,provider=FixtureProvider([(9,9)],override=override))
        self.assertNotIn((1,1),ranges(result))
        self.assertIn((1,12),ranges(result))

    def test_low_choice_confidence_retains_broader_suspicion(self):
        def override(qid,q,state,target):
            if qid=="where":
                return {"type":"choice","choice":"none","confidence":0,
                        "probabilities":{k:int(k=="none") for k in q["criteria"]}}
        result,_=self.run_case(12,provider=FixtureProvider([(9,9)],override=override))
        self.assertEqual(ranges(result),{(1,12)})

    def test_failed_final_check_preserves_initial_high(self):
        def override(qid,q,state,target):
            if qid.startswith("verify_"):
                raise jev.JevError("transport_failed")
        result,_=self.run_case(12,provider=FixtureProvider([(9,9)],override=override))
        self.assertIn((1,12),ranges(result))
        self.assertNotIn((9,9),ranges(result))
        self.assertEqual(result["status"],"incomplete")

    def test_disagreeing_direct_check_does_not_silently_close_parent(self):
        def override(qid,q,state,target):
            if qid.startswith("verify_") and target.size==12:
                return {"type":"noul","noul":0.05}
        result,_=self.run_case(12,provider=FixtureProvider([(9,9)],override=override))
        self.assertIn((1,12),ranges(result))
        self.assertIn((9,9),ranges(result))

    def test_failed_covered_check_does_not_remove_parent(self):
        def override(qid,q,state,target):
            if qid=="covered":
                return {"type":"noul","noul":0.05}
        result,_=self.run_case(12,provider=FixtureProvider([(9,9)],override=override))
        self.assertEqual(ranges(result),{(1,12),(9,9)})

    def test_hard_call_budget_retains_validated_initial_findings(self):
        result,provider=self.run_case(96,[(17,18),(64,65)],max_calls=1)
        self.assertEqual(len(provider.requests),1)
        self.assertEqual(result["calls_attempted"],1)
        self.assertEqual(result["status"],"incomplete")
        self.assertIn((1,48),ranges(result))
        self.assertTrue(result["coverage"]["unassessed_ranges"])

    def test_question_budget_is_not_request_budget(self):
        result,provider=self.run_case(max_questions=12)
        self.assertEqual(len(provider.requests),0)
        self.assertEqual(result["status"],"incomplete")
        self.assertEqual(result["questions_attempted"],0)
        self.assertTrue(any(i["code"]=="question_budget_exhausted" for i in result["issues"]))

    def test_window_planning_cap_is_explicit(self):
        result,_=self.run_case(96,max_windows=5)
        self.assertLessEqual(len(result["windows"]),5)
        self.assertEqual(result["status"],"incomplete")

    def test_empty_file_needs_no_gateway_call(self):
        result,provider=self.run_case(0)
        self.assertFalse(provider.requests)
        self.assertEqual(result["status"],"complete")

    def test_whole_file_score_does_not_gate_root_plans(self):
        provider=FixtureProvider()
        result=scan(source(96),provider,Config())
        self.assertEqual(len(result["windows"]),34)
        self.assertEqual(result["coverage"]["assessed_lines"],96)

    def test_oversized_target_splits_without_truncating_lines(self):
        result,provider=self.run_case(16,text="\u5b57"*500,max_depth=0)
        self.assertEqual(result["status"],"complete")
        self.assertTrue(provider.requests)
        for req in provider.requests:
            for e in req["state"]["excerpts"]:
                self.assertTrue(all(line["text"]=="\u5b57"*500 for line in e["lines"]))
        self.assertEqual(result["coverage"]["assessed_lines"],16)

    def test_single_oversized_line_is_unreviewed(self):
        result,provider=self.run_case(1,text="\u5b57"*10000)
        self.assertFalse(provider.requests)
        self.assertEqual(result["status"],"incomplete")
        self.assertEqual(result["coverage"]["unassessed_ranges"],[[1,1]])

    def test_context_selection_sees_packet_text_then_rescores(self):
        provider=FixtureProvider([(5,5)],need_packet=100)
        result,_=self.run_case(120,provider=provider)
        self.assertTrue(any(e["phase"]=="context_selected" and e["packets"]
                            for e in result["events"]))
        self.assertTrue(any(w["phase"]=="enriched" for w in result["windows"]))
        self.assertEqual(result["status"],"complete")
        self.assertIn((5,5),ranges(result))

    def test_missing_context_not_found_means_incomplete(self):
        provider=FixtureProvider([(5,5)],need_packet=1000)
        result,_=self.run_case(48,provider=provider)
        self.assertEqual(result["status"],"incomplete")
        self.assertTrue(any(i["code"]=="context_unresolved" for i in result["issues"]))

    def test_context_round_is_bounded_and_disabled_zero_is_visible(self):
        result,_=self.run_case(120,provider=FixtureProvider([(5,5)],need_packet=100),
                               max_context_packets=0)
        self.assertEqual(result["status"],"incomplete")
        self.assertFalse(any(w["phase"]=="enriched" for w in result["windows"]))

    def test_no_scores_or_findings_in_model_state(self):
        result,provider=self.run_case(12,[(5,5)])
        for request in provider.requests:
            state=request["state"]
            self.assertTrue({"source_sha256","file","total_lines",
                             "excerpts","specification","context_scope"} <= set(state))
            self.assertNotIn("score",state)
            self.assertNotIn("support",state)
            for outcome in state.get("investigation", {}).get("outcomes", []):
                self.assertNotIn("score", outcome)
        for f in result["findings"]:
            self.assertNotIn("why",f)
            self.assertNotIn("bug_type",f)

    def test_specification_is_kept_verbatim_and_inferred_flag_explicit(self):
        provider=FixtureProvider()
        report=scan(source(2),provider,Config(),"Exact contract.\nKeep order.")
        self.assertEqual(report["specification_mode"],"provided")
        self.assertEqual([r["text"] for r in provider.requests[0]["state"]["specification"]["lines"]],
                         ["Exact contract.", "Keep order."])
        with self.assertRaises(ValueError):
            scan(source(2),provider,Config()," ")
        with self.assertRaises(ValueError):
            scan(source(2),provider,Config(),"x"*8193)

    def test_batch_omission_or_failure_cannot_become_clean(self):
        class Broken:
            def evaluate(self,state,questions,**kwargs):
                return {}
        report=scan(source(48),Broken(),Config(whole_file=False))
        self.assertEqual(report["status"],"incomplete")
        self.assertFalse(report["findings"])

    def test_localization_cap_preserves_other_unlocalized_suspicions(self):
        result,_=self.run_case(12,[(2,2),(9,9)],max_localizations=1)
        self.assertIn((2,2),ranges(result))
        self.assertIn((1,12),ranges(result))

    def test_question_packer_splits_count_and_never_drops_ids(self):
        pack,_=load_pack()
        state=state_for(source(1),Span(1,1),0,None)
        questions={f"q{i}": {"type":"noul","instructions":"Question?",
                            "criteria":{"true":"Yes","false":"No"}} for i in range(145)}
        batches=list(pack_questions(state,questions))
        self.assertTrue(all(len(b)<=64 and failed is None for b,failed in batches))
        self.assertEqual([k for b,_ in batches for k in b],list(questions))

    def test_target_bounds_are_in_each_instruction_not_just_question_id(self):
        pack,_=load_pack()
        qs=screening(pack,Span(1,48),[Span(13,24)])
        self.assertIn("inclusive original lines 13 through 24",qs["screen_L13-24"]["instructions"])

if __name__=="__main__":
    unittest.main()
