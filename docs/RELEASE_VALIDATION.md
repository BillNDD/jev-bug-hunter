# v0.5.0b1 validation record

Development validation date: 2026-09-29. Baseline: v0.4.2, commit
`fcaacdeedf07561b18721ca3a5f16d528de7e709`. Runtime: Python 3.11.16 on Windows.
This is a staged, opt-in beta, not a completed implementation of all 112
requirements in the supplied B00-B13 proposal.

## Deterministic checks

- The unchanged baseline passed 218 unit tests.
- The final beta passed **250 unit tests** in 92.091 seconds.
- The offline smoke runner passed **18/18** synthetic scenarios with no network.
- New tests cover scoped support/refutation/missing-evidence rules, distinct
  counterparts, enrichment, active/superseded observations, union conflicts and
  uncovered evidence, relation-gate provenance, diffuse Choice beams, sentinels,
  reused/capped searches, local project coverage, budgets, child deadlines and
  restricted inherited environment.
- Four packing tests compare actual request bytes with the previous algorithm,
  including 80 randomized Unicode/escaping/boundary cases, question-count limits,
  oversized singletons, a maximum Choice menu and mutation isolation.

The final two conflict regressions failed against the preceding implementation
before their fixes: a union could supersede an uncovered missing-evidence view,
and a superseded positive observation could lend its relation gate to an
unqualified union. Both now pass. These intentionally contradictory paths use
controlled provider answers; they are not claimed as live-provider reproductions.

## Actual hosted development smoke

The service was the fixed TypeSafe endpoint with model `jev-1.13.0`.
The campaign explicitly selected nearest-rounding acceptance at two decimal
places. The relation gate stayed 0.70. Inputs were synthetic source and
requirements; outcome labels were outside source, specification and requests.
No reviewed target was executed. There were no automatic service-error retries.

The final live group used at most 24 calls, 512 questions and a 120-second work
deadline per case. Observed outcomes:

| Case | Declared scope | Observation | Calls / questions | Wall seconds |
|---|---|---|---|---|
| F01: local wrong price | standalone | Complete; suspect ranges 1-2 and 2-2 | 8 / 21 | 18.171 |
| F03: helper violates specification | project | Complete; suspect range 1-3 | 5 / 16 | 13.125 |
| F04: valid helper and caller | project | Complete; no suspicions | 4 / 14 | 4.282 |
| F05: required external helper unavailable | isolate | Incomplete; missing evidence retained | 3 / 13 | 5.219 |

All four expected outcomes were observed. The final group used 20 calls and
64 questions, with 29,240 input tokens and 1,684 output tokens reported by the
provider. Every receipt settled as validated; there were no unknown-usage attempts.
"Complete; no suspicions" describes the configured scan, not proof of correctness.

Two later conflict-only fixes were verified with the reproducing tests above.
All 20 final-group live responses were then replayed through the final CLI with
network construction forbidden. Every request SHA-256 matched its original
live request, and each full report matched except its run ID and timestamps.
This replay made zero additional provider calls; it is replay evidence, not a
second independent live sample.

## Retained development failures and total accounting

The first draft's six-case live group made 40 calls / 113 questions. A local
valid case and a legacy-policy positive case completed, while other cases exposed
unnecessary localization/action loops or retrieved helper text missing from
subsequent direct verification. Those observations led to corrections; they were
retained rather than replaced in the development evidence.

One follow-up was interrupted after a newly added verification view was
re-enumerated as investigation work. It had six settled, validated calls / 20
questions and no completed scan report. The fix freezes the work iteration and
gives verification-only views no search priority; an exact-call-count engine
regression now detects that loop.

Across all three live groups, the campaign used **66 calls / 197 questions**,
87,607 input tokens and 5,368 output tokens. All 66 receipts were settled and
validated. The declared campaign ceiling was 192 calls. Interrupted work remains
included in usage and is not counted as a passed scan.

## Packaging, privacy and release gates

Publication also checks the wheel in a fresh environment, packaged question
resources, CLI input/error behavior, and source-archive test helpers. The
repository contains a Windows/Linux Python 3.11/3.12/3.13 CI matrix. Its actual
results are linked from the prerelease; configuration alone is not a pass stamp.

The publication privacy review covers prospective tracked files, reachable Git
history and author metadata, and every wheel/source-archive member. Checks look
for the active environment's secret values, known credential forms, local host
paths, local identity and non-public email addresses. Explicit synthetic privacy
test fixtures remain. Public release assets contain the package and documentation,
not local smoke-run directories, raw source/spec requests or credentials.
Pattern scanning plus manual review is bounded assurance, not proof that every
possible form of sensitive information is absent.

The packing microbenchmark and its limitations are documented in
[EXPERT_REVIEW.md](EXPERT_REVIEW.md). Development smoke and replay do not establish
held-out detection accuracy, calibration, statistical noninferiority, adversarial
robustness, or an end-to-end speedup. The default policy remains `legacy-v1`.
