# Neuro-Formal Verification

Release preparation for [Neuro-Formal Verification: Agentic Language-Agnostic
Formal Program Reasoning](https://arxiv.org/abs/2608.21516v3).

This repository has two components:

- **Paper artifact:** the data, retained evidence, and minimal Dafny/CBMC
  implementation needed to examine and reproduce the reported experiments.
- **GitHub integration (in development):** a refutation-only workflow that turns statements in
  closed pull requests into at most ten source-grounded properties and reports
  accepted model refutations in developer-readable Discussions.

## Layout

| Directory | Purpose |
|---|---|
| `nfv/` | Shared inference and formal-reasoning implementation; Dafny and CBMC backends |
| `paper/` | Frozen experiment provenance, reproduction instructions, and paper evidence |
| `tests/` | Core and artifact regression tests |

See the [paper artifact instructions](paper/README.md). The GitHub integration
and Z3 example will be published separately under `workflow/` and `examples/z3/`.

The artifact and the workflow serve different purposes. Historical experiment
settings must remain frozen for paper comparisons. The integration runs the
refutation arm only; failure to refute does **not** establish correctness.

## What a finding means

The formal verifier checks a generated model under recorded assumptions.
A checked refutation of that model is not automatically a confirmed bug in the
original program. Translation fidelity, caller preconditions, external-function
contracts, and reachability all matter. Findings must distinguish source-backed
facts from unsupported assumptions and retain the formal evidence.

No finding, timeout, translation failure, or abstention is a proof that a program
is safe. The integration publishes no Discussion when it has no accepted
refutation.

The initial GitHub source adapter is deliberately conservative: it supports a
small, independently source-bound subset of scalar C/C++ return functions.
Unsupported loops, heap effects, or ambiguous source constructs abstain rather
than being replaced with an invented implementation. This is not a general
C++ verifier. Its installation guide will document the precise supported subset
and environment-model limitations when the integration is published.

## Quick start

Use Python 3.11 or newer. From a checkout:

```sh
python -m pip install -e ".[inference]"
python -m unittest discover -s tests -v
python -m paper.reproduce --require-complete
```

The inference extra installs the historical pipeline's SDK dependency; these
tests and the reproduction command do not make model requests. Dafny 4.11.0
is required for real-verifier integration tests. See the component READMEs for
verifier setup and separately authorized live runs. CBMC 6.9.0 has been tested
locally; the retained experiment records do not establish its historical version.

## Retained paper evidence

All twelve final result files were recovered from the original author's local
checkout, preserved byte-for-byte, and recorded with SHA-256 provenance.
Offline reproduction regenerates twenty tables; all fifteen that have submitted
counterparts match their numeric values. In particular, it recovers **118 correct
claims out of 128 combined Dafny claims** and **65 correct claims out of 72 CBMC
claims**. These are correctness counts for reported claims, not counts of
universally safe programs.

The [manifest](paper/manifest.json) distinguishes frozen raw evidence from
source adaptations and historical dirty/resumed runs. Reproducing the retained
metrics is supported; reproducing every historical model interaction bit-for-bit
is not claimed.

## Reproducibility and model access

Reanalyzing retained results and running deterministic tests should not require
an inference credential. Rerunning the model pipeline does require a supported
Copilot identity and can consume AI credits. Supply credentials through your
environment or GitHub Actions secrets, never through committed files.

Historical results are distinct from newly generated results: the same model
name and budget do not guarantee identical behavior from an evolving service.
Preserve dataset hashes, code revisions, tool versions, model identifiers,
budgets, raw outcomes, and usage for any rerun.

## Release status

This is release preparation, not a declaration that public redistribution or
paper reproduction has been approved. Before changing repository visibility:

- Resolve all missing evidence and provenance qualifications documented by the
  artifact, or state their limits explicitly.
- Obtain the repository owner's license decision and confirm redistribution
  rights and attribution for imported datasets, code, and example evidence.
- Review retained logs for credentials, account identifiers, and private data.
- Complete the documented local checks and an authorized, bounded integration
  run with the intended billing identity.

Changing visibility, publishing external Discussions, and resuming unrelated
audit campaigns are separate operations; installing the source does none of
them.