# Neuro-Formal Verification

Code and data for the paper [*Neuro-Formal Verification: Agentic Language-Agnostic
Formal Program Reasoning*](https://arxiv.org/abs/2608.21516) (arXiv:2608.21516).

This repository contains two components:

- **Paper artifact:** nfv-micro-bench dataset, retained results, experiment scripts, and Dafny/CBMC backends.
  See [paper/README.md](paper/README.md) for the paper, dataset, model
  configuration, and instructions for running experiments.
- **GitHub Agentic Workflow integration (prototype implementation):** a PR
  reviewer that selects nontrivial properties, tries verification first, then
  refutation with Dafny, and
  comments the result. See [workflow/README.md](workflow/README.md) for setup,
  model configuration, supported languages, and examples.

To try the reviewer in another repository, follow the
[onboarding guide](workflow/ONBOARDING.md) for installing the
AW template, selecting inference billing and setting its required secrets, with
an optional fork first. The helper skill is optional.

See the [code and dataset transparency note](TRANSPARENCY.md) for intended
uses, dataset provenance, evaluation, and limitations.

## Folder structure

| Directory | Purpose |
|---|---|
| `nfv/` | Core implementation, backends, and experiment runner |
| `paper/` | Paper artifact: dataset, retained results, documentation and utilities |
| `tests/` | Core and artifact tests |
| `workflow/` | Prototype GitHub Agentic Workflow integration (`nfv-workflow` package and `aw.yml`) |
| `.github/workflows/nfv-pr-review.md` | The NFV PR reviewer agentic workflow |

A checked model refutation depends on its translation and assumptions; it is
not automatically a confirmed source-code bug. Failure to refute is not a proof
of correctness.
Verification likewise applies to the translated model under its assumptions,
not automatically to the source code.

Contributors and coding agents: see [AGENTS.md](AGENTS.md).
