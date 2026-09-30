# Frozen first-pass local execution study

Historical measurements of the first optimization pass, before later source,
oversized-view and worker changes. They are not measurements of the final
candidate. Raw numeric evidence is in [the companion JSON](pass1-local-runtime.json).

Baseline: published `v0.5.0b1`, commit `3faa0955d90eb7a56c06b05f188ce7cadeb2555d`. Windows, Python 3.11.16.

The benchmark ran the real CLI, validator, source/project reads, durable receipts and output generation. Only transport replies were fixed. No Jev requests were sent.

All 10 workloads preserved the exact ordered request hashes, normalized reports, stdout, normalized stderr and normalized terminal receipts: **True**. Four cases replayed all 20 distinct recorded live request hashes through the actual adapter.

Sampling stopped after storage latency became highly variable. Every completed observation is retained, including the slow outlier. The following ranges are observed local execution times, **not a stable overall speedup estimate**. Cold means a fresh worker; imports, transport startup, HTTP/TLS and Jev time are excluded. Warm observations are a separate diagnostic pair.

| Workload | Calls | Cold pairs | Baseline local wall (s) | Candidate local wall (s) | Baseline CPU median (s) | Candidate CPU median (s) | Durable writes |
|---|---:|---:|---:|---:|---:|---:|---:|
| small-clean | 1 | 2 | 0.248–1.079 | 0.278–0.941 | 0.094 | 0.086 | 4 → 2 |
| wide-clean | 28 | 2 | 6.168–12.231 | 3.832–5.297 | 0.469 | 0.336 | 112 → 56 |
| hot-local | 59 | 2 | 16.582–180.639 | 7.688–26.521 | 0.984 | 0.531 | 236 → 118 |
| scoped-hot | 8 | 2 | 2.280–5.772 | 0.864–3.119 | 0.141 | 0.141 | 32 → 16 |
| project-clean | 68 | 1 | 21.684–21.684 | 12.173–12.173 | 0.812 | 0.484 | 272 → 136 |
| privacy-large | 1 | 1 | 4.060–4.060 | 4.095–4.095 | 3.688 | 3.750 | 4 → 2 |
| live-F01 | 8 | 1 | 5.890–5.890 | 19.067–19.067 | 0.234 | 0.141 | 32 → 16 |
| live-F03 | 5 | 1 | 2.873–2.873 | 2.321–2.321 | 0.109 | 0.125 | 20 → 10 |
| live-F04 | 4 | 1 | 2.880–2.880 | 2.182–2.182 | 0.125 | 0.094 | 16 → 8 |
| live-F05 | 3 | 1 | 1.475–1.475 | 2.255–2.255 | 0.172 | 0.125 | 12 → 6 |

Successful attempts use two durable journal writes instead of four. This is an exact reduction in required operations; it does not imply an exact twofold wall-time improvement.

The 15.95 MB input intentionally has a one-call/eight-window budget. It remains incomplete in both versions. Its profile exposed repeated oversized-root fitting (2,085 fitting calls and 10,422 request encodes), not a privacy-scan bottleneck. The chronology is bounded breadth-first planning before the window budget is exhausted.

| Separate preparation memory stress | Questions | Batches | Baseline peak bytes | Candidate peak bytes | Same planned wires/result |
|---|---:|---:|---:|---:|---:|
| largest_valid_empty_text_root | 15313 | 1936 | 7320113 | 8455466 | True |
| ten_thousand_library_questions | 10000 | 371 | 4302914 | 5409148 | True |

Memory figures trace incremental request-planning allocations with inputs already allocated. They are not whole-process peak memory. The JSON companion retains every timing observation, warm-pair results, hash manifests, profile categories, unmatched completed observations and interruption accounting.
