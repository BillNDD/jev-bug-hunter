"""Command line entry point; importing this module performs no review."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .core import Config, Source, format_findings, scan
from .handoff import format_handoff
from .jev import HostedJev, JevError, check_sensitive
from .repository import ProjectIndex


class SafeParser(argparse.ArgumentParser):
    """Do not echo arbitrary arguments, paths, or possible secrets on failure."""

    def error(self, message: str) -> None:
        self.exit(2, "Invalid command-line arguments; use --help for usage.\n")


def decimal_argument(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Expected a decimal number.") from None
    if not result.is_finite():
        raise argparse.ArgumentTypeError("Expected a finite number.")
    return result


def parser() -> argparse.ArgumentParser:
    result = SafeParser(
        prog="python -m bug_hunter",
        description=(
            "Use pinned Jev Noul/Choice judgments to locate suspected bugs in a text "
            "file. Findings are suspicions, not verified defects."
        ),
    )
    result.add_argument("file", type=Path)
    result.add_argument("--spec-file", type=Path)
    result.add_argument("--encoding", default="utf-8-sig")
    result.add_argument("--width", type=int, default=48)
    result.add_argument("--min-width", type=int, default=12)
    result.add_argument("--max-depth", type=int, default=4)
    result.add_argument("--context-lines", type=int, default=12)
    result.add_argument("--inspect-threshold", "--drill-threshold", dest="drill_threshold",
                        help="Detailed-search threshold; does not gate speculative screening.",
                        type=decimal_argument,
                        default=Decimal("0.60"))
    result.add_argument("--report-threshold", type=decimal_argument,
                        default=Decimal("0.80"))
    result.add_argument("--max-calls", type=int, default=1000)
    result.add_argument("--max-questions", type=int, default=20000)
    result.add_argument("--max-windows", type=int, default=50000)
    result.add_argument("--max-context-packets", type=int, default=8)
    result.add_argument("--max-localizations", type=int, default=4)
    result.add_argument("--context-threshold", type=decimal_argument,
                        default=Decimal("0.60"))
    result.add_argument("--choice-confidence", type=decimal_argument,
                        default=Decimal("0.50"))
    profiles = result.add_mutually_exclusive_group()
    profiles.add_argument("--choice-rounding-places", type=int, choices=range(2, 9),
                        default=None,
                        help="Opt in to mass tolerance for nearest rounding at N places.")
    profiles.add_argument("--strict-choice-mass", action="store_true",
                        help="Use exact normalized-mass validation instead of "
                             "rounding tolerance (the default).")
    result.add_argument("--localization-beam-width", type=int, default=3,
                        help="Maximum candidate intervals per localization round; each keeps the confidence gate.")
    result.add_argument("--max-handoff-candidates", type=int, default=64,
                        help="Candidate cap per reference kind per finding; omissions are flagged.")
    result.add_argument("--no-bug-lenses", action="store_true",
                        help="Disable the additional parallel Jev bug-lens questions.")
    result.add_argument("--max-action-steps", type=int, default=4)
    result.add_argument("--max-action-targets", type=int, default=32)
    result.add_argument("--evidence-min-width", type=int, default=8)
    result.add_argument("--evidence-beam-width", type=int, default=3)
    result.add_argument("--evidence-max-depth", type=int, default=4)
    result.add_argument("--max-evidence-candidates", type=int, default=96)
    result.add_argument("--relation-threshold", type=decimal_argument,
                        default=Decimal("0.70"))
    result.add_argument("--max-relationships", type=int, default=4)
    result.add_argument("--scope", choices=("project", "standalone", "isolate"),
                        default=None,
                        help="Required caller declaration: project (related "
                             "files may be searched), standalone (none exist), "
                             "isolate (judge this file only). Never inferred.")
    result.add_argument("--project-root", type=Path,
                        help="Related directory; required with --scope project, "
                             "forbidden otherwise.")
    result.add_argument("--max-project-files", type=int, default=128)
    result.add_argument("--max-project-file-bytes", type=int, default=1048576)
    result.add_argument("--max-project-total-bytes", type=int, default=16777216)
    result.add_argument("--max-project-candidates", type=int, default=128)
    result.add_argument("--output-format", choices=("compact", "ranges", "json"),
                        default="compact", help="Stdout only; all formats are also saved.")
    result.add_argument("--no-whole-file", action="store_true")
    result.add_argument("--output-dir", type=Path,
                        default=Path("bug_hunter_runs"))
    result.add_argument("--deadline-seconds", type=float, default=30.0)
    result.add_argument("--run-deadline-seconds", type=float, default=300.0,
                        help="Cooperative run work deadline, including indexing; hosted calls use remaining time.")
    return result


def write_new(path: Path, text: str) -> None:
    """Only create new private files inside the unique run directory."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def failure(code: str) -> int:
    print(f"Scan failed ({code}).", file=sys.stderr)
    return 2


def need_user_input(field: str, hint: str) -> int:
    """Machine-readable scope contract for human and AI callers."""
    print(f"NEED_USER_INPUT {field}: {hint}", file=sys.stderr)
    return 3


