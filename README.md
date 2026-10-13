# Neuro-Formal Verification

This repository contains two components:

- **Paper artifact:** datasets, experiment scripts, and Dafny/CBMC backends.
  See [paper/README.md](paper/README.md) for the paper, dataset, model
  configuration, instructions for running experiments, and table reproduction.
- **GitHub Agentic Workflow integration (prototype implementation):** a
  refutation-only audit of intended properties in closed pull requests.
  See [workflow/README.md](workflow/README.md) for setup, model configuration,
  supported languages, and examples.

## Folder structure

| Directory | Purpose |
|---|---|
| `nfv/` | Core implementation, backends, and experiment datasets |
| `paper/` | Paper artifact documentation and reproduction utilities |
| `tests/` | Core and artifact tests |
| `workflow/` | Prototype GitHub Agentic Workflow integration |
| `examples/` | Integration examples |

A checked model refutation depends on its translation and assumptions; it is
not automatically a confirmed source-code bug. Failure to refute is not a proof
of correctness.
