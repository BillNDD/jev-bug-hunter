# Effectiveness and end-to-end performance: first-principles review

The useful outcome is a timely, evidence-backed debugging handoff under an
explicit scope and budget. Fewer calls, more findings, a narrower line range,
or a shorter incomplete run is not automatically a better outcome.

## What the measurements establish

The four final development smoke cases took 40.797 seconds combined. Their
20 transport-child intervals totalled 12.453 seconds, including child startup,
TLS, network and provider work. The adapter's elapsed times before terminal
receipt writes totalled 31.359 seconds. About 18.906 seconds therefore occurred
inside the adapter but outside transport; another 9.438 seconds includes final
receipt writes, executor/CLI work and report output. These are coarse timing
buckets, not attribution to particular filesystem operations. Sub-millisecond
validation timings rounded to zero do not mean validation is free.

For this trace alone, even eliminating the entire transport interval would cap
speedup at approximately 1.44x if everything else stayed unchanged. Profile local
journaling and preparation before assuming the provider is the bottleneck.
This small, sequential development sample does not establish production latency
distributions, a universal bottleneck, or comparative model quality.

The implemented packing optimization is narrower and better controlled: one
68-question screen produced the same four request byte strings with both
algorithms. Across nine alternating paired samples of 60 iterations, median
packing time fell from 44.005 ms to 4.361 ms, about 10.09x on the development
Windows/Python 3.11.16 host. This saves about 39.6 ms for that packing operation,
not 90% of a scan. Reproduce with
`python tools/benchmark_packing.py . packing-result.json` from a source checkout.

## Ranked performance work

| Priority | Change | Why it helps; condition for preserving results |
|---|---|---|
| 1 | Measure receipt and filesystem costs separately | Add monotonic spans for serialization, pre-dispatch durable writes, terminal durable writes, engine work and final output. Do not include source or key values. The current timing buckets leave substantial local cost unattributed. |
| 2 | Reduce redundant durable writes after crash testing | The adapter currently makes four atomic receipt replacements per attempt. Consolidating pre-dispatch states may save more than a network tweak. A durable dispatch intent must precede possible submission, and a durable terminal receipt must precede releasing answers. Failed writes and interrupted/unknown attempts must remain distinguishable. Never simply remove `fsync` or move durability behind returned answers. |
| 3 | Prepare immutable work while transport is outstanding | Render/hash already-determined later windows and candidates, precompute numbered specification fragments, and size known questions during network wait. Preparation may overlap; no dependent semantic decision may consume an unfinished answer. Bound memory and stop preparation at cancellation. |
| 4 | Use exact fragment and planning caches | Cache immutable rendered fragments and exact UTF-8 sizes, including specification and halo fragments. Bind to snapshot hash, encoding, physical-line mapping, coordinates, rendering contract and order. File path/mtime alone is insufficient. Reuse successful and capped search plans with their original limitations; do not present a capped search as complete. |
| 5 | Suppress duplicate in-flight requests | With concurrency, a cache miss can race another identical miss. One keyed in-flight entry can serve all exact consumers of the same validated, durably recorded attempt. A timeout/unknown receipt is not permission to resubmit. An identical request is one observation, not several independent votes. |
| 6 | Run independent requests concurrently with deterministic reservations | The lower bound is the longest dependent request chain, not the sum of every call. Parallelize only independent ready work. Reserve calls/questions/windows and select admitted work in a stable order before dispatch; merge results deterministically. Responses arriving first must not steal another target's budget. Preserve fixed request bytes, validator and reducer. Establish rate-limit, cancellation and tail-latency behavior before adoption. |
| 7 | Reuse connections in supervised, killable workers | Retaining DNS/TLS/HTTP connections can remove setup time. Keep a finite request count/lifetime, per-request deadlines, bounded response framing, original receipt ownership and forced worker termination. Never carry a stale response into the next request. Do not replace killable isolation with an uninterruptible thread. Measure this after local costs. |
| 8 | Consider group commit for a concurrent scheduler | Several ready dispatch intents can share one durable journal flush; several terminal records can share another. Each associated dispatch or answer waits for its own commit acknowledgement. Crash recovery must preserve every possible submission and charge unknown attempts. This is a storage-protocol change, not a trivial speed flag. |
| 9 | Tune deployment, not judgments | Measure receipt-directory storage latency, contention and network distance using authorized synthetic inputs. A faster approved local volume may beat code changes. Moving data to another host requires the existing data/scope authorization; avoid synced or remote receipt storage unless its durability is characterized. Do not disable security software globally. |
| 10 | Optimize consumer handoff and replay | Show provisional progress with explicit unresolved status, then emit the final complete handoff. Retain exact successful receipts for deterministic replay and focused debugging instead of purchasing repeated opinions. Earlier visible progress improves time-to-first-useful-evidence; it is not a shorter completed scan. Cross-run reuse needs a defined freshness and compatibility policy. |

