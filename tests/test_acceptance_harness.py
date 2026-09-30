"""Offline tests of outcome gates and campaign safety, not claims about Jev accuracy."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from acceptance import __main__ as harness
from acceptance.catalog import CATALOG_REVISION, MIGRATION, cases, verify_witnesses, witness_in_domain
from acceptance.ledger import loads, new_budget, read, save, set_paused, transaction, validate_budget


class TrustedCatalogTests(unittest.TestCase):
    def test_all_authored_labels_have_trusted_executable_witnesses(self):
        catalogue = cases()
        self.assertEqual(len(catalogue), 34)
        self.assertEqual(sum(case["expected"] == "defect" for case in catalogue), 16)
        self.assertEqual(sum(case["expected"] == "clear" for case in catalogue), 16)
        for case in catalogue:
            with self.subTest(case=case["id"]):
                self.assertTrue(verify_witnesses(case)["verified"])

    def test_changed_source_or_helper_is_never_executed(self):
        for key in ("source", "helpers"):
            case = deepcopy(cases()[0])
            case[key] = "raise RuntimeError('untrusted')" if key == "source" else {"rogue.py": "raise RuntimeError('untrusted')"}
            with patch("builtins.exec", side_effect=AssertionError("arbitrary code executed")) as executor:
                with self.assertRaisesRegex(ValueError, "untrusted_witness_case"):
                    verify_witnesses(case)
                executor.assert_not_called()

    def test_old_numerical_counterexamples_are_outside_explicit_v2_domains(self):
        self.assertTrue(math.isinf(sum([1e308, 1e308]) / 2))
        with self.assertRaises(OverflowError):
            _ = 10**400 / 1000
        self.assertFalse(witness_in_domain("C006", [[1e308, 1e308]]))
        self.assertFalse(witness_in_domain("C006", [[True]]))
        self.assertFalse(witness_in_domain("C006", [[1] * 1001]))
        self.assertFalse(witness_in_domain("C018", [10**400]))
        self.assertFalse(witness_in_domain("C018", [float("inf")]))
        self.assertFalse(witness_in_domain("C018", [float("nan")]))
        self.assertFalse(witness_in_domain("C018", [True]))
        self.assertTrue(witness_in_domain("C006", [[-1000000, 1000000]]))
        self.assertTrue(witness_in_domain("C018", [1000000000000]))
        self.assertEqual(MIGRATION["historical_misses"], ["C005", "C011", "C013"])
        self.assertFalse(MIGRATION["historical_outcomes_reinterpreted"])


class FakeInstalledCLI:
    """Test-owned output writer; never imports or executes a target fixture."""
    def __init__(self, manifest, *, after_case=None, timeout=False, failed_receipt=False):
        self.cases = {case["id"]: case for case in manifest["cases"]}
        self.case_calls = []
        self.after_case = after_case
        self.timeout = timeout
        self.failed_receipt = failed_receipt
        self.version = "test-package"
        self.default_rounding = 2
        self.commands = []

    def __call__(self, command, **kwargs):
        if "-c" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({"version": self.version,
                "effective_choice_rounding_places": self.default_rounding,
                "module_sha256": {"__init__.py": "a" * 64}}).encode(), b"")
        self.commands.append(command)
        folder = Path(kwargs["cwd"])
        case = self.cases[folder.name]
        self.case_calls.append(case["id"])
        output = folder / "artifacts" / "run-fixture"
        (output / "receipts").mkdir(parents=True)
        save(output / "receipts" / "call_1.json", {"status": "dispatching" if self.timeout else
             "failed" if self.failed_receipt else "validated"})
        if self.timeout:
            raise subprocess.TimeoutExpired(command, 240, output=b"partial output", stderr=b"partial error")
        findings, rows = [], []
        for bug in case["bugs"]:
            lo, hi = bug["allowed_region"]
            findings.append({"start_line": lo, "end_line": hi})
            rows.append({"suspect": [lo, hi], "relevant_requirement": {"status": "selected",
                         "refs": [{"file": "SPEC", "range": [1, 1]}]},
                         "read_with": {"refs": ([{"file": case["citation"], "range": [1, 1],
                            "source_sha256": case["files_sha256"][case["citation"]]}] if case["citation"] else [])}})
        missing = case["expected"] == "unresolved"
        report = {"source": {"sha256": case["files_sha256"][case["name"]],
                              "line_count": case["line_count"]},
                  "status": "incomplete" if missing else "complete", "findings": findings,
                  "handoff": {"findings": rows}, "calls_attempted": 1,
                  "issues": [{"code": "context_unresolved", "affects_completion": True}] if missing else [],
                  "usage": {"input_tokens": 1, "output_tokens": 1}}
        specification = {"sha256": case["files_sha256"]["requirements.txt"],
                         "line_count": case.get("requirement_line_count", 1), "hash_basis": "original-bytes"}
        report["specification"] = specification
        report["handoff"].update(source=report["source"], specification=specification)
        save(output / "report.json", report)
        save(output / "handoff.json", report["handoff"])
        (output / "findings.txt").write_text("fixture output\n", encoding="utf-8")
        if self.after_case:
            self.after_case(case["id"])
        return subprocess.CompletedProcess(command, 2 if missing else int(bool(findings)),
                                           b"fixture output\n", b"")


class CampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog_folder = tempfile.TemporaryDirectory()
        cls.frozen = Path(cls.catalog_folder.name) / "frozen"
        # Durability is not what these logic tests claim to establish.
        with redirect_stdout(io.StringIO()), patch("acceptance.ledger.os.fsync"):
            harness.freeze(cls.frozen, 512)

    @classmethod
    def tearDownClass(cls):
        cls.catalog_folder.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "suite"
        shutil.copytree(self.frozen, self.root)
        self.manifest = harness.load_suite(self.root)
        self.environment = patch.dict(os.environ, {"TYPESAFE_API_KEY": "offline-placeholder-never-submitted"})
        self.environment.start()
        self.fsync = patch("acceptance.ledger.os.fsync")
        self.fsync.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.fsync.stop)

    def args(self, **changes):
        options = {"suite": self.root, "python": Path("unused-test-python"), "run": "trial",
                   "live": True, "case": ["C001", "C002"], "policy": None, "budget": None,
                   "resume": False, "continue_after_service_failure": False}
        options.update(changes)
        return SimpleNamespace(**options)

    def run_fake(self, fake, **changes):
        # Integrity/path checks have separate real-filesystem tests. Reusing
        # their validated manifest keeps accounting tests focused on dispatch.
        with patch("acceptance.__main__.subprocess.run", side_effect=fake), \
             patch("acceptance.__main__.load_suite", return_value=self.manifest), redirect_stdout(io.StringIO()):
            return harness.run_live(self.args(**changes))

    def test_frozen_v2_migration_and_trivial_hunters_are_rejected(self):
        self.assertEqual(self.manifest["schema"], "jev-outcome-acceptance-v2")
        self.assertEqual(self.manifest["catalog_revision"], CATALOG_REVISION)
        self.assertEqual(self.manifest["outcome_gate_revision"], harness.OUTCOME_GATE_REVISION)
        harness.assert_gate_discriminates(self.manifest)
        with patch("acceptance.__main__.judge_outcome", return_value={"passed": True}):
            with self.assertRaisesRegex(AssertionError, "acceptance gate permits trivial hunter"):
                harness.assert_gate_discriminates(self.manifest)
        for wanted in ("C005", "C011", "C013"):
            case = next(row for row in self.manifest["cases"] if row["id"] == wanted)
            report = {"source": {"sha256": case["files_sha256"][case["name"]], "line_count": case["line_count"]},
                      "status": "complete", "findings": [], "handoff": {"findings": []}}
            self.assertIn("missed_or_unhelpfully_broad_defect", harness.judge_outcome(case, report, 0)["failures"])

    def test_outcome_gate_requires_matching_findings_and_concrete_spec_evidence(self):
        # This is the compact ranges-only shape observed in the actual settled
        # C001 baseline handoff; SPEC identity is shared at document level.
        case = self.manifest["cases"][0]
        source = {"sha256": case["files_sha256"][case["name"]], "line_count": case["line_count"]}
        spec = {"sha256": case["files_sha256"]["requirements.txt"], "line_count": 1,
                "hash_basis": "original-bytes"}
        report = {"source": source, "specification": spec, "status": "complete",
                  "findings": [{"start_line": 1, "end_line": 2}],
                  "handoff": {"source": source, "specification": spec, "findings": [
                      {"suspect": [1, 2], "relevant_requirement": {"status": "selected", "ranges": [[1, 1]]}}]}}
        self.assertTrue(harness.judge_outcome(case, report, 1)["passed"])
        variants = []
        fabricated = deepcopy(report)
        fabricated["findings"] = []
        variants.append((fabricated, 0, "handoff_findings_disagree"))
        missing = deepcopy(report)
        missing["handoff"]["findings"][0]["relevant_requirement"] = {"status": "selected"}
        variants.append((missing, 1, "missing_requirement_citation"))
        for ref in ({"file": "SPEC", "range": [1, 2]},
                    {"file": "SPEC", "range": [1, 1], "source_sha256": "b" * 64}):
            invalid = deepcopy(report)
            invalid["handoff"]["findings"][0]["relevant_requirement"] = {"status": "selected", "refs": [ref]}
            variants.append((invalid, 1, "invalid_requirement_citation"))
        altered = deepcopy(report)
        altered["handoff"]["specification"]["sha256"] = "b" * 64
        variants.append((altered, 1, "wrong_requirement_snapshot"))
        for invalid, exit_code, failure in variants:
            outcome = harness.judge_outcome(case, invalid, exit_code)
            self.assertFalse(outcome["passed"])
            self.assertIn(failure, outcome["failures"])

    def test_manifest_rejects_traversal_absolute_reserved_and_duplicate_names(self):
        variants = []
        for name in ("../outside.py", "C:/outside.py", "\\\\host\\outside.py", "NUL.py", "trailing."):
            manifest = deepcopy(self.manifest)
            case = manifest["cases"][0]
            case["files_sha256"][name] = case["files_sha256"].pop(case["name"])
            case["name"] = name
            variants.append(manifest)
        duplicate = deepcopy(self.manifest)
        duplicate["cases"].append(deepcopy(duplicate["cases"][0]))
        variants.append(duplicate)
        alias = deepcopy(self.manifest)
        alias["cases"][0]["files_sha256"]["SUBJECT.PY"] = alias["cases"][0]["files_sha256"]["subject.py"]
        variants.append(alias)
        for manifest in variants:
            save(self.root / "manifest.json", manifest)
            (self.root / "manifest.sha256").write_text(harness.sha((self.root / "manifest.json").read_bytes()))
            with self.assertRaisesRegex(ValueError, "invalid_frozen_suite"):
                harness.load_suite(self.root)
        with self.assertRaisesRegex(ValueError, "duplicate_json_key"):
            loads('{"charged":193,"charged":0}')

    def test_input_directory_link_cannot_escape_frozen_scope(self):
        source = self.root / "inputs" / "C001"
        outside = Path(self.temp.name) / "outside"
        self.assertTrue(source.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(outside.resolve().is_relative_to(Path(self.temp.name).resolve()))
        source.rename(outside)
        try:
            source.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "frozen_suite_path_escape"):
            harness.load_suite(self.root)

    def test_shared_ledger_requires_explicit_valid_limit_and_basis(self):
        for change in ({"basis": ""}, {"basis": None}, {"limit": True}, {"limit": 0}, {"charged": -1}):
            budget = new_budget(512)
            budget.update(change)
            with self.assertRaisesRegex(ValueError, "invalid_campaign_budget"):
                validate_budget(budget)
        budget = new_budget(512)
        budget.pop("ledger_id")
        save(self.root / "budget.json", budget)
        fake = FakeInstalledCLI(self.manifest)
        with self.assertRaisesRegex(ValueError, "campaign_ledger_identity_required"):
            self.run_fake(fake)
        self.assertEqual(fake.case_calls, [])
        self.assertEqual(read(self.root / "budget.json"), budget)

    def test_manifest_migration_does_not_modify_predecessor(self):
        old = deepcopy(self.manifest)
        old["schema"] = "jev-outcome-acceptance-v1"
        old.pop("catalog_revision")
        old.pop("migration_sha256")
        save(self.root / "manifest.json", old)
        raw = (self.root / "manifest.json").read_bytes()
        (self.root / "manifest.sha256").write_text(harness.sha(raw) + "\n")
        with redirect_stdout(io.StringIO()):
            harness.freeze(Path(self.temp.name) / "revision2", 512, predecessor=self.root)
        self.assertEqual((self.root / "manifest.json").read_bytes(), raw)
        migration = read(Path(self.temp.name) / "revision2" / "migration.json")
        self.assertEqual(migration["predecessor_manifest_sha256"], harness.sha(raw))
        self.assertFalse(migration["historical_outcomes_reinterpreted"])

    def test_paused_campaign_does_not_even_probe_runtime_or_create_run(self):
        set_paused(self.root / "budget.json", True)
        fake = FakeInstalledCLI(self.manifest)
        with patch("acceptance.__main__.subprocess.run", side_effect=AssertionError("spawned while paused")), redirect_stdout(io.StringIO()):
            self.assertEqual(harness.run_live(self.args()), 1)
        self.assertFalse((self.root / "runs").exists())
        self.assertFalse(fake.case_calls)

    def test_boundary_pause_settles_current_case_and_resume_never_repeats_it(self):
        ledger = self.root / "budget.json"
        fake = FakeInstalledCLI(self.manifest, after_case=lambda _: set_paused(ledger, True))
        self.assertEqual(self.run_fake(fake), 1)
        budget = read(ledger)
        self.assertEqual(fake.case_calls, ["C001"])
        self.assertTrue(budget["paused_by_user"])
        self.assertIsNone(budget["reservation"])
        self.assertEqual(budget["charged"], 1)
        self.assertEqual(self.run_fake(fake, resume=True), 1)
        set_paused(ledger, False)
        resumed = FakeInstalledCLI(self.manifest)
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual(resumed.case_calls, ["C002"])
        self.assertEqual(read(ledger)["charged"], 2)
        summary = read(self.root / "runs" / "trial" / "summary.json")
        self.assertTrue(summary["selection_passed"])
        self.assertFalse(summary["release_gate_passed"])

    def test_signal_requests_boundary_pause_without_raising_into_current_case(self):
        with harness.boundary_pause() as requested:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(requested[0])

    def test_shared_budget_preserves_prior_charges_and_limits_future_cases(self):
        ledger = Path(self.temp.name) / "shared.json"
        budget = new_budget(34)
        budget["charged"] = 2
        budget["attempts"] = [{"run": "prior", "case": "C999", "charged_calls": 2, "unsettled": False}]
        save(ledger, budget)
        fake = FakeInstalledCLI(self.manifest)
        self.assertEqual(self.run_fake(fake, budget=ledger), 1)
        current = read(ledger)
        self.assertEqual(fake.case_calls, ["C001"])
        self.assertEqual(current["charged"], 3)
        self.assertEqual(current["attempts"][0], budget["attempts"][0])
        self.assertEqual(read(self.root / "budget.json")["charged"], 0)
        self.assertEqual(read(self.root / "runs" / "trial" / "summary.json")["hold"], "campaign_call_budget")

    def test_existing_reservation_blocks_all_dispatch(self):
        with transaction(self.root / "budget.json") as budget:
            budget["reservation"] = {"run": "prior", "case": "C009", "maximum_calls": 32}
        fake = FakeInstalledCLI(self.manifest)
        with self.assertRaisesRegex(ValueError, "unsettled_prior_campaign_reservation"):
            self.run_fake(fake)
        self.assertFalse(fake.case_calls)

    def test_package_default_profile_is_checked_and_pinned_without_cli_override(self):
        wrong = FakeInstalledCLI(self.manifest)
        wrong.default_rounding = None
        with self.assertRaisesRegex(ValueError, "package_default_profile_does_not_match_frozen_suite"):
            self.run_fake(wrong, use_package_default=True)
        self.assertFalse(wrong.case_calls)
        right = FakeInstalledCLI(self.manifest)
        self.assertEqual(self.run_fake(right, case=["C001"], use_package_default=True), 0)
        self.assertNotIn("--choice-rounding-places", right.commands[0])
        plan = read(self.root / "runs" / "trial" / "plan.json")
        self.assertEqual(plan["choice_profile_mode"], "package-default")
        with self.assertRaisesRegex(ValueError, "resume_plan_or_runtime_changed"):
            self.run_fake(FakeInstalledCLI(self.manifest), case=["C001"], resume=True)

    def test_timeout_retains_partial_output_charge_and_unsettled_reservation(self):
        fake = FakeInstalledCLI(self.manifest, timeout=True)
        self.assertEqual(self.run_fake(fake), 1)
        budget = read(self.root / "budget.json")
        self.assertEqual(budget["charged"], 1)
        self.assertIsNotNone(budget["reservation"])
        self.assertTrue(budget["attempts"][0]["unsettled"])
        self.assertIn("case_timeout", budget["attempts"][0]["result"]["failures"])
        self.assertEqual((self.root / "runs" / "trial" / "C001" / "stdout.txt").read_bytes(), b"partial output")
        with self.assertRaisesRegex(ValueError, "unsettled_prior_campaign_reservation"):
            self.run_fake(fake, resume=True)
        self.assertEqual(fake.case_calls, ["C001"])

    def test_process_io_error_retains_reservation_even_with_complete_artifacts(self):
        fake = FakeInstalledCLI(self.manifest)

        def interrupted_run(command, **kwargs):
            result = fake(command, **kwargs)
            if "-c" not in command:
                raise OSError("test process communication failed")
            return result

        self.assertEqual(self.run_fake(interrupted_run), 1)
        budget = read(self.root / "budget.json")
        self.assertEqual(budget["charged"], 1)
        self.assertIsNotNone(budget["reservation"])
        self.assertTrue(budget["attempts"][0]["unsettled"])
        self.assertIn("case_process_error", budget["attempts"][0]["result"]["failures"])
        with self.assertRaisesRegex(ValueError, "unsettled_prior_campaign_reservation"):
            self.run_fake(interrupted_run, resume=True)
        self.assertEqual(fake.case_calls, ["C001"])

    def test_settled_failure_requires_explicit_continue_and_is_never_retried(self):
        failed = FakeInstalledCLI(self.manifest, failed_receipt=True)
        self.assertEqual(self.run_fake(failed), 1)
        budget = read(self.root / "budget.json")
        self.assertIsNone(budget["reservation"])
        self.assertFalse(budget["attempts"][0]["result"]["passed"])
        with self.assertRaisesRegex(ValueError, "explicit_continuation_after_service_failure_required"):
            self.run_fake(FakeInstalledCLI(self.manifest), resume=True)
        resumed = FakeInstalledCLI(self.manifest)
        self.assertEqual(self.run_fake(resumed, resume=True, continue_after_service_failure=True), 1)
        self.assertEqual(resumed.case_calls, ["C002"])
        self.assertEqual(read(self.root / "budget.json")["charged"], 2)

    def test_resume_recovers_missing_result_from_atomic_journal_without_dispatch(self):
        self.assertEqual(self.run_fake(FakeInstalledCLI(self.manifest), case=["C001"]), 0)
        result = self.root / "runs" / "trial" / "C001" / "result.json"
        expected = result.read_bytes()
        result.unlink()
        resumed = FakeInstalledCLI(self.manifest)
        self.assertEqual(self.run_fake(resumed, case=["C001"], resume=True), 0)
        self.assertFalse(resumed.case_calls)
        self.assertEqual(result.read_bytes(), expected)
        self.assertEqual(read(self.root / "budget.json")["charged"], 1)

    def test_resume_refuses_changed_runtime_or_mutated_case_result(self):
        self.assertEqual(self.run_fake(FakeInstalledCLI(self.manifest), case=["C001"]), 0)
        different = FakeInstalledCLI(self.manifest)
        different.version = "different"
        with self.assertRaisesRegex(ValueError, "resume_plan_or_runtime_changed"):
            self.run_fake(different, case=["C001"], resume=True)
        result = self.root / "runs" / "trial" / "C001" / "result.json"
        changed = read(result)
        changed["passed"] = False
        save(result, changed)
        with self.assertRaisesRegex(ValueError, "recorded_case_result_changed"):
            self.run_fake(FakeInstalledCLI(self.manifest), case=["C001"], resume=True)


if __name__ == "__main__":
    unittest.main()
