"""Regressions for faults observed in the default-budget live project smoke."""
from decimal import Decimal
import unittest

from bug_hunter.core import Config, Source, Span
from bug_hunter.engine import Search
from bug_hunter.evidence import EvidenceRef
from bug_hunter.jev import JevError
from bug_hunter.repository import ProjectEntry, ProjectIndex
from tests.support import FixtureProvider, ranges, scan


def inputs():
    primary=Source.from_text('from pricing import unit_price\n\ndef quote(quantity):\n    return quantity * unit_price()\n', 'quote.py')
    helper=Source.from_text('def unit_price():\n    return 21\n','pricing.py')
    spec='quote must charge exactly 20 per item. unit_price supplies the per-item price.\n'
    project=ProjectIndex('synthetic',(ProjectEntry('pricing.py',helper),),1,1,helper.byte_count,
                         False,0,0,0,False,False)
    return primary,spec,project


def provider(*,context_after=0.05,relation=0.86,verification=0.84,warm=False):
    def override(qid,q,state,target):
        if qid.startswith('screen_'):
            return {'type':'noul','noul':0.44}
        if qid.startswith('lens_'):
            return {'type':'noul','noul':0.65 if warm else 0.53}
        if qid=='need_context':
            return {'type':'noul','noul':context_after if state.get('related_sources') else 0.78}
        if qid.startswith('evidence_'):
            return {'type':'noul','noul':0.79}
        if qid.startswith('relation_'):
            return {'type':'noul','noul':relation}
        if qid.startswith('verify_evidence_'):
            return {'type':'noul','noul':verification}
        if qid.startswith('verify_'):
            return {'type':'noul','noul':0.05}
    return FixtureProvider(override=override)


