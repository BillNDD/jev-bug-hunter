# Running performance tally

**Updated 2026-09-30.** All eleven improvements are integrated in v0.5.0b2.
Runtime regression and packaging checks pass. The completed live development
suite passed 28/34 cases, with six missed defects: its full outcome gate failed.
Read [the current validation record](BETA2_VALIDATION.md) for the beta's limits.

Updated 2026-09-30. Baseline: published **v0.5.0b1**, commit
`3faa0955d90eb7a56c06b05f188ce7cadeb2555d`. Acceptance below means the stated
implementation checks passed. It does not mean every live outcome passed or
establish improved model accuracy.

All figures below measure local code, using fixed synthetic or recorded replies
where needed. They do not measure a speed change in Jev. Component workloads
overlap: **do not add their savings or multiply their speedups**. The earlier
10.09x packing improvement is already part of this baseline and is not counted
again.

Following the owner's direction, later entries may state **code-derived
expectations** instead of timings. These describe removed work or tighter bounds,
not measured wall-time savings. Correctness checks remain required.

| ID | Accepted implementation | Measured workload | Baseline | Candidate | Saved | Validation / limitation |
|---|---|---|---:|---:|---:|---|
| P01 | Consolidate four durable receipt replacements to two; avoid redundant unlink | Complete adapter call, real disk and finite fixed reply | 129.487 ms | 55.121 ms | **74.366 ms / 57.4%** | Nine alternating paired rounds, 27 calls per variant; same request bytes. Durable intent/terminal ordering, abrupt process exit and write-failure checks pass. |
| P02 | Validate/snapshot once and reuse exact request fragments; incremental rank-menu sizing | 180-line multiscale preparation | 119.80 ms | 15.94 ms | **103.86 ms / 86.7%** | Seven paired rounds. All 148 payloads in the preparation matrix identical; no question/evidence reduction. |
| P03 | Reuse request bytes, local state hashes, root geometry and handoff indexes | 384-line screening through the actual executor | 194.5 ms | 118.1 ms | **76.4 ms / 39.3%** | Nine paired rounds against old executor with common dependencies; full reports and ordered request hashes identical. Retained wire data capped at 1 MiB, further limited by remaining calls. |
| P04 | Skip site initialization in the isolated stdlib-only child | Exact-child startup/preflight rejection | 432.96 ms | 403.86 ms | **29.10 ms / 6.7%** | Thirteen rotating paired rounds; same interpreter, OpenSSL, trust roots, TLS policy and fixed error. No HTTP request. |
| P05 | Exact bounded Decimal mass arithmetic, fewer redundant schema/value conversions | Full validation of a 255-option rounded Choice | 8.192 ms | 1.219 ms | **6.973 ms / 85.1%** | Nine paired rounds. 1,308 full hosted/object outcomes match baseline, including invalid inputs; exact argmax and both profiles preserved. |
| P06 | Bounded physical-line splitting/counting, size-hinted reads, early raw-buffer release, reusable relative window plans, fewer path resolutions | Read 200,000 LF-delimited lines | 159.0 ms | 48.7 ms | **110.3 ms / 69.4%** | Sixteen complete baseline/pass-one/pass-two outcomes match. Measurement precedes the final block-local CR detection refinement; that last refinement is checked for correctness, not retimed. |
| P07 | Remove one duplicate CLI source/spec confidentiality scan | CLI input admission | Three full source/spec checks across CLI and library setup | Two checks at the retained boundaries | **Code-derived: one joined copy and full traversal removed** | The pre-output privacy gate and library-boundary validation remain. No standalone wall-time claim. |
| P08 | Prove mandatory source views exceed the cap before building their line objects and repeated halo requests | Necessarily oversized views in large-file planning | Full line objects and repeated halo encodes | Cheap lower bound and mandatory validation | **Code-derived: avoids doomed view construction and repeated serialization** | Final 29 focused checks pass. Exact-fit, Unicode, malformed-input precedence, mutable library inputs and halo-merge cases preserve behavior. Complete large-file report/request parity passes. |
| P09 | Index simple issues by stable coordinates; sort handoff parents once | Repeated limitations and finding-parent lookup | Repeated full issue scans and parent sorts | Indexed candidate comparisons; one parent sort | **Code-derived: removes quadratic issue-list scans and repeated sorts** | Current values remain authoritative, including resolved and renewed limitations. Four Search scenarios preserve full reports and 149 ordered requests / 1,738 questions. |
| P10 | Reuse one bounded, killable interpreter and SSLContext; retain fresh HTTPS per request | Consecutive hosted calls within worker lifetime | Interpreter and trust-store setup per call | Setup once per admitted worker lifetime | **Code-derived: amortizes startup and trust loading; TCP/TLS handshakes remain** | Parent/worker/receipt checks pass, with independent actual-process cleanup probes. No retries after failure. Two initial integration failures were invalid synthetic fixtures; corrected typing preserves negative tests. No universal single-call speedup claim. |
| P11 | Count project windows arithmetically and instantiate only exact spread-selected references | Large project evidence pools, per target | Enumerate every window/reference, then discard most | O(files + selected candidates) planning | **Code-derived: bounded construction instead of full-pool allocation** | Fourteen sampler/executor checks pass, including seven constructed refs instead of 199,992 with identical selection, randomized inventories, events and full legacy/beta report/request parity. |

