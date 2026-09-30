# Jev Bug Hunter

A bounded, first-pass bug hunter — one more tool in the toolshed.

**v0.5.0b2 is an experimental beta.** Existing commands use `legacy-v1`.
Use `--search-policy scoped-v2-beta` to try evidence-scoped verification and
broader Choice exploration. The live scoped-policy development suite detected
**10 of 16 defective programs**, cleared all 16 corrected controls and returned
incomplete for both missing-evidence cases. **Six known bugs were missed; the
full outcome acceptance gate failed.** A clear scan is not proof of correctness.
See the [v0.5.0b2 validation and limitations](docs/BETA2_VALIDATION.md).

All eleven local performance improvements and the localization fixes are tracked in the
[running improvement table](docs/PERFORMANCE_PROGRESS.md) and the concise
[engineering brief](docs/PERFORMANCE_BRIEF.md). Measured observations and
code-derived expectations are labeled separately; neither establishes improved
bug-detection accuracy or a universal runtime guarantee.

The maintained beta branch is `beta/v0.5.0b1`; its current package version is
`0.5.0b2`. The original `v0.5.0b1` tag remains a historical snapshot. The
[original beta design](docs/BETA_REVIEW.md),
[original validation record](docs/RELEASE_VALIDATION.md) and
[earlier expert review](docs/EXPERT_REVIEW.md) describe that earlier version.

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
accumulates in a local audit trail whose receipts hold hashes, usage metadata
and validated judgments, never your source, your specification, or your credentials.

The loop: every window is screened; hot regions are drilled into; suspicious
spans are re-asked as independent recheck questions that never echo their own
answer; and the judge itself then chooses among a bounded set of investigation
steps — read another region, test a cross-file relationship, search the
specification — each paired with a "continue?" control under a hard budget.
General screen hits can establish initial suspicions even when later
investigation is unresolved. In `legacy-v1`, direct rechecks provide additional
support and relationships can promote only without a contrary same-span recheck.
The beta instead binds support, explicit refutation and missing evidence to a
specific provisional proposition and evidence revision. Findings carry line
citations with optional read-with and
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

Install Python 3.11 or newer, then install this beta in a virtual environment:

```sh
python -m venv .venv
# Activate .venv using your shell's activation command.
python -m pip install "git+https://github.com/BillNDD/jev-bug-hunter.git@v0.5.0b2"
jev-bug-hunter --help
```

For a source checkout, select the `beta/v0.5.0b1` branch, enter its directory,
and run `python -m pip install .`. No third-party runtime dependencies are required.

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
Under `legacy-v1`, the bounded localization beam uses alternatives from the same Choice vector.
Every candidate retains `min(confidence, p[candidate]) >= choice-confidence`;
probabilities are never compared across menus or summed into bug confidence.
That legacy 0.50 gate cannot admit three options in a normalized distribution.
The opt-in beta selects up to three positive-mass options from one valid menu,
then verifies each independently. This optional beam ends after that menu;
removing an option cannot remove the corresponding defect from the evidence.
Low Choice confidence is retained as a diagnostic. Sentinel disagreements can
trigger bounded mechanical subdivision and an unresolved limitation; they do
not establish a clean result. Truncating the planned beam with a hard budget
records incompleteness.

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
the action loop. Promotion is deferred until final reconciliation. Under
`legacy-v1` it still requires the contrary-recheck check. If retrieved evidence supplies previously missing
context, a fresh `need_context` Noul reassesses the original region. Only a
validated value below the context threshold makes that old limitation
non-affecting. The original issue and a `context_reassessed` event remain in the
report; unrelated errors and contrary rechecks are preserved. Failed checks are
not retried on unchanged evidence.

Beta direct rechecks receive the actual retrieved passages within the evidence
and payload caps. A low support value alone does not refute a proposition.
Conflicts may receive one assessment of the original combined passages; it can
supersede only observations whose evidence it includes. Superseded observations
cannot lend their relationship gate to a new assessment. Other missing-evidence
and execution limitations remain visible.

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
| `--search-policy` | legacy-v1 | opt in with `scoped-v2-beta`; changes judgments and exploration |
| `--refute-threshold` | 0.80 | explicit refutation cutoff in the beta policy only |
| `--context-threshold` | 0.60 | context/evidence/requirement relevance threshold |
| `--relation-threshold` | 0.70 | target↔evidence relationship threshold |
| `--choice-confidence` | 0.50 | legacy Choice execution cutoff; beta exploration uses positive-mass ranking |
| `--choice-rounding-places` | 2 | fixed mass tolerance for nearest rounding at N places, 2–8 |
| `--strict-choice-mass` | off | explicitly require normalized mass with 10^-12 serialization slack; mutually exclusive with explicit rounding |
| `--localization-beam-width` | 3 | legacy gated intervals per round; beta positive-mass intervals from one menu; max-localizations caps both |
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

The isolated child receives the effective per-call timeout, including the
remaining run budget. Socket timeouts report `deadline_exceeded`. The parent
still enforces the overall transport deadline and terminates a stalled child.

The development adapter reuses one serial worker and its TLS trust configuration
within bounded request, idle and lifetime limits. Each request opens a fresh HTTPS
connection and retains its own durable dispatch intent and terminal receipt.
Worker failures are not retried. Unsupported worker runtimes use the existing
single-use child, selected before dispatch. The CLI closes its owned transport
before emitting completion; cleanup failures make the scan incomplete.

Choice mass uses a fixed two-place compatibility tolerance by default in the
CLI, hosted adapter and raw/object validators. TypeSafe's API describes the sum
as approximate; valid displayed probabilities can total 0.99. The checker keeps
the original values and requires that their clipped rounding intervals contain
a normalized distribution. It still requires exact displayed argmax, finite
values in range, matching options and atomic batch validity. It never infers a
profile from a failed response, renormalizes it, or retries it automatically.
Use `--strict-choice-mass` (or explicit `rounding_places=None` in Python) for the
previous strict policy. This tolerance is a compatibility choice, not a provider
precision guarantee. See [the localization fix](docs/ROUNDING_LOCALIZATION.md).

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

A library scan borrows its gateway. When using the hosted adapter, close it when
finished, preferably with a context manager:

```python
from bug_hunter.jev import HostedJev

with HostedJev("receipts") as gateway:
    result = scan(source, gateway, config, spec=requirements,
                  context_scope="declared_standalone")
```

Use a gateway from one caller at a time. Its `last_receipt` and `last_call_stats`
describe that calling thread's latest attempt. Cancellation supervises the whole
pipe exchange, including writes. Abrupt parent death during HTTP removes that
supervision; socket timeouts remain, so it is not a hard wall-time guarantee after
the parent process has died.

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

The opt-in beta uses report **6** and handoff **3**. It adds scoped proposition,
evidence-revision and role-evaluation records, policy identity, and separate
execution/assessment status. Receipt 3 gains optional measured timeout,
transport/validation duration and wire-size metadata. Historical receipt and
legacy report meanings remain unchanged.

## Limitations

- No suspected ranges is **not** a correctness certificate. The output is a
  static-model assessment of the supplied evidence only. Thresholds are
  operating heuristics, not calibrated probabilities of real bugs.
- Cross-file detection remains incomplete: evidence retrieval and relationship
  judgments can miss defects. See the known C027 failure in the
  [current validation record](docs/BETA2_VALIDATION.md).
- Live scans need direct network egress to `api.typesafe.ai`; proxy
  configuration is not supported.
- Private-file permission hardening (`0600`/`0700`) applies on POSIX systems.

## License

MIT — see [LICENSE](LICENSE).
