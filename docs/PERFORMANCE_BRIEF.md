# Performance engineering brief

v0.5.0b2 against v0.5.0b1
(`3faa0955d90eb7a56c06b05f188ce7cadeb2555d`). All eleven improvements below are
integrated. See [the running tally](PERFORMANCE_PROGRESS.md) for measured
savings and acceptance status. Provider latency and bug-detection accuracy are
separate from local execution speed.

## Implemented first pass

- **Durable receipts:** replace four journal writes with durable dispatch intent
  and a terminal result. Persist intent before transport and the terminal record
  before releasing answers. Preflight failures still get a terminal receipt;
  interrupted dispatch remains potentially submitted. Preserve reported usage
  even when answer validation fails. Avoid unlinking a temporary file already
  consumed by a successful atomic replacement.
- **Preparation:** validate and own request state once, assemble exact immutable
  JSON fragments, and size growing rank menus incrementally. The executor reuses
  prepared request bytes and hashes. All batches are preflighted before dispatch;
  retained wire bodies are capped at 1 MiB, with overflow reconstructed only when
  admitted. No question, evidence passage or required validation is omitted.
- **Validation:** use exact Decimal arithmetic in a private, bounded precision
  context instead of repeated Fraction construction. Preserve displayed argmax,
  strict and rounded mass rules, atomic batch acceptance and custom-gateway
  parity. The context is independent of the caller's Decimal settings and traps
  inexact operations.
- **Bookkeeping:** reuse local state hashes, immutable window geometry, evidence
  lookups and handoff indexes. Copy mutable answer containers while retaining
  immutable scalars. Avoid persistent identity caches over records whose nested
  objects a library caller could mutate.
- **Input and inventory:** use bounded native line splitting and count physical
  CR/LF delimiters before allocating line objects. Reuse relative multiscale
  geometry where bounds permit it; retain incremental planning for tight caps.
  Use actual read bytes for file admission and hashing. Remove a duplicate CLI
  confidentiality scan while retaining pre-output and library-boundary checks.
- **Child startup:** skip site initialization for the isolated child that imports
  only the standard library. The child still validates the canonical request,
  uses the pinned endpoint/model, and inherits only the credential and required
  platform temporary-directory variables.

## Second pass

- **Oversized views:** prove a mandatory view exceeds the state cap using a
  conservative lower bound before allocating every source-line record. Validate
  all initially visible text and the original question; fall back to the original
  rendering/error path for unsupported or malformed library inputs. Do not assume
  zero-halo exact size is monotone: larger halos can merge excerpt headers.
- **Issue and handoff work:** index simple issues by stable coordinates, then
  compare their current contents so resolved and renewed limitations remain
  distinct. Sort parent ranges once. Cap retained request bytes by both 1 MiB and
  remaining-call capacity; overflow still gets complete preflight and provenance.
- **Source memory:** size-hint the first read without trusting file metadata for
  admission. Read observed growth from the same handle. Count lines in bounded
  blocks and release raw bytes before normalization/splitting. Actual bytes still
  determine the hash and byte limits; CR/LF coordinates remain unchanged.

## Transport integration

A reusable, serial transport worker amortizes interpreter and certificate
store setup over its bounded lifetime. It keeps a fresh HTTPS connection per request, so it
does not remove TCP/TLS handshakes. Acceptance requires correlated bounded frames,
deadline supervision of both pipe writes and reads, verified worker ownership,
kill/reap on failure, no automatic retries, and cleanup before CLI success.
Uncertain cleanup must prevent replacement workers from dispatching more work.

Worker lifecycle checks include actual Windows process-handle signaling rather
than relying solely on a cached exit code. Failed cleanup retains ownership and
prevents further dispatch. Startup, pipe writes/reads, malformed frames, repeated
interruptions, idle retirement and retirement races have independent fault
coverage. Parent receipt ownership and CLI cleanup checks now pass with the
worker selected by default on supported runtimes. Unsupported runtimes select
the existing single-use path before dispatch; errors never trigger fallback.

The final project sampler computes original window counts and spread ranks
arithmetically, constructing only selected references. It preserves the original
global order, available counts and per-target limitations/events. Primitive
immutable inputs use the bounded path; unsupported library shapes retain the
original enumeration path. Exhaustive geometry and complete report/request
comparisons pass.

## Hosted probability compatibility

The shared Choice mass default is now the fixed two-place compatibility profile;
explicit None/strict CLI mode retains normalized-mass validation. This accepts
observed 0.99 displayed sums that the previous strict default would reject,
potentially preventing localization. Exact argmax,
raw scores, thresholds and atomic batch validation are unchanged. Read
[the focused fix brief](ROUNDING_LOCALIZATION.md) before modifying this policy.

## Beta localization correctness

An already-admitted positive-mass interval proposal can now reach bounded
independent verification even when a repeated broad existence answer is below
the inspection threshold. The broad answer is not explicit refutation of the
narrower proposition. Choice remains routing only; the absolute support,
refutation and missing-evidence answers still determine the result. Legacy gates,
sentinel handling and reporting/relation thresholds are unchanged.

This can consume additional calls within existing caps and may leave less budget
for later targets. It is an effectiveness change requiring matched valid controls,
not a performance-equivalence claim. No prompt was rewritten to rescue an exposed
case, and old misses remain recorded.

Localization traces also distinguish planned verification candidates from those
whose current-view triad actually validated. Compatible cache reuse qualifies;
caps, invalid or partial answers and stale earlier support do not. A validated
refutation or missing-evidence result counts as an assessment, not bug support.

The owner accepts code-derived performance expectations. Further timing campaigns
are unnecessary; retain existing observations and qualify their scope. In
particular, local storage latency is variable, the preflight has a small guard
cost for fitting views, and worker reuse adds lifecycle work while amortizing
startup over multiple calls. Do not claim every micro-operation is faster.

## Validation and handoff constraints

Compare ordered request bytes and normalized complete reports, not only final
finding counts. Keep measurements of local fixed-response work distinct from
live service timing. Exercise receipt failures, cancellation, memory bounds,
invalid responses and exact replay of prior hosted responses. Do not add
overlapping component savings or multiply their speedups.

The actual installed v0.5.0b2 scoped-policy campaign completed 34 cases: 28 passed,
including 10/16 defective programs, 16/16 corrected controls and 2/2 honest
missing-evidence outcomes. The six missed bugs keep its outcome gate failed.
See [the validation record](BETA2_VALIDATION.md) for scope and reproducibility.
Do not select a search policy merely because local execution becomes faster:
legacy and scoped policies ask different questions. The relation gate remains
0.70. No finite benchmark establishes an absolute optimization ceiling.

The next effectiveness experiments are described in
[ideas from the Jev regression article](JEV_BLOG_IDEAS.md). They are proposals;
the shipped question pack was not rewritten or recalibrated to pass these cases.
