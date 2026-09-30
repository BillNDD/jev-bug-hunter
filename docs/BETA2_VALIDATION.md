# v0.5.0b2 validation and known limits

Date: 2026-09-30. Destination: the existing `beta/v0.5.0b1` branch in
`BillNDD/jev-bug-hunter`. Package version: `0.5.0b2`. The original beta tag and
stable main branch retain their previous versions.

**The full live outcome gate failed.** This is an experimental update carrying
performance and correctness fixes, with six known missed defects disclosed
below. It is not a claim of complete bug detection, calibrated confidence, or
production readiness.

## Changes for maintainers

- Integrates P01-P11 from the [performance tally](PERFORMANCE_PROGRESS.md):
  fewer durable receipt writes, exact prepared-request reuse, faster exact
  validation, bounded source/view preparation and inventory, indexed execution
  bookkeeping, a supervised serial transport worker and arithmetic window
  sampling. Measured component savings overlap; do not sum them.
- Defaults Choice validation to the fixed two-decimal rounding profile.
  `--strict-choice-mass` retains strict validation. A displayed total of 0.99
  can now be valid; impossible mass, wrong displayed argmax and incomplete
  batches still fail. There is no renormalization or automatic retry. See the
  [rounding/localization fix](ROUNDING_LOCALIZATION.md).
- Lets an already-admitted positive-mass beta interval candidate receive
  independent verification despite a lower broad-existence answer. This can
  spend more of the existing bounded call budget. Choice is routing, not bug
  evidence; the report and relation thresholds remain 0.80 and 0.70.
- Separates planned candidates from completed, validated current-evidence
  assessments in localization traces. Broad provisional findings may remain
  alongside narrow findings; they are not separate proven defects.

The default search policy remains `legacy-v1`. The hosted worker creates a
fresh HTTPS connection for every request. It amortizes interpreter and trust
store setup, not TCP/TLS handshakes. Failed/uncertain cleanup retains ownership
and prevents replacement dispatch. Parent death during an in-flight network
operation remains subject to the existing socket-timeout limitation.

## Actual live outcome campaign

The installed wheel ran the actual CLI against the service, without fake
answers or target execution, using `scoped-v2-beta`, package-default rounding
and the original 0.70 relation threshold. Labels were held outside scanner
inputs. Each expected finding required useful target localization and actual
source/specification citations; project cases also required helper evidence.

| Outcome class | Passed | Total |
|---|---:|---:|
| Defective programs detected | 10 | 16 |
| Individual defects covered | 11 | 17 |
| Corrected controls clear | 16 | 16 |
| Missing evidence reported incomplete | 2 | 2 |
| Entire case checks | 28 | 34 |

The six defective cases returned complete scans with no findings:

| Case | Missed behavior |
|---|---|
| C005 | Empty-input division |
| C011 | Half-open upper bound |
| C013 | Early return inside a loop |
| C017 | Unit conversion |
| C027 | Cross-file helper limit; no supporting helper citation |
| C031 | SQL NULL count semantics |

C029 detected both separated defects and produced narrow spans, while retaining
several broader provisional ancestors. The rounding fix removes one localization
failure mechanism; it does not promise only minimal findings.

All 203 requests had validated replies and known usage. No request was retried.
There were no service/response-validation failures in this run. These successful
protocol checks do not turn the six semantic misses into passes.

A separate installed `legacy-v1` compatibility smoke passed the arithmetic
defect/control pair C001/C002 using the same package-default rounding: 13 live
requests, all with known usage. That two-case subset does not establish full
legacy-policy acceptance. The shared campaign ledger charges 409 calls including
193 earlier calls, the 203-call scoped suite and this 13-call smoke, within its
512-call limit; it has no unresolved reservation.

This is a small synthetic development suite, not a representative or untouched
held-out accuracy study. Earlier versions informed engineering work. The v2
catalog corrects overly broad numeric domains in two specifications; the source
programs and defect labels are preserved, and old outcomes are retained. See
[the migration record](../acceptance/MIGRATION.md). No general recall, precision,
probability calibration or E2E speedup follows from these numbers.

## Reproducibility

The [public outcome record](benchmarks/beta2-live-outcomes.json) contains case
results and the hashes of all 14 installed runtime resources. Those hashes
matched the final working source after the live run. The frozen manifest is
`d09abee7a2ff738c7d6bf169b39ac856b9bcae9fe7a80679d97ed5a287950b8f`.

The [acceptance runner](../acceptance/README.md) freezes source/specification
snapshots, pins the installed runtime, verifies concrete outcomes and keeps
failed attempts in an external charged-call ledger. A passed selected subset
does not set `release_gate_passed`; the full campaign's value remains `false`.
The catalog contains executable trusted witnesses for evaluation, while the
scanner itself never executes a target.

## Offline and distribution evidence

- Full runtime regression: 369 tests, successful with two platform skips.
  Later harness/default-profile/process-error checks were exercised separately.
- Final offline CLI smoke: all 18 cases passed on v0.5.0b2; zero provider calls.
- Focused rounding validation: 32 tests. Both search policies localize the
  fixed-response 12-line regression to line 7; explicit strict mode rejects
  the same rounded batch and records an incomplete result.
- Transport reviews include actual Windows process termination, bounded pipe
  writes/reads, startup failures, frame corruption, retirement races and retained
  ownership after failed cleanup. Parent and worker suites use real child
  processes with local deterministic fixtures, not provider calls.
- Exhaustive geometry checks cover 19,800 configurations, with exact ordered
  project selection and complete request/report comparisons.
- An earlier frozen performance candidate replayed four historical live cases:
  20 requests / 64 questions with original request hashes and equivalent full
  reports apart from run identifiers/timestamps. This predates the final beta
  routing change; it is performance evidence, not final-build live accuracy.
- Wheel installation, resources, CLI entry/exit behavior and source archive
  completeness pass. An observable marker verifies target code is not executed.

Final offline CLI smoke and harness checks, artifact hashes, and the publication
privacy review are recorded with the release. Privacy checks combine manual
diff review, active-environment secret matching and pattern scans; they do not
prove the absence of every possible private datum. Raw local campaign logs and
credentials are not publication artifacts.
