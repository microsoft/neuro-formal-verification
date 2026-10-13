# Positive cross-file environment integration

This test fixture exercises the prototype NFV workflow integration.

This is synthetic test data, **not a discovered GitHub bug**. Publication of
its synthetic source identity is prohibited.

`caller.cpp` includes `helper.h`; the header has one scalar prototype. The complete
fixture inventory identifies one companion implementation, `helper.cpp`.
The changed caller invokes that leaf, whose parsed source body returns `x + 1`.
The synthetic developer claim says the result equals `x`, and the input is zero.

From the pinned repository root:

```powershell
nfv-workflow check --bundle workflow\tests\fixtures\cross-file\bundle.json --proposals workflow\tests\fixtures\cross-file\proposals.json --out .nfv-work\fixture-cross-file
```

Actually observed: **one accepted conditional refutation**, through the real NFV
Dafny backend. `report.json` in the output directory records the computed result `1`, the
source-derived callee definition/prototype, and actual verifier output. The
model contains a body-defined `helper` function and a call from `NfvTarget`.
No source program is compiled or executed, and no inference or GitHub mutation
is required.

The package tests also reject multiple implementation candidates, overloads,
mismatched prototypes, invented callee bodies, and incomplete source inventory.
General cross-file resolution is unsupported; the build linking this exact
captured implementation remains an explicit condition.