def execute(args: argparse.Namespace) -> int:
    # Scope is the caller's explicit contract; the tool never infers it.
    if args.scope is None:
        return need_user_input(
            "project_scope", "provide project directory | standalone | isolate")
    if args.scope == "project" and args.project_root is None:
        return need_user_input("project_root", "provide related directory")
    if args.scope != "project" and args.project_root is not None:
        return need_user_input(
            "conflicting_options",
            "--project-root conflicts with --scope standalone | isolate")
    if args.scope == "project" and not args.project_root.is_dir():
        print("Scan failed (project_root_invalid).", file=sys.stderr)
        return 2
    started_at = datetime.now(timezone.utc).isoformat()
    started_monotonic = time.monotonic()
    config = Config(
        width=args.width,
        min_width=args.min_width,
        max_depth=args.max_depth,
        context_lines=args.context_lines,
        drill_threshold=args.drill_threshold,
        report_threshold=args.report_threshold,
        max_calls=args.max_calls,
        max_questions=args.max_questions,
        max_windows=args.max_windows,
        max_context_packets=args.max_context_packets,
        max_localizations=args.max_localizations,
        context_threshold=args.context_threshold,
        choice_confidence=args.choice_confidence,
        whole_file=not args.no_whole_file,
        max_handoff_candidates=args.max_handoff_candidates,
        bug_lenses=not args.no_bug_lenses,
        max_action_steps=args.max_action_steps,
        max_action_targets=args.max_action_targets,
        evidence_min_width=args.evidence_min_width,
        evidence_beam_width=args.evidence_beam_width,
        evidence_max_depth=args.evidence_max_depth,
        max_evidence_candidates=args.max_evidence_candidates,
        relation_threshold=args.relation_threshold,
        max_relationships=args.max_relationships,
        max_project_candidates=args.max_project_candidates,
        localization_beam_width=args.localization_beam_width,
        run_deadline_seconds=args.run_deadline_seconds,
    )
    deadline_at = started_monotonic + config.run_deadline_seconds
    source = Source.read(args.file, encoding=args.encoding)
    spec = None
    if args.spec_file is not None:
        spec_source = Source.read(
            args.spec_file, encoding=args.encoding,
            max_bytes=8192, max_lines=2000,
        )
        spec = spec_source
    parent = args.output_dir.resolve()
    project = None
    if args.scope == "project":
        project = ProjectIndex.read(
            args.project_root, primary_path=args.file, encoding=args.encoding,
            max_files=args.max_project_files,
            max_file_bytes=args.max_project_file_bytes,
            max_total_bytes=args.max_project_total_bytes,
            exclude_paths=(parent, args.spec_file),
            deadline_at=deadline_at,
        )

    # Check all source content, including text outside any submitted window.
    # Checking the output path before mkdir also prevents secret-bearing paths.
    check_sensitive(
        source.name, "\n".join(source.lines),
        "\n".join(spec.lines) if spec is not None else None,
        str(args.file), str(args.spec_file), str(parent),
        *([item for entry in project.entries for item in
           (entry.relpath, "\n".join(entry.source.lines))] if project else []),
    )
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    print(f"Results run: {json.dumps(run_dir.name)}", file=sys.stderr)
    gateway = HostedJev(
        run_dir / "receipts", deadline_seconds=args.deadline_seconds,
        rounding_places=(None if args.strict_choice_mass
                         else args.choice_rounding_places),
    )
    gateway.check_sensitive(source.name, "\n".join(source.lines),
                            "\n".join(spec.lines) if spec is not None else None)
    if spec is None:
        print(
            "Intent is inferred from the file; no specification was supplied.",
            file=sys.stderr,
        )
    result = scan(source, gateway, config=config, spec=spec, project=project,
                  context_scope={"project": "project",
                                 "standalone": "declared_standalone",
                                 "isolate": "owner_isolated"}[args.scope],
                  deadline_at=deadline_at)
    result["run_id"] = run_dir.name
    result["started_at_utc"] = started_at
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report_text = json.dumps(
        result, ensure_ascii=True, indent=2, allow_nan=False,
    ) + "\n"
    findings_text = format_handoff(result["handoff"])
    ranges_text = format_findings(result)
    handoff_text = json.dumps(result["handoff"], ensure_ascii=True,
                              separators=(",", ":"), allow_nan=False) + "\n"
    gateway.check_sensitive(report_text, findings_text, ranges_text, handoff_text)
    write_new(run_dir / "report.json", report_text)
    write_new(run_dir / "findings.txt", findings_text)
    write_new(run_dir / "handoff.json", handoff_text)
    write_new(run_dir / "ranges.txt", ranges_text)
    sys.stdout.write({"compact": findings_text, "ranges": ranges_text,
                      "json": handoff_text}[args.output_format])
    complete = result["status"] == "complete"
    count = len(result["findings"])
    status = "complete" if complete else "incomplete"
    attempted = result["calls_attempted"]
    assessed = result["coverage"]["assessed_lines"]
    print(
        f"Scan {status}; {count} suspected region(s); {attempted} call(s) "
        f"attempted; {assessed}/{len(source.lines)} source lines assessed.",
        file=sys.stderr,
    )
    if not complete:
        return 2
    return 1 if count else 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except JevError as error:
        return failure(error.code)
    except (UnicodeError, LookupError):
        return failure("input_encoding_error")
    except OSError:
        return failure("file_io_error")
    except (ValueError, TypeError):
        return failure("invalid_input_or_configuration")
    except KeyboardInterrupt:
        return failure("interrupted")
    except Exception:
        # Never expose exception text or its chained provider/source content.
        return failure("unexpected_error")


if __name__ == "__main__":
    raise SystemExit(main())
