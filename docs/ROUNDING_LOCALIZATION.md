# Rounded Choice mass and localization

The development candidate fixes an overstrict default that could stop
localization while leaving a broad suspicion. This change applies to both
`legacy-v1` and `scoped-v2-beta`.

## Cause and evidence

The previous default required the displayed probabilities to sum to one within
1e-12. A sum of 0.99 therefore failed as `response_distribution`. Localization
asks an existence question and a Choice together; atomic validation correctly
discarded the entire pair, so its selected narrow interval never reached direct
verification. The run remained incomplete and could retain the broad finding.

The [TypeSafe API schema](https://api.typesafe.ai/openapi.json), checked
2026-09-30, describes Choice probabilities as summing approximately to one.
Its [Choice guide](https://docs.typesafe.ai/primitives/choice) shows two-place
examples but does not promise a particular rounding precision.

A read-only audit of 193 existing development receipts found 88 Choice answers:
two had displayed mass 0.99, one during screening and one during localization.
The original runs explicitly selected the two-place profile and accepted them.
This audit made no new provider calls. It confirms the compatibility problem
without treating the screenshot as a complete reproduction of that user's run.

## Change

One shared `DEFAULT_CHOICE_ROUNDING_PLACES = 2` now supplies the CLI, hosted
adapter, raw/object validators and the fallback for custom gateways that do not
declare a profile. An explicit `rounding_places=None` remains strict; the CLI
equivalent is `--strict-choice-mass`. Receipts, reports and scoped evaluations
record the effective profile.

For two places, each displayed value contributes its interval bounded by
value ±0.005 and clipped to [0, 1]. Their summed intervals must include one.
This is not an unconditional ±0.01 total allowance. Values need not be on a
two-decimal grid. With many choices, this can admit a diffuse, uninformative
vector; acceptance alone never establishes a bug or bypasses routing gates.

The validator still enforces exact displayed argmax, finite values in [0, 1],
complete option sets, schemas, model identity and whole-batch acceptance. Raw
probabilities and confidence are preserved. There is no renormalization,
adaptive profile inference or automatic retry of an invalid response.

## Regression evidence and limits

The offline CLI regression supplies a known 0.99-mass localization answer and
requires a reported single-line finding from an initial 12-line view under both
search policies. Explicit strict mode must retain the broad suspicion, record
the validation failure, and emit an incomplete result. Impossible mass and a
wrong displayed argmax must still reject the entire pair, with usage retained.
Raw/object validation and implicit custom gateways use the same default.

These finite-reply tests establish routing and output behavior, not independent
Jev detection accuracy. A broader finding can still be legitimate: confidence,
existence judgments, missing evidence, selected sentinels and budgets can stop
narrowing. The beta may retain a broad provisional parent alongside a supported
narrow child. This fix does not promise single-line findings for every defect.
