# Outcome acceptance runner

This public development regression set asks whether the installed checker finds
authored defects, clears corrected controls, cites useful ranges and requirements,
and states missing evidence honestly. It is not a held-out accuracy estimate.
Only fixture source and requirements enter the scanned directory; labels and
witnesses remain outside it. Witness verification executes only fresh copies of
the authored catalogue. The scanner never executes target code.

Freeze a new revision without changing the old suite or its results:

```text
python -m acceptance freeze --out NEW_SUITE --predecessor OLD_SUITE
```

Schema `jev-outcome-acceptance-v2` records revision `bounded-builtins-v2` and a
hashed migration record. See [MIGRATION.md](MIGRATION.md). Frozen manifests, file
hashes, case IDs and paths are checked before dispatch; changing the sidecar hash
does not permit absolute paths, traversal or linked inputs outside the suite.

Run an installed candidate against an explicitly chosen shared call ledger:

```text
python -m acceptance live --suite NEW_SUITE --budget SHARED_LEDGER.json --python INSTALLED_PYTHON --run candidate_beta2 --live --use-package-default
```

`TYPESAFE_API_KEY` must already exist in the authorized environment. The shared
ledger must declare its positive limit, existing charged calls, attempts,
reservation, stable ledger identity, and nonempty accounting basis. Freeze creates a separate local
ledger for standalone campaigns; it does not transfer earlier charges or grant
additional authorization. Always pass the existing shared ledger when a ceiling
covers multiple suites or earlier development work. This runner never resets
its charged total, limit, previous attempts or pause flag.

`--use-package-default` omits the rounding override and requires the installed
parser's effective default to equal the frozen profile, currently two places.
Without the flag, the runner explicitly requests two places for comparisons.
The run pins that choice, policy, manifest, ledger identity, package version and
installed module hashes, and outcome-gate revision. Resume refuses an incompatible
runtime or plan. Resume supports only v2 run journals and does not convert legacy
v1 ledgers or attempts. Preserve earlier results verbatim; any transfer of prior
charges into a new shared ledger requires an explicit accounting record.

Pause and resume at case boundaries:

```text
python -m acceptance pause --budget SHARED_LEDGER.json
python -m acceptance unpause --budget SHARED_LEDGER.json
python -m acceptance live --suite NEW_SUITE --budget SHARED_LEDGER.json --python INSTALLED_PYTHON --run candidate_beta2 --live --use-package-default --resume
```

Pause lets an already running case settle and prevents the next reservation.
Console Ctrl+C requests the same boundary stop while a case is running; the CLI
child has its own process group/session. `--resume` never clears a pause flag.
Unpause is a separate explicit action and does not clear unresolved reservations.

A durable reservation precedes each case. Settlement charges the observed
receipt/report count and atomically journals its outcome. Failed and interrupted
attempts remain recorded. Resume skips every previously attempted case, including
failures, and can reconstruct a missing result file from that journal. Timeouts,
unobserved exits, missing reports or uncertain receipts retain the reservation
for explicit audit. There is no automatic retry or automatic reservation release.
A settled service failure stops the run; continuing to other unstarted cases
requires `--resume --continue-after-service-failure`.

The summary separates a passed selected subset from the full release gate. Exit
0 means every requested case passed; `release_gate_passed` additionally requires
every case in the frozen suite. Known model misses remain failures. Offline
harness tests exercise bookkeeping and trivial-hunter rejection with explicit
fakes; only the `live` command supplies evidence about actual Jev outcomes.
