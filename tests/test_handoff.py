"""Offline handoff tests. Fixture judgments are assigned, never live inference."""
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from bug_hunter import __main__ as cli, jev
from bug_hunter.core import Source, Span, Config
from bug_hunter.engine import Search
from bug_hunter.handoff import (assessment, passages, format_handoff,
                               source_candidates, capped)
from tests.support import FixtureProvider, KEY, source, ranges, scan

FIXTURE = ('def helper(values):\n'
           '    return [float(v) for v in values]\n\n'
           'def process(values):\n'
           '    result = []\n'
           '    # Synthetic test text; never executed.\n'
           '    for value in values:\n'
           '        result.append(value)\n'
           '    return result\n\n'
           '# End of synthetic fixture.\n'
           '# No real detection is measured.\n')
SPEC = ('Requirements\n\nKeep order.\n\nReject empty input.\n'
        'Raise ValueError for empty input.\n\nKeep output length.\n')


def selected_references(qid, question, state, target):
    if qid.startswith('read_with_'):
        return {'type':'noul', 'noul':0.95 if qid=='read_with_L1-2' else 0.05}
    if qid.startswith('requirement_'):
        return {'type':'noul', 'noul':0.95 if qid=='requirement_R5-6' else 0.05}


class AssessmentTests(unittest.TestCase):
    def fake(self, screens=(), checks=(), exists=()):
        return SimpleNamespace(
            cfg=Config(), observations=[{'start_line':a,'end_line':b,'score':v,
                                        'judgments': {'screen': v}}
                                       for a,b,v in screens],
            rechecks=[{'target':[a,b],'score':v} for a,b,v in checks],
            existence_checks=[{'target':[a,b],'score':v} for a,b,v in exists])

    def test_supported_requires_actual_qualifying_direct_recheck(self):
        s=self.fake([(8,9,'0.90')],[(8,9,'0.80')])
        self.assertEqual(assessment(s,Span(8,9)),'recheck-supported')

    def test_no_successful_recheck_is_not_rechecked(self):
        for checks in ([],[(8,9,None)]):
            s=self.fake([(8,9,'0.90')],checks)
            self.assertEqual(assessment(s,Span(8,9)),'not-rechecked')

    def test_contrary_same_target_screen_or_check_means_conflicting(self):
        for screens,checks in (([(8,9,'0.9'),(8,9,'0.79')],[(8,9,'0.9')]),
                               ([(8,9,'0.9')],[(8,9,'0.3')]),
                               ([(8,9,'0.9')],[(8,9,'0.9'),(8,9,'0.1')])):
            with self.subTest(screens=screens,checks=checks):
                self.assertEqual(assessment(self.fake(screens,checks),Span(8,9)),
                                 'conflicting')

    def test_different_broader_or_overlapping_target_is_not_conflict(self):
        s=self.fake([(1,48,'0.05'),(7,9,'0.01'),(8,9,'0.91')],
                    [(8,9,'0.88'),(8,10,'0.02')])
        self.assertEqual(assessment(s,Span(8,9)),'recheck-supported')

    def test_failure_is_unknown_not_a_negative_probability(self):
        s=self.fake([(8,9,'0.9')],[(8,9,'0.9'),(8,9,None)])
        self.assertEqual(assessment(s,Span(8,9)),'recheck-supported')

    def test_exact_target_existence_disagreement_is_not_hidden(self):
        s=self.fake([(8,9,'0.9')],[(8,9,'0.9')],[(8,9,'0.3')])
        self.assertEqual(assessment(s,Span(8,9)),'conflicting')


