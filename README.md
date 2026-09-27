# Jev Bug Hunter

A bounded, first-pass bug hunter — one more tool in the toolshed.

`jev-bug-hunter` is a first-pass bug hunter for source files.
Point it at a file — optionally with a specification and a related-files scope —
and it returns suspected regions using the TypeSafe Jev judge. Call, question,
window and investigation limits bound the configured work. Runtime and cost
depend on input, configuration and provider behavior; they are not guaranteed.

How it works: the tool never executes your program. Python splits the target
into overlapping windows and asks the hosted TypeSafe Jev judge strict,
closed-form questions about each region — true/false Noul judgments carrying
probabilities, and closed-choice questions carrying a full probability vector
for localization. Every response is validated before it can affect anything:
exact displayed-argmax under the selected probability profile, atomic batches,
a fixed error taxonomy, and fail-closed handling — a malformed or missing
answer becomes a recorded limitation, never an invented score. Evidence
accumulates in a local audit trail whose receipts hold hashes and usage
metadata only, never your source, your specification, or your credentials.

The loop: every window is screened; hot regions are drilled into; suspicious
spans are re-asked as independent recheck questions that never echo their own
answer; and the judge itself then chooses among a bounded set of investigation
steps — read another region, test a cross-file relationship, search the
specification — each paired with a "continue?" control under a hard budget.
Evidence only becomes a reported finding when it survives that whole loop: a
general screen hit, an independent recheck, or a relationship with no contrary
recheck behind it. Findings carry line citations with optional read-with and
requirement references, and the statuses are honest — "unknown" and
"not-identified" are real outputs, and any failed search, hit limit, or
incomplete pass marks the run incomplete rather than clean.

This is one tool in the toolshed, not the whole workshop. It complements — and
does not replace — test suites, static analyzers, fuzzing, and careful human
review. A run that reports no suspicions is a fast signal, never a correctness
certificate.

Python is the deterministic executor: it reads files, preserves line identity,
enumerates valid spans, enforces limits, validates Jev responses, and writes the
audit trail. Jev makes the semantic decisions. There is no runtime generative LLM,
no generated diagnosis, no automatic patch, and the target program is never executed.

## Project scope

The caller declares the scope; the tool never infers a project boundary.

```sh
# related files may be searched; a project root is required
python -m bug_hunter target.py --scope project --project-root ./repo

# the owner declares there is no related project
python -m bug_hunter target.py --scope standalone

# related files may exist; judge this file only
python -m bug_hunter target.py --scope isolate
```

Missing or conflicting scope information fails clearly and machine-readably:

```text
NEED_USER_INPUT project_scope: provide project directory | standalone | isolate
NEED_USER_INPUT project_root: provide related directory
```

Exit codes: `0` complete, no suspicions · `1` complete, suspicions reported ·
`2` scan failed or incomplete · `3` caller input needed (see `NEED_USER_INPUT`).

## Setup

Real scans call the hosted TypeSafe service (`api.typesafe.ai`) and need your own
`TYPESAFE_API_KEY`. The key is read from the environment only — never a flag or
file — and is never written to receipts, reports, or errors. Direct network
egress is required (no proxy support).

PowerShell:

```powershell
$secret = Read-Host "TypeSafe API key" -AsSecureString
$env:TYPESAFE_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
python -m bug_hunter target.py --scope standalone --spec-file requirements.txt
Remove-Item Env:TYPESAFE_API_KEY
```

POSIX shell:

```sh
read -rs TYPESAFE_API_KEY && export TYPESAFE_API_KEY
trap 'unset TYPESAFE_API_KEY' EXIT
python3 -m bug_hunter target.py --scope standalone --spec-file requirements.txt
```

Without a specification, intent is explicitly marked inferred. Source limit:
16 MiB / 200 000 physical lines; CLI specifications: 8 KiB / 2 000 lines. No
target line or specification text is silently truncated. The encoded state also
has a 16 KiB limit and each request a 24 KiB limit. A specification whose numbered
representation cannot fit is recorded in an incomplete report before any call.

## Compact output

The default is a short debugger handoff. Same-file references are line ranges;
cross-file references include the indexed project-relative path. Primary and
specification identities are basenames plus SHA-256; cross-file JSON references
also carry their snapshot hash. Absolute paths and identifying parent-directory
components are not printed. Stderr prints an opaque run ID; artifacts are under
the caller's `--output-dir` and that ID.

This example is generated from a deterministic offline fixture and checked by
the test suite; its judgments are not a live Jev result.

