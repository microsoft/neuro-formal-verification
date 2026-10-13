# Positive synthetic source-bound integration

This test fixture exercises the prototype NFV workflow integration.

This deliberately false expectation is **synthetic test data**, not an actual
GitHub PR or a discovered bug. No Discussion is created and the placeholder
source identity must not be used for publication.

The captured C++ function returns `x + 1`; the synthetic comment claims it
returns `x`. The witness is `x = 0`.

From the repository root, with the core/workflow packages and Dafny installed:

```powershell
nfv-workflow check --bundle workflow\tests\fixtures\scalar\bundle.json --proposals workflow\tests\fixtures\scalar\proposals.json --out .nfv-work\fixture-scalar
```

Expected and actually observed: **one accepted conditional refutation**, zero
errors. The evidence records:

* actual NFV `run_dafny` calls in unbounded mode;
* a verified negated expectation on the concrete input;
* independent parsing of the captured `increment` declaration and exact
  source/model expression match;
* a source-matched result of `1`;
* removal of the redundant nonnegative-domain requirement, followed by a fresh
  negative check and final recheck;
* final Dafny text, verifier/version logs, and SHA-256.

Changing the proposed body to an unrelated expression causes abstention even
if that expression can satisfy the negative check. Unsupported source forms
likewise cannot become publishable findings.