class HandoffTests(unittest.TestCase):
    def run_case(self, override=None, spec=SPEC, **config):
        provider=FixtureProvider([(9,9)],override=override)
        report=scan(Source.from_text(FIXTURE,'fixture.txt'),provider,
                    Config(whole_file=False,**config),spec)
        return report,provider

    def test_exact_reference_ids_are_selected_by_jev_not_invented(self):
        report,p=self.run_case(selected_references)
        f=report['handoff']['findings'][0]
        self.assertEqual(f['suspect'],[9,9])
        self.assertEqual(f['read_with']['ranges'],[[1,2]])
        self.assertEqual(f['relevant_requirement']['ranges'],[[5,6]])
        self.assertEqual(f['assessment'],'recheck-supported')
        self.assertEqual(report['status'],'complete')
        self.assertTrue(any(q.startswith('requirement_') for r in p.requests for q in r['questions']))

    def test_requirement_question_references_passage_and_spec_stays_in_state(self):
        _,p=self.run_case(selected_references)
        for r in p.requests:
            for qid,q in r['questions'].items():
                if qid=='requirement_R5-6':
                    # Untrusted spec text never enters instructions: the
                    # question references the passage by immutable coordinates.
                    self.assertIn('SPEC:R5-R6',q['instructions'])
                    self.assertNotIn('Reject empty input.',q['instructions'])
                    self.assertNotIn('Raise ValueError for empty input.',
                                     q['instructions'])
                    self.assertIn('L9-9',q['instructions'])
                    self.assertNotIn('text', r['state']['specification'])
                    numbered=[row['text'] for row in
                              r['state']['specification']['lines']]
                    self.assertIn('Reject empty input.',numbered)
                    self.assertIn('Raise ValueError for empty input.',numbered)

    def test_new_semantic_decisions_are_noul_only_batched(self):
        _,p=self.run_case(selected_references)
        batches=[r for r in p.requests if any(k.startswith(('read_with_','requirement_'))
                                              for k in r['questions'])]
        self.assertTrue(any(len(r['questions'])>1 for r in batches))
        self.assertTrue(all(q['type']=='noul' for r in batches for q in r['questions'].values()))

    def test_read_with_passage_is_visible_and_outside_suspected_target(self):
        _,p=self.run_case(selected_references)
        for r in p.requests:
            for qid in r['questions']:
                if qid.startswith('read_with_'):
                    a,b=map(int,qid.split('L')[1].split('-'))
                    visible={line['line'] for e in r['state']['excerpts'] for line in e['lines']}
                    self.assertTrue(set(range(a,b+1))<=visible)
                    self.assertNotIn(9,range(a,b+1))

    def test_no_spec_has_no_requirement_calls_or_fabricated_reference(self):
        report,p=self.run_case(spec=None)
        self.assertEqual(report['handoff']['findings'][0]['relevant_requirement']['status'],
                         'not-supplied')
        self.assertFalse(any(k.startswith('requirement_') for r in p.requests for k in r['questions']))
        self.assertIsNone(report['specification'])

    def test_no_qualified_reference_is_not_identified_not_random_winner(self):
        report,_=self.run_case()
        f=report['handoff']['findings'][0]
        for k in ('read_with','relevant_requirement'):
            self.assertEqual(f[k]['status'],'not-identified')
            self.assertEqual(f[k]['ranges'],[])

    def test_failed_handoff_keeps_findings_and_marks_unknown(self):
        def fail(qid,q,state,target):
            if qid.startswith(('read_with_','requirement_')):
                raise jev.JevError('transport_failed')
        report,_=self.run_case(fail)
        self.assertEqual(ranges(report),{(9,9)})
        self.assertEqual(report['scan_status'],'complete')
        self.assertEqual(report['handoff_status'],'incomplete')
        self.assertEqual(report['status'],'incomplete')
        f=report['handoff']['findings'][0]
        self.assertEqual(f['read_with']['status'],'unknown')
        self.assertEqual(f['relevant_requirement']['status'],'unknown')
        self.assertIn('evaluation-failed',f['limitations'])

    def test_handoff_budget_consumes_same_global_budget_without_erasing_findings(self):
        full,p=self.run_case()
        detection_calls=next(i for i,r in enumerate(p.requests)
                             if any(k.startswith('read_with_') for k in r['questions']))
        limited,_=self.run_case(max_calls=detection_calls)
        self.assertEqual(ranges(limited),ranges(full))
        self.assertEqual(limited['scan_status'],'complete')
        self.assertEqual(limited['handoff_status'],'incomplete')
        self.assertIn('budget-exhausted',limited['handoff']['findings'][0]['limitations'])

    def test_display_cap_reports_more_references_without_dropping_full_trace(self):
        def all_relevant(qid,q,state,target):
            if qid.startswith(('read_with_','requirement_')):
                return {'type':'noul','noul':0.95}
        report,_=self.run_case(all_relevant)
        f=report['handoff']['findings'][0]
        self.assertLessEqual(len(f['relevant_requirement']['ranges']),2)
        self.assertGreater(f['relevant_requirement']['additional_count'],0)
        self.assertGreater(len(report['findings'][0]['relevant_requirement']['all_ranges']),2)
        self.assertIn('more in report.json',format_handoff(report['handoff']))

    def test_candidate_cap_is_explicit_and_not_clean_completion(self):
        report,_=self.run_case(max_handoff_candidates=1)
        self.assertEqual(report['scan_status'],'complete')
        self.assertEqual(report['handoff_status'],'incomplete')
        self.assertIn('reference-pool-limited',report['handoff']['findings'][0]['limitations'])

    def test_compact_handoff_has_no_scores_excerpts_or_diagnoses(self):
        report,_=self.run_case(selected_references)
        text=format_handoff(report['handoff'])
        self.assertEqual(len(text.splitlines()),7)
        self.assertIn('SPEC:5-6',text)
        self.assertIn('read-with: 1-2',text)
        for forbidden in ('0.95','0.05','def process','Reject empty','confidence','why','severity'):
            self.assertNotIn(forbidden,text)
        raw=json.dumps(report['handoff'])
        self.assertNotIn('"score"',raw)
        self.assertNotIn('"support"',raw)
        self.assertTrue(report['handoff_evidence'])

    def test_same_target_conflict_is_visible_and_parent_reference_retained(self):
        def contrary(qid,q,state,target):
            if qid.startswith('verify_') and target==Span(1,12):
                return {'type':'noul','noul':0.05}
        report,_=self.run_case(contrary)
        parent=next(f for f in report['handoff']['findings'] if f['suspect']==[1,12])
        child=next(f for f in report['handoff']['findings'] if f['suspect']==[9,9])
        self.assertEqual(parent['assessment'],'conflicting')
        self.assertEqual(child['assessment'],'recheck-supported')
        self.assertEqual(child['unresolved_parents'],[[1,12]])

    def test_failed_recheck_not_rechecked_and_limited_not_false_conflict(self):
        def fail(qid,q,state,target):
            if qid.startswith('verify_'):
                raise jev.JevError('transport_failed')
        report,_=self.run_case(fail)
        f=report['handoff']['findings'][0]
        self.assertEqual(f['assessment'],'not-rechecked')
        self.assertIn('evaluation-failed',f['limitations'])

    def test_recorded_negative_evidence_is_retained(self):
        report,_=self.run_case()
        self.assertTrue(any(row['score']=='0.05' for row in report['handoff_evidence']))
        self.assertTrue(report['rechecks'])
        self.assertTrue(report['existence_checks'])

    def test_full_programmatic_spec_has_honest_inline_hash_basis(self):
        report,_=self.run_case(spec='One.\r\n\r\nTwo.\r\n')
        self.assertEqual(report['specification']['sha256'],
                         hashlib.sha256(b'One.\r\n\r\nTwo.\r\n').hexdigest())
        self.assertEqual(report['specification']['line_count'],3)
        self.assertEqual(report['specification']['hash_basis'],'utf8-supplied-text')

    def test_passage_coordinates_and_blank_line_handling(self):
        s=Source.from_text('one\r\n\r\ntwo\rlast\n')
        self.assertEqual(s.lines,('one','','two','last'))
        self.assertEqual(list(passages(s.lines,Span(1,4))),[Span(1,1),Span(3,4)])
        for n in range(1,65):
            lines=('x',)*n
            p=list(passages(lines,Span(1,n)))
            self.assertTrue(all(s.size<=8 for s in p))
            self.assertEqual(set(i for s in p for i in range(s.start,s.end+1)),set(range(1,n+1)))

    def test_inline_spec_limits_and_nul_fail_before_calls(self):
        for spec in ('x\n'*2001,'abc\x00def'):
            with self.subTest(spec=spec[:20]),self.assertRaises(ValueError):
                self.run_case(spec=spec)

    def test_path_control_characters_cannot_inject_extra_findings(self):
        report,_=self.run_case()
        report['handoff']['source']['path']='a\nF999 | suspect: 1-2\x1b[31m\u202e'
        text=format_handoff(report['handoff'])
        self.assertNotIn('\x1b',text)
        self.assertNotIn('\u202e',text)
        # The source path is never printed, so control characters in it
        # cannot inject extra findings lines at all.
        self.assertNotIn('F999', text)
        self.assertIn('FILE: "fixture.txt"', text)
        self.assertEqual(len(text.splitlines()),7)

    def test_empty_input_is_explicit_without_any_model_calls(self):
        p=FixtureProvider()
        report=scan(source(0),p)
        text=format_handoff(report['handoff'])
        self.assertIn('SCAN: complete',text)
        self.assertIn('No suspected ranges reported',text)
        self.assertFalse(p.requests)