<!-- generated-example:start -->
```text
FILE: "fixture.txt"
SNAPSHOT: sha256:002d90f32ead248f77da2fc510d5cdf1b099ff7889f8c5e202eb066d5a1b5834
SPEC: "<inline-spec>" [sha256:995359a32be2e45f2e7a691fa50513beb4b79b45e943506818aff4dc896e17c4; utf8-supplied-text]
SCAN: complete | HANDOFF: complete | static assessment only | lines: 1-based inclusive

F1 | suspect: 9-9 | assessment: recheck-supported
   read-with: 1-2 | relevant-requirement: SPEC:5-6
```
<!-- generated-example:end -->

`read-with` means Jev judged that passage useful to inspect alongside the target.
It is not a claim that the referenced passage itself is defective. Each run writes:

- `findings.txt` — compact human handoff
- `handoff.json` — compact machine handoff
- `ranges.txt` — legacy line-only output
- `report.json` — full observations, lens judgments, actions, evidence searches,
  relationships, issues, and provenance
- `receipts/` — per-request receipts carrying validation metadata, hashes, usage,
  and the validated judgments — never raw source or spec text, never the key

Usage metadata is retained even when a judgment fails validation. Report totals
separate known token counts from attempts with unknown usage. A durable
`dispatching` receipt means submission is possible and the outcome is unknown;
it must not be treated as permission to retry an uncharged request.

## Detection geometry

The multiscale/offset design: at width 48, a 73-line file has root targets 1-48,
25-72 and 26-73. A 48-line region additionally preplans 24-line and 12-line
regular/half-offset/end-anchored targets. Smaller targets are screened even when
parents are cold. Eligible small targets offer complete contiguous intervals to
Jev Choice, followed by a separate Noul recheck. Broad suspicions are retained
when narrowing is unsupported or incomplete.
The bounded localization beam uses alternatives from the same Choice vector.
Every candidate retains `min(confidence, p[candidate]) >= choice-confidence`;
probabilities are never compared across menus or summed into bug confidence.

## Jev action semantics

The action Choice does **not** establish that a bug exists. It only selects a
legal next investigation operation. A paired Noul determines whether another
semantic step is warranted. Questions in one request remain independent; Python
never lets one same-batch answer silently become another question's state.

Actions are bounded and may include: `search_file`, `test_relationship` (when
candidate evidence exists), `search_requirement` (when a specification exists),
`localize` (for eligible small targets), `retain`, and `finish`. Project search
is caller-declared and runs automatically under `--scope project` — it is never
a Jev decision. All action decisions and outcomes are preserved in `report.json`.
Each following action receives selected evidence and factual operation history,
not previous model scores as proof. Independent relationship and verification
questions share a request when they fit. Requirement relevance questions sharing
one view are batched as well.

A qualifying relationship verification can route an initially cold target into
the action loop. This grants no early report support: promotion still waits for
the contrary-recheck check. If retrieved evidence supplies previously missing
context, a fresh `need_context` Noul reassesses the original region. Only a
validated value below the context threshold makes that old limitation
non-affecting. The original issue and a `context_reassessed` event remain in the
report; unrelated errors and contrary rechecks are preserved. Failed checks are
not retried on unchanged evidence.

Positive action-step/target budgets that leave work unexamined make the run
incomplete. Setting either action limit to zero explicitly disables that phase
and records the policy in the events. Zero relationships means no relationship
tests. A capped or failed requirement search is unknown unless it has selected
positive references; its incompleteness is preserved in either case.

## Repository mode safety and scope

Repository mode is a bounded evidence index, not a code loader. Files are never
imported or executed. The index uses strict decoding and skips symlinks,
binary/NUL content, invalid text, dotfiles and known secret patterns, common
VCS/build/cache directories, the primary target, the specification file, and the
output directory. The report records index counts and whether configured
indexing limits were hit. A limit hit makes the configured run incomplete rather
than silently implying exhaustive project coverage. Unreadable directories and
files and line-limit exclusions are separate limitations. Secret/binary policy
exclusions are not limit hits. Byte accounting uses the actual read snapshot,
including when a file changes during indexing.

No parser-based symbol resolution is performed. Jev judges semantic relevance
from the actual candidate text it is shown.

## Controls