class AutonomousProjectTests(unittest.TestCase):
    def run_case(self,gw):
        primary,spec,project=inputs()
        report=scan(primary,gw,Config(),spec=spec,project=project,context_scope='project')
        return report

    def test_cold_relationship_target_enters_real_action_loop_with_evidence(self):
        gw=provider()
        report=self.run_case(gw)
        self.assertEqual(report['windows'][0]['score'],'0.53')
        self.assertIn((1,4),ranges(report))
        self.assertEqual([a['action'] for a in report['jev_actions']],['finish'])
        state=next(r['state'] for r in gw.requests if 'action' in r['questions'])
        self.assertEqual(state['investigation_evidence'][0]['file'],'pricing.py')
        self.assertTrue(report['jev_actions'][0]['executed'])

    def test_fresh_context_answer_resolves_only_missing_context_limitation(self):
        report=self.run_case(provider())
        issue=next(i for i in report['issues'] if i['code']=='context_unresolved')
        self.assertFalse(issue['affects_completion'])
        event=next(e for e in report['events'] if e['phase']=='context_reassessed')
        self.assertTrue(event['resolved'])
        self.assertEqual(event['score'],'0.05')
        self.assertTrue(any(r['phase']=='context-reassessment' and
                            r['state_sha256']==event['state_sha256'] for r in report['requests']))
        self.assertEqual(report['status'],'complete')

    def test_still_missing_context_stays_incomplete(self):
        report=self.run_case(provider(context_after=0.9))
        self.assertEqual(report['status'],'incomplete')
        self.assertTrue(any(i['code']=='context_unresolved' and i['affects_completion'] for i in report['issues']))
        self.assertEqual(len([r for r in report['requests'] if r['phase']=='context-reassessment']),1)

    def test_failed_context_answer_never_clears_limitation_or_retries_unchanged_state(self):
        class BrokenContext:
            def __init__(self):
                self.delegate=provider()
                self.context_calls=0
            def evaluate(self,state,questions,*,metadata):
                if metadata['phase']=='context-reassessment':
                    self.context_calls+=1
                    raise JevError('response_schema')
                return self.delegate.evaluate(state,questions,metadata=metadata)
        gw=BrokenContext()
        report=self.run_case(gw)
        self.assertEqual(gw.context_calls,1)
        self.assertEqual(report['status'],'incomplete')
        self.assertTrue(any(i['code']=='context_unresolved' and i['affects_completion'] for i in report['issues']))
        event=next(e for e in report['events'] if e['phase']=='context_reassessed')
        self.assertIsNone(event['score'])
        self.assertFalse(event['resolved'])

    def test_context_resolution_does_not_clear_contrary_recheck_or_promote(self):
        report=self.run_case(provider(warm=True))
        self.assertFalse(report['findings'])
        self.assertTrue(any(r['score']=='0.05' for r in report['rechecks']))
        self.assertTrue(any(e['phase']=='context_reassessed' and e['resolved'] for e in report['events']))

    def test_subthreshold_relationship_verification_does_not_arm_cold_target(self):
        report=self.run_case(provider(verification=0.59))
        self.assertFalse(report['jev_actions'])
        self.assertFalse(report['findings'])

    def test_context_resolution_preserves_unrelated_affecting_issues(self):
        primary,spec,project=inputs()
        search=Search(primary,provider(),Config(),spec,project,context_scope='project')
        search.issue('project_index_limited',Span(1,4),'project-index')
        report=search.run()
        self.assertEqual(report['status'],'incomplete')
        self.assertTrue(any(i['code']=='project_index_limited' and i['affects_completion'] for i in report['issues']))
        self.assertTrue(any(i['code']=='context_unresolved' and not i['affects_completion'] for i in report['issues']))

    def test_context_resolution_does_not_clear_a_different_region(self):
        primary=Source.from_text('\n'.join('line' for _ in range(10)),'primary.py')
        helper=Source.from_text('price = 21\n','pricing.py')
        gw=provider()
        search=Search(primary,gw,Config(),None)
        for region in (Span(1,4),Span(7,10)):
            search.groups.append({'id':region.id,'region':region,'phase':'screen',
                                  'state':search.state_for(region)})
            search.issue('context_unresolved',region,'context')
        ref=EvidenceRef(helper,'pricing.py',Span(1,1),'project')
        search.evidence_by_target[Span(1,4)]={ref.id:{'ref':ref,'score':Decimal('0.79')}}
        search.reassess_context_with_evidence()
        issues={tuple(i['range']):i for i in search.issues}
        self.assertFalse(issues[(1,4)]['affects_completion'])
        self.assertTrue(issues[(7,10)]['affects_completion'])
        self.assertEqual(len(gw.requests),1)

    def test_oversized_combined_context_keeps_unknown_without_submission(self):
        primary=Source.from_text('\n'.join('x'*2400 for _ in range(4)),'primary.py')
        helper=Source.from_text('y'*10000,'pricing.py')
        gw=provider()
        search=Search(primary,gw,Config(),None)
        region=Span(1,4)
        search.groups.append({'id':'base','region':region,'phase':'screen','state':search.state_for(region)})
        search.issue('context_unresolved',region,'context')
        ref=EvidenceRef(helper,'pricing.py',Span(1,1),'project')
        search.evidence_by_target[Span(1,1)]={ref.id:{'ref':ref,'score':Decimal('0.79')}}
        search.reassess_context_with_evidence()
        self.assertFalse(gw.requests)
        self.assertTrue(any(i['code']=='context_unresolved' and i['affects_completion'] for i in search.issues))
        self.assertTrue(any(i['code']=='context_packet_too_large' for i in search.issues))

    def test_visible_evidence_does_not_use_up_slots_for_new_context(self):
        primary,spec,project=inputs()
        gw=provider()
        search=Search(primary,gw,Config(evidence_beam_width=1),spec,project,context_scope='project')
        region=Span(1,4)
        search.groups.append({'id':'base','region':region,'phase':'screen','state':search.state_for(region)})
        search.issue('context_unresolved',region,'context')
        visible=EvidenceRef(primary,primary.name,Span(4,4),'source')
        new=EvidenceRef(project.entries[0].source,'pricing.py',Span(1,2),'project')
        search.evidence_by_target[region]={visible.id:{'ref':visible,'score':Decimal('0.99')},
                                           new.id:{'ref':new,'score':Decimal('0.79')}}
        search.reassess_context_with_evidence()
        self.assertEqual(len(gw.requests),1)
        self.assertEqual(gw.requests[0]['state']['related_sources'][0]['file'],'pricing.py')
        self.assertFalse(search.issues[0]['affects_completion'])


if __name__=='__main__':
    unittest.main()
