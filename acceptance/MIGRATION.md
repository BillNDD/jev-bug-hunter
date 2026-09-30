# Catalogue v1 to bounded-builtins-v2

This revision repairs two overly broad authored contracts. Source programs,
defect/control labels, bug locations and the other thirty cases are unchanged.
The old frozen suite, receipts, reports and scores must remain intact. New
outcomes belong to a separately identified revision and run.

| Cases | v1 contract defect | v2 contract |
|---|---|---|
| C005 / C006, average | `[1e308, 1e308]` contains finite numbers, but the corrected control overflows its intermediate sum and returns infinity. The mathematical mean is finite. | A built-in list of at most 1,000 built-in integers, excluding bool, each between −1,000,000 and 1,000,000. Empty input returns zero; nonempty input uses ordinary Python float division rounding. |
| C017 / C018, milliseconds | `10**400` is a finite integer admitted by the old wording, but the corrected control's true division can raise overflow. | A finite built-in int or float, excluding bool, between zero and 10¹² inclusive. Ordinary Python float division rounding, including normal underflow rounding, is allowed. |

The average's largest possible absolute intermediate integer sum is 10⁹; the
unit conversion's largest result is 10⁹. The corrected programs satisfy these
bounded numerical contracts without requiring exact rational representation.
Witnesses now include domain boundaries and a non-exactly-representable mean.

The observed v1 misses **C005, C011 and C013 remain misses**. Restricting C005's
numeric domain does not excuse its empty-list failure. C011 and C013 are
unchanged. No prior pass/fail is relabeled, no receipt is discarded, and v2
results must not silently replace the v1 record.

Freeze writes schema `jev-outcome-acceptance-v2`, catalogue revision
`bounded-builtins-v2`, and a hashed `migration.json`. Supplying `--predecessor`
records the prior manifest hash while leaving all predecessor files untouched.
This is a public development regression catalogue, not independent held-out
evidence of improved detection accuracy.