| Flag | Default | Meaning |
|---|---|---|
| `--scope` | *(required)* | `project` / `standalone` / `isolate` — never inferred |
| `--project-root` | — | related directory; required with `project`, forbidden otherwise |
| `--spec-file` | — | requirements document to judge against |
| `--width` / `--min-width` / `--max-depth` | 48 / 12 / 4 | window geometry |
| `--context-lines` | 12 | halo lines around a target |
| `--inspect-threshold` | 0.60 | drill threshold |
| `--report-threshold` | 0.80 | report/support threshold |
| `--context-threshold` | 0.60 | context/evidence/requirement relevance threshold |
| `--relation-threshold` | 0.70 | target↔evidence relationship threshold |
| `--choice-confidence` | 0.50 | choice execution cutoff; not bug confidence |
| `--choice-rounding-places` | off | opt-in mass tolerance for nearest rounding at N places, 2–8 |
| `--strict-choice-mass` | default behavior | mutually exclusive with explicit rounding; normalized mass with 10^-12 serialization slack |
| `--localization-beam-width` | 3 | candidate intervals per round, each independently gated; max-localizations still caps their total |
| `--no-bug-lenses` | off | disable the five extra Jev bug-lens questions |
| `--max-action-steps` / `--max-action-targets` | 4 / 32 | bounded investigation actions |
| `--evidence-min-width` / `--evidence-beam-width` / `--evidence-max-depth` | 8 / 3 / 4 | evidence search geometry |
| `--max-evidence-candidates` / `--max-relationships` | 96 / 4 | evidence and relationship caps |
| `--max-project-files` / `--max-project-file-bytes` | 128 / 1 MiB | project index caps |
| `--max-project-total-bytes` / `--max-project-candidates` | 16 MiB / 128 | project index caps |
| `--max-calls` / `--max-questions` / `--max-windows` | 1000 / 20000 / 50000 | judge budgets |
| `--max-context-packets` / `--max-localizations` | 8 / 4 | enrichment and localization caps |
| `--max-handoff-candidates` | 64 | compact-reference candidate cap |
| `--no-whole-file` | off | omit the optional whole-file observation |
| `--deadline-seconds` | 30.0 | per-call deadline |
| `--run-deadline-seconds` | 300.0 | cooperative work deadline, including project indexing |
| `--output-format` | compact | `compact`, `json`, or legacy `ranges` |
| `--output-dir` | `bug_hunter_runs` | artifact parent |

Both probability profiles require the exact displayed argmax; displayed ties
are allowed. Rounding tolerance applies to total mass, does not normalize or
modify returned values, and does not require values to lie on a decimal grid.
It is an explicit local acceptance policy, not a guarantee of provider precision.

The run deadline is checked at work boundaries; hosted transport gets at most
the remaining time. It stops further work and records incompleteness. It cannot
interrupt an operating-system filesystem call or an arbitrary custom gateway;
report finalization can finish after the deadline. It is not a hard process
termination guarantee.

## Library use

```python
from bug_hunter.core import Source, scan

result = scan(source, gateway, config, spec=None, project=None,
              context_scope="declared_standalone")
```

`context_scope` is the caller's explicit declaration: `"project"`,
`"declared_standalone"`, or `"owner_isolated"`. It is mandatory in both the CLI
and library and is never inferred. A `project` index and
`context_scope="project"` must agree. Custom gateways may return finite native
floats or Decimal numbers; the same validator produces owned Decimal snapshots.

## Testing

The unit suite is offline, deterministic, and free — no network, no key:

```sh
python -m unittest discover -s tests
```

`python -m tests.smoke --out smoke-results` runs seeded-bug scenarios against
deterministic synthetic provider data (network construction is forbidden by the
harness). Passing tests establish implementation mechanics only. They do not
establish live provider compatibility, bug-detection recall/precision,
calibration, or adversarial robustness.

For effectiveness measurement, freeze a separate labeled holdout before running
scans and keep its labels out of source, spec and requests. The offline scorer
reads saved reports only; it never calls Jev or executes target files:

```sh
python -m bug_hunter.evaluation holdout.json --report case1=run-id/report.json
```

The suite has `schema_version: 1`, `partition: "heldout"`, and a `cases` array.
Each case has an opaque `id`, original `source_sha256`, `line_count`, and a
`bug_ranges` array of inclusive `[start, end]` pairs (empty for a valid case).
Freeze labels before evaluating; the scorer cannot establish their independence
for you. It rejects mismatched snapshots and any relation threshold other than
0.70. It reports broad defect coverage separately from exact localization,
complete misses separately from unresolved cases, valid-file false flags, and
known/unknown usage. Missing or incomplete runs never count as clean negatives.
Live compatibility, accuracy and comparative cost still require separately
authorized provider runs; none are implied by the offline suite.

## Schemas

- `report.json`: schema **5**
- `handoff.json`: schema **2**
- receipts: schema **3**

## Limitations

- No suspected ranges is **not** a correctness certificate. The output is a
  static-model assessment of the supplied evidence only. Thresholds are
  operating heuristics, not calibrated probabilities of real bugs.
- Cross-file relationship detection (bugs visible only across files) is early:
  the evidence search finds the right related material, but judges decline
  marginal cross-file claims. Planned follow-up work.
- Live scans need direct network egress to `api.typesafe.ai`; proxy
  configuration is not supported.
- Private-file permission hardening (`0600`/`0700`) applies on POSIX systems.

## License

MIT — see [LICENSE](LICENSE).
