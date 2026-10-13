# General imperative-model replay with an actual repair

This replay exercises the prototype NFV workflow integration.

This is **synthetic source and a handwritten model-response fixture**, not a
GitHub PR, a live model-quality evaluation, or a source-confirmed bug.
No inference or remote mutation occurs.

The C++ source has a loop that counts to `n` and then returns one extra.
The intended claim says the result equals `n`. The fake proposer returns a
tagged imperative Dafny translation with an insufficient initial loop invariant.

From the pinned repository root, with the core inference extra, workflow package,
Dafny 4.11.0, and matching Z3 installed:

```powershell
python workflow\tests\fixtures\general-model\replay.py
```

The **real NFV type checker** resolves the translation without verifying its
original postcondition. Trusted code negates that postcondition and pins `n=2`.
The first real negative check fails. The offline fixture then supplies only
`invariant r == i`; it cannot change computation or the goal. The next negative
check, concrete witness call, returning-case checks, and final recheck succeed.

Observed result: **one checked conditional MODEL refutation, one annotation
repair, zero model-service calls**. Evidence is written to `.nfv-work/fixture-general-model/report.json` and its
per-property Dafny/log/transcript files. Default budgets remain six syntax
repairs and ten annotation repairs; this case needs only one.

The unit suite additionally exercises C, C++, Java, C#, Go, JavaScript,
TypeScript, Python, and Rust fixtures through fake translation responses and actual
Dafny checking. This validates orchestration and gates, not automatic translation
accuracy. A new live translator smoke remains separately coordinated.
