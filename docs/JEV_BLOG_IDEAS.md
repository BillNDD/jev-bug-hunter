# Ideas from "Jev, Regression, and More"

Read on 2026-09-30: Diane Tchuindjo's
[Jev, Regression, and More](https://dianetc.com/musings/jev/).

The article tests Jev on math-error location and pairwise answer quality. Its
wrapper computes position arithmetic outside Jev. It also shows why a lower
squared error can reflect predictions pulled toward the middle rather than
better error detection, and reports no useful gain from a finer Choice grid.
The comparisons are task-specific; they do not establish code-review accuracy.

## Implications for this implementation

JBH already computes line coordinates in code and uses Choice to route
investigation. Preserve that division. In particular, do not average line
positions using Choice probabilities: two plausible distant defects can average
to an unrelated middle line. Raw Choice mass is not an absolute bug probability.
The existing support/refutation/missing-evidence checks remain necessary.

The current live development result (10/16 buggy programs detected, all corrected
controls clear) makes detection sensitivity a concrete priority. Faster plumbing
and successful response validation alone cannot resolve those misses.

## Proposed experiments, not shipped changes

| Priority | Experiment | Evidence needed to adopt it |
|---|---|---|
| 1 | Classify bounded, concrete contract propositions instead of relying only on a broad "bug exists" question. Example: whether an allowed empty input reaches division with denominator zero. Supply the actual clause, relevant branch and helper facts. | Matched defective/corrected pairs at fixed call budgets; fewer missed defects without increased false alarms. Do not supply oracle labels or assume extracted syntax proves runtime semantics. |
| 2 | Propose statement/branch/function candidates where reliable parsing is available, retaining line-window fallback for other languages or malformed source. | Measure candidate coverage and useful final localization separately. Preserve all source coordinates and compare request size/call count. |
| 3 | Diagnose each stage: screening, candidate coverage, evidence retrieval and independent verification. | Attribute each miss to the observed stage before changing prompts or gates. Report defect-level detection, control false alarms, unresolved outcomes and localization breadth. |
| 4 | Develop prompt variants offline using a separate training/development set, then freeze them before a fresh evaluation. | Equal-budget comparisons on unseen defect families and realistic files. Preserve every failed outcome. Investigate calibration only with enough separate labeled data; do not rescale these 34 cases until they pass. |

These are engineering hypotheses inferred from the article and JBH's own
outcomes. The article does not validate them for JBH. The shipped question pack,
report threshold and relation threshold are unchanged. Increasing menu density,
lowering cutoffs or adding repeated votes is not an adopted fix.
