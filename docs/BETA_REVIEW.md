# v0.5.0b1: implementation review and creator-agent brief

This beta reviews the September 29 implementation brief against v0.4.2,
commit `fcaacdeedf07561b18721ca3a5f16d528de7e709`. The supplied document is a
proposal, not evidence that its 112 acceptance requirements have passed.

Enable the experimental policy with:

```sh
jev-bug-hunter unit.py --scope standalone --search-policy scoped-v2-beta
```

The default remains `legacy-v1`. Its existing numeric flags retain their
meaning. The relation gate remains **0.70** in both policies. The beta's
0.80 support/refutation and 0.60 missing-evidence cutoffs are operating defaults,
not calibrated probabilities or measured optima.

## Changes adopted

| Brief | Decision and implementation |
|---|---|
| B00 | Minimal immutable source references, scoped proposition keys, evidence revisions, exact structural hashes and role provenance are implemented. A complete obligation scheduler is deferred. |
| B01 | Support, explicit refutation and missing evidence are independent Noul roles on one bound proposition. Low support alone is not refutation. Counterparts have separate histories. Direct rechecks receive retrieved passages. Real contradictions retain both observations and can receive one fresh union assessment when it actually adds evidence. Supersession is restricted to covered observations, and promotion uses active observations with their own relation gate. Oversized/failed unions remain unresolved. |
| B02 | Valid diffuse Choices can select several positive-mass localization candidates with deterministic menu-order ties. Each candidate receives separate absolute verification. One fixed menu defines the optional beam; repeated shrinking menus do not pretend to remove defects from the source. Low-confidence read-only action winners may execute within the existing limits; positive continue plus a terminal Choice stays unresolved. |
| B03 | In the beta, a whole-file observation no longer suppresses local root relationship searches, and an unrelated hot subspan no longer replaces the root's relational target. Native repository-wide primary scanning is a separate feature and is not implemented. |
| B07 | In-run search memos preserve capped/failed search limitations. Exact successful reuse links to the original request; repeated failed requests are not silently dispatched again under the beta policy. Persistent caches, resume and cross-batch reuse are not implemented. |
| B08 | Exact incremental UTF-8 request sizing replaces repeated serialization of growing candidate batches. Actual flushes still use the authoritative encoder. Tests compare complete bytes, boundaries and order with the old algorithm. A local packing microbenchmark improved about 10.09x; this is not an end-to-end speed claim. Concurrency and a new scheduler are deferred. |
| B09 | The child socket timeout follows the effective caller deadline. Timeout errors remain specific. Transport inherits only required platform variables, its timeout and the TypeSafe key. Receipts add observed transport/validation timing and byte counts. Killable per-call isolation and durable dispatch/terminal writes remain. |
| B11/B13 | Versioned role contracts, explicit opt-in migration, report/handoff schema changes, compatibility tests and release qualifications are implemented. No calibration, detection-accuracy or hosted speedup claim is made. |

The prior packaging defect is also fixed: source archives include test helpers
and the smoke runner. CI covers Windows/Linux with Python 3.11, 3.12 and 3.13,
plus offline smoke, build, clean wheel installation and archive-content checks.
CI results must be read from the actual workflow run, not inferred from its YAML.

## Deliberate boundaries

The proposition records identify **provisional region or target/counterpart
suspicions**. They do not describe a fully grounded, unique real-world defect.
Consequently this is a partial implementation of B01, not the full B10 predicate,
argument-binding, semantic duplicate and location-lineage system. Broad initial
screen suspicions remain separate. The beta conservatively retains broad parents
instead of using another provisional child's location to erase them. This can
increase displayed ranges and reviewer effort.

A previously insufficient view can be resolved by a sufficient assessment that
actually includes its supplied passages. New missing material outside that view
remains unresolved. Support/refutation conflicts are order independent; scores
are never averaged or counted as independent votes. A failed request is not a
negative answer. Transport, inventory, unrelated context and deadline issues
cannot be cleared by a claim-level answer.

B04's reserved frontier is a worthwhile next experiment, but its proposed
48/32/16 allocation is unmeasured. The current depth-based search and its visible
limits remain. B05 structural/joint-evidence retrieval, B06 canonical planning,
B08 concurrency/scheduler changes, and B10 full defect identity require additional
design and paired quality tests. They are not present in this beta. The existing
single-primary project scope is unchanged. There is no `--repo`, `--dry-run`,
persistent worker pool, cross-run cache, or calibrated-policy mode.

B12's distinction between semantic detection and broad range overlap is sound.
The old range scorer is retained for historical comparisons; it does not measure
semantic precision. A representative independently labelled holdout and paired
complete-scope performance study remain open. Development smoke cases cannot
establish detection accuracy, calibration, statistical noninferiority or a
hosted speedup.

## Verification and maintenance

`tests/test_beta_policy.py` exercises diffuse beams, role classifications,
counterpart separation, insufficient/enriched views, new uncovered context,
union resolution/conflicts/size failures, relation-gate provenance, search reuse,
local project obligations, budget exhaustion and timeout/environment handling.
The unchanged baseline had 218 passing tests. See the
[release validation notes](RELEASE_VALIDATION.md) for the actual beta totals,
retained development failures and live observations. See the
[expert review](EXPERT_REVIEW.md) for ranked effectiveness and speed priorities.

Preserve the original response validator, pinned model/endpoint, explicit scope,
physical source lines, privacy boundaries, fixed error taxonomy and durable
unknown-submission handling. Do not convert the new provisional propositions
into exact defect identities without implementing and testing their bindings.
Do not enable the beta by default merely because synthetic routing tests pass.
