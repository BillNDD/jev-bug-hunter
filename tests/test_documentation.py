"""Executable examples prevent drift between documentation and actual output."""
from pathlib import Path
import unittest

from bug_hunter.core import Config, Source
from bug_hunter.handoff import format_handoff
from tests.support import FixtureProvider, scan
from tests.test_handoff import FIXTURE, SPEC, selected_references


class DocumentationTests(unittest.TestCase):
    def test_readme_example_is_generated_by_current_formatter(self):
        report = scan(Source.from_text(FIXTURE, "fixture.txt"),
                      FixtureProvider([(9, 9)], override=selected_references),
                      Config(whole_file=False), SPEC)
        actual = format_handoff(report["handoff"])
        readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
        block = readme.split("<!-- generated-example:start -->", 1)[1].split("<!-- generated-example:end -->", 1)[0]
        expected = block.split("```text\n", 1)[1].split("```", 1)[0]
        self.assertEqual(actual, expected)

    def test_library_scope_contract_has_no_implicit_default(self):
        from bug_hunter.core import scan as public_scan
        with self.assertRaises(ValueError):
            public_scan(Source.from_text("x"), FixtureProvider())
        readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn("mandatory in both the CLI", readme)


if __name__ == "__main__":
    unittest.main()
