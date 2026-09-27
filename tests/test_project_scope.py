"""Project-scope contract pins.

The caller declares the scope; the tool never infers the project boundary,
and Jev never decides whether related files exist."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from bug_hunter import core
from bug_hunter.__main__ import main
from bug_hunter.repository import ProjectIndex
from tests.support import FixtureProvider, source


class TestScopeContract(unittest.TestCase):
    """CLI acceptance criteria from the fix, executed exactly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'target.py'
        self.target.write_text('def f(x):\n    return x / 0\n', encoding='utf-8')
        self.out = self.root / 'out'
        self._saved_key = os.environ.pop('TYPESAFE_API_KEY', None)
        self.addCleanup(self._restore_key)

    def _restore_key(self):
        if self._saved_key is not None:
            os.environ['TYPESAFE_API_KEY'] = self._saved_key

    def run_main(self, *extra):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main([str(self.target), '--output-dir', str(self.out),
                         *extra])
        return code, err.getvalue()

    def test_missing_scope_is_need_user_input(self):
        code, err = self.run_main()
        self.assertEqual(code, 3)
        self.assertIn('NEED_USER_INPUT project_scope', err)
        self.assertIn('standalone | isolate', err)

    def test_project_without_root_is_need_user_input(self):
        code, err = self.run_main('--scope', 'project')
        self.assertEqual(code, 3)
        self.assertIn('NEED_USER_INPUT project_root', err)

    def test_project_root_conflicts_with_non_project_scope(self):
        code, err = self.run_main('--scope', 'isolate',
                                  '--project-root', str(self.root))
        self.assertEqual(code, 3)
        self.assertIn('NEED_USER_INPUT conflicting_options', err)

    def test_project_root_must_exist(self):
        code, err = self.run_main('--scope', 'project',
                                  '--project-root', str(self.root / 'missing'))
        self.assertEqual(code, 2)
        self.assertIn('project_root_invalid', err)

    def test_library_scan_cannot_omit_scope(self):
        # The library API never infers scope: omission raises, loudly.
        with self.assertRaises(ValueError):
            core.scan(source(12), None)
        with self.assertRaises(ValueError):
            core.scan(source(12), None, context_scope="bogus")

    def test_three_modes_pass_scope_validation(self):
        # Without an API key each stops at the key gate, never at scope.
        cases = [('--scope', 'standalone'), ('--scope', 'isolate'),
                 ('--scope', 'project', '--project-root', str(self.root))]
        for extra in cases:
            with self.subTest(extra=extra):
                code, err = self.run_main(*extra)
                self.assertNotIn('NEED_USER_INPUT', err)
                self.assertNotIn('conflicting_options', err)
                self.assertEqual(code, 2)
                self.assertIn('missing_api_key', err)


class TestScopeCoherence(unittest.TestCase):
    def test_scan_rejects_incoherent_or_unknown_scope(self):
        src = source(4)
        with self.assertRaises(ValueError):
            core.scan(src, object(), context_scope='project')
        with self.assertRaises(ValueError):
            core.scan(src, object(), context_scope='bogus')
        with self.assertRaises(ValueError):
            core.scan(src, object(), project=object(),
                      context_scope='declared_standalone')


class TestScopeProvenance(unittest.TestCase):
    def test_scope_reaches_state_and_search_project_is_not_a_jev_choice(self):
        gw = FixtureProvider(bugs=[(2, 2)])
        src = source(12)
        result = core.scan(src, gw, context_scope='owner_isolated')
        self.assertEqual(result['context_scope'], 'owner_isolated')
        self.assertTrue(gw.requests)
        for request in gw.requests:
            self.assertEqual(request['state']['context_scope'],
                             'owner_isolated')
            for q in request['questions'].values():
                if q['type'] == 'choice':
                    self.assertNotIn('search_project', q['criteria'])

    def test_project_scope_runs_bounded_search_over_supplied_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'other.py').write_text('def g():\n    return 1\n',
                                           encoding='utf-8')
            idx = ProjectIndex.read(root)
            gw = FixtureProvider(bugs=[(2, 2)])
            src = source(12)
            result = core.scan(src, gw, project=idx, context_scope='project')
            self.assertEqual(result['context_scope'], 'project')
            related = json.dumps([r for req in gw.requests
                                  for r in req['state'].get('related_sources',
                                                            [])])
            self.assertIn('other.py', related)

    def test_cold_target_ignites_through_project_relationship(self):
        # The smoke scenario in miniature: screens cold in isolation; only
        # related evidence + relationship testing can create the suspicion.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pricing.py').write_text('percent is an integer 0-100\n' * 8,
                                             encoding='utf-8')
            idx = ProjectIndex.read(root)

            def override(qid, q, state, target):
                if qid.startswith(('evidence_', 'relation_', 'verify_evidence_')):
                    hit = any('pricing.py' in json.dumps(r)
                              for r in state.get('related_sources', []))
                    return {'type': 'noul', 'noul': 0.95 if hit else 0.05}
                return None

            gw = FixtureProvider(override=override)
            src = source(12)
            result = core.scan(src, gw, project=idx, context_scope='project')
            self.assertTrue(result['evidence_searches'])
            self.assertTrue(result['relationships'])
            self.assertTrue(result['findings'])
            origins = [row['origin'] for row in result['findings'][0]['support']]
            self.assertIn('relationship_recheck', origins)

    def test_cold_target_stays_cold_without_project_scope(self):
        def override(qid, q, state, target):
            if qid.startswith(('evidence_', 'relation_', 'verify_evidence_')):
                return {'type': 'noul', 'noul': 0.95}
            return None

        gw = FixtureProvider(override=override)
        result = core.scan(source(12), gw, context_scope='declared_standalone')
        self.assertFalse(result['evidence_searches'])
        self.assertFalse(result['findings'])


if __name__ == '__main__':
    unittest.main()