Measure complete-scope wall time, time to the first useful finding, p50/p95/p99
request latency, known/unknown usage, local CPU, write latency and incomplete
rate. Pair runs on frozen scope and policies. Report latency under matched
completeness and quality; never win a benchmark by silently doing less work.

Byte-identical local preparation optimizations offer the strongest no-regression
argument. Scheduling/transport changes can preserve requested work and reduction
semantics, but provider nondeterminism and rate limits still require deployment
validation. Exact same answers are not promised by a hosted model.

## Ranked effectiveness work

1. **Complete the grounded defect identity and evidence contract.** The beta's
   region/counterpart propositions prevent several invalid vetoes, but remain
   existential suspicions. A truly typed predicate, bound participants and
   conditions, contradiction rules and location lineage would support justified
   deduplication and narrowing. Do not merge nearby ranges merely because they
   overlap. Keep the 0.70 relationship gate unchanged.
2. **Build an independent, representative holdout.** Include valid files,
   missing context, multiple unrelated bugs, guarded paths, internally consistent
   cross-file violations, large scope and near-limit payloads. Label semantic
   mechanisms as well as line ranges, outside model evidence. Separate clean
   complete misses from incomplete runs. Synthetic development successes cannot
   establish precision/recall or calibrate thresholds.
3. **Improve evidence coverage before increasing judge repetition.** Evaluate
   reserved broad-search capacity, language-aware caller/helper candidates, and
   bounded joint evidence bundles where the necessary fact spans more than one
   passage. Keep exclusions, ordering, caps and remaining obligations visible.
   Current project mode still has one primary target; a true repository scanner
   needs explicit inventory and coverage accounting.
4. **Use Choice as a relative search signal.** The beta's positive-mass beam
   corrects the impossible width-three legacy gate. Verify every promoted
   candidate against its absolute proposition; no sum of Choice probabilities
   becomes bug confidence. Test alternative beam and abstention policies on the
   holdout. Do not assume the top item or a provider confidence statistic measures
   calibrated real-world correctness.
5. **Spend questions on distinct uncertainty.** Support, refutation and missing
   evidence are different roles. Ask independent questions on the same fixed
   view; retrieve actual missing facts before another assessment. Repeated
   paraphrases are correlated opinions, not additional ground truth. Preserve
   their cost and disagreement if an experiment genuinely needs them.

TypeSafe documents the [Choice vector](https://docs.typesafe.ai/primitives/choice)
and [confidence](https://docs.typesafe.ai/confidence), and describes independent
[fan-out](https://docs.typesafe.ai/patterns/fan-out). Their availability supports
these design options; it does not establish this tool's calibration or a measured
speedup. Contract/schema validation and deterministic arithmetic stay in Python.

Shorter prompts, fewer windows/lenses, a different model, heuristic-only
retrieval, early cancellation of alternative evidence, speculative calls, and
adaptive thresholds can reduce latency or improve average quality. None is a
result-preserving optimization by construction. Keep them experimental until a
paired holdout study supports the tradeoff.