class CompactCliTests(unittest.TestCase):
    def run_cli(self, root, fmt=None, override=selected_references, spec=True):
        path=root/'fixture.txt';path.write_text(FIXTURE,encoding='utf-8')
        req=root/'requirements.txt';req.write_bytes(b'\xef\xbb\xbf'+SPEC.replace('\n','\r\n').encode())
        argv=[str(path),'--no-whole-file','--scope','standalone',
              '--output-dir',str(root/'runs')]
        if spec: argv+=['--spec-file',str(req)]
        if fmt: argv+=['--output-format',fmt]
        provider=FixtureProvider([(9,9)],override=override)
        out,err=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,{'TYPESAFE_API_KEY':KEY}), \
             patch.object(socket,'create_connection',side_effect=AssertionError('no network')), \
             patch.object(jev.subprocess,'run',side_effect=provider.process), \
             redirect_stdout(out),redirect_stderr(err):
            code=cli.main(argv)
        results=list((root/'runs').glob('run-*/report.json'))
        self.assertTrue(results,err.getvalue())
        return code,out.getvalue(),json.loads(results[0].read_text()),results[0],provider

    def test_default_compact_and_saved_json_and_ranges_agree(self):
        with tempfile.TemporaryDirectory() as td:
            code,text,report,path,_=self.run_cli(Path(td))
            self.assertEqual(code,1)
            self.assertEqual(text,path.with_name('findings.txt').read_text())
            self.assertIn('F1 | suspect: 9-9 | assessment: recheck-supported',text)
            self.assertIn('read-with: 1-2 | relevant-requirement: SPEC:5-6',text)
            self.assertEqual(path.with_name('ranges.txt').read_text(),
                             'Bug suspected between lines 9 and 9.\n')
            self.assertEqual(json.loads(path.with_name('handoff.json').read_text()),report['handoff'])

    def test_raw_file_spec_hash_preserves_bom_crlf_and_original_coordinates(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_,_,report,_,_=self.run_cli(root)
            self.assertEqual(report['specification']['sha256'],
                             hashlib.sha256((root/'requirements.txt').read_bytes()).hexdigest())
            self.assertEqual(report['specification']['hash_basis'],'original-bytes')
            self.assertEqual(report['specification']['line_count'],8)

    def test_json_stdout_is_compact_and_matches_handoff_file(self):
        with tempfile.TemporaryDirectory() as td:
            code,text,report,path,_=self.run_cli(Path(td),'json')
            self.assertEqual(code,1)
            self.assertEqual(text,path.with_name('handoff.json').read_text())
            self.assertEqual(json.loads(text),report['handoff'])
            self.assertNotIn('"score"',text)

    def test_ranges_mode_is_exact_legacy_stdout_only(self):
        with tempfile.TemporaryDirectory() as td:
            code,text,report,path,_=self.run_cli(Path(td),'ranges')
            self.assertEqual((code,text),(1,'Bug suspected between lines 9 and 9.\n'))
            self.assertIn('FILE:',path.with_name('findings.txt').read_text())

    def test_malformed_reference_response_does_not_fabricate_refs(self):
        def malformed(qid,q,state,target):
            if qid.startswith(('read_with_','requirement_')):
                return {'type':'noul','noul':'0.95'}
        with tempfile.TemporaryDirectory() as td:
            code,text,report,path,_=self.run_cli(Path(td),override=malformed)
            self.assertEqual(code,2)
            self.assertEqual(report['scan_status'],'complete')
            self.assertIn('HANDOFF: incomplete',text)
            self.assertIn('read-with: unknown',text)
            self.assertIn('relevant-requirement: unknown',text)
            self.assertEqual(ranges(report),{(9,9)})

    def test_receipts_and_handoff_never_embed_source_spec_or_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            code,text,report,path,_=self.run_cli(Path(td))
            for f in path.parent.joinpath('receipts').glob('*.json'):
                saved=f.read_text()
                for token in ('Reject empty input.', 'def process', KEY):
                    self.assertNotIn(token,saved)
            for token in ('Reject empty input.','def process',KEY):
                self.assertNotIn(token,text)


if __name__=='__main__':
    unittest.main()