The [rounded-mass localization fix](ROUNDING_LOCALIZATION.md) is a compatibility
correction rather than a timed optimization. It prevents a 0.99-mass Choice from
unnecessarily failing under the default profile while preserving strict opt-in.
The beta also corrects a broad-existence veto on admitted narrow proposals and
separates planned from actually validated rechecks in its trace. Those correctness
changes can add bounded verification calls; they are not claimed as speedups.

Additional measured workloads for the same implementations:

| Implementation | Workload | Baseline | Candidate | Saved |
|---|---|---:|---:|---:|
| P02 | 48-line root preparation | 8.19 ms | 2.61 ms | 5.58 ms / 68.1% |
| P02 | Dense 200-target preparation | 696.70 ms | 60.25 ms | 636.45 ms / 91.4% |
| P03 | Scoped findings executor | 139.6 ms | 83.0 ms | 56.6 ms / 40.5% |
| P05 | 64 Noul answers | 1.587 ms | 1.202 ms | 0.385 ms / 24.3% |
| P05 | Mixed 16-answer rounded batch | 1.703 ms | 0.589 ms | 1.114 ms / 65.4% |
| P06 | Read 200,000 CRLF-delimited lines | 179.9 ms | 81.2 ms | 98.7 ms / 54.9% |
| P06 | Bounded project inventory | 366.9 ms | 185.5 ms | 181.4 ms / 49.4% |
| P06 | Tiny-file read peak allocation | 16,786,775 bytes | 10,029 bytes | 16,776,746 bytes / 99.94% |
| P08 | Rejected 50,000-line view peak temporary allocation | 55.17 MB | 8.97 KB | More than 99.9% less in this measured case |
| Second pass combined | 15.95 MB capped scan, against pass one | 4.266 s | 0.331 s | 3.935 s / 92.2%; includes P08 and other second-pass bookkeeping |

The combined second-pass scan preserved its complete report, all 2,086 issues
and request hashes. These measurements precede final small source/preflight
refinements, which were correctness-tested without another timing campaign.
The preflight added about 37 microseconds to ordinary 48-line fits in one earlier
measurement. Its final version removes an unnecessary span/sort allocation, but
no new timing claim is made. Large-view gains do not imply every small operation
became faster.

P03's final retained-wire bound is `min(1 MiB, remaining calls × 24 KiB)`.
The earlier broad stress measurements used the fixed 1 MiB ceiling and observed
about 1.1 MB additional transient peak allocation. The final bound further
reduces retained memory when few calls remain; this is a code-derived bound.

## Combined whole-scan result

The frozen pass-one comparison preserved request order/content, normalized full
reports, stdout, stderr and terminal receipts across all ten workloads: 49 timed
and 20 separately profiled completed CLI invocations. Four cases replayed all
20 original live request hashes. No additional Jev calls were made.
The [frozen first-pass record](benchmarks/pass1-local-runtime.md) retains the
workload ranges, hash manifests, memory measurements and interrupted-run audit.

The broad disk-backed timing run encountered substantial host storage variation:
one repeated baseline workload increased from roughly 20 seconds to 185 seconds.
That observation is retained, not discarded. Combined wall-time savings will not
be treated as stable merely because a noisy baseline makes the candidate look
faster. CPU measurements, durable-write counts and request/report parity are
reported separately.

## Rejected or corrected experiments

| Experiment | Decision and reason |
|---|---|
| Retain every prepared request body | Rejected: a legal 7,814-question screen increased peak allocation from 3.64 MB to 14.52 MB. The accepted 1 MiB cap reduced it to 4.68 MB, an explicitly bounded increase. |
| Cache evidence IDs solely because a dataclass is frozen | Rejected: frozen records can contain mutable manually constructed library objects. Main preparation gains do not require that cache. |
| Normalize CRLF/CR with overlapping full-size intermediates | Initial version rejected for extra peak allocation. The accepted reader releases raw bytes first and performs replacements separately; retained whole-read memory measurements precede only the final block-local CR refinement. |
| Claim a strong independent halo-fitting speedup | Not supported: measurements were small/noisy. Reuse remains local to a fit invocation, with equivalent requests. |
| Replace per-call processes with the first persistent-worker prototype | Initial version rejected because its pipe-write deadline was incomplete. P10 adds supervised writes/reads, actual process-termination proof and poisoned ownership after uncertain cleanup. |

New iterations are added only after their implementation and evidence are
accepted. No finite benchmark proves that no further optimization is possible.
This experimental beta update includes the failed outcome gate and its known
misses; it does not qualify the system as a reliable bug-detection gate.
