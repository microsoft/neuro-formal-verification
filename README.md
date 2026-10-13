# Neuro-Formal Verification

Code and data for the paper [*Neuro-Formal Verification: Agentic Language-Agnostic
Formal Program Reasoning*](https://arxiv.org/abs/2608.21516v3) (arXiv:2608.21516v3).

This repository contains two components:

- **Paper artifact:** dataset, retained results, experiment scripts, and Dafny/CBMC backends.
  See [paper/README.md](paper/README.md) for the paper, dataset, model
  configuration, and instructions for running experiments.
- **GitHub Agentic Workflow integration (prototype implementation):** a
  refutation-only audit of intended properties in merged pull requests.
  See [workflow/README.md](workflow/README.md) for setup, model configuration,
  supported languages, and examples.

## Folder structure

| Directory | Purpose |
|---|---|
| `nfv/` | Core implementation, backends, and experiment runner |
| `paper/` | Paper artifact: dataset, retained results, documentation and utilities |
| `tests/` | Core and artifact tests |
| `workflow/` | Prototype GitHub Agentic Workflow integration |

A checked model refutation depends on its translation and assumptions; it is
not automatically a confirmed source-code bug. Failure to refute is not a proof
of correctness.
