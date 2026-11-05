<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Neuro-Formal Verification

Code and data for the paper [*Neuro-Formal Verification: Agentic Language-Agnostic
Formal Program Reasoning*](https://arxiv.org/abs/2608.21516) (arXiv:2608.21516).

This repository contains two components:

- **Paper artifact:** nfv-micro-bench dataset, retained results, experiment scripts, and Dafny/CBMC backends.
  See [paper/README.md](paper/README.md) for the paper, dataset, model
  configuration, and instructions for running experiments.
- **NFV PR Auditor (prototype GitHub Agentic Workflow):** a post-merge PR
  auditor that selects nontrivial properties, tries verification
  first, then refutation with Dafny, and opens a linked issue only for accepted
  conditional model refutations. See [workflow/README.md](workflow/README.md) for setup,
  model configuration, supported languages, and examples.

To try NFV PR Auditor in another repository, follow the
[onboarding guide](workflow/ONBOARDING.md) for installing the
AW template, selecting inference billing and setting its required secrets, with
an optional fork first. The helper skill is optional.

The code and artifacts are intended for research reproduction, authorized
experimentation, and feedback on neuro-formal reasoning. The prototype reviewer
is not a substitute for source review, testing, or a security assessment.

See the [code and dataset transparency note](TRANSPARENCY.md) for intended
uses, dataset provenance, evaluation, and limitations.

## Folder structure

| Directory | Purpose |
|---|---|
| `nfv/` | Core implementation, backends, and experiment runner |
| `paper/` | Paper artifact: dataset, retained results, documentation and utilities |
| `tests/` | Core and artifact tests |
| `workflow/` | Prototype GitHub Agentic Workflow integration (`nfv-workflow` package and `aw.yml`) |
| `.github/workflows/nfv-pr-auditor.md` | NFV PR Auditor: merged-PR audits with evidence-linked issues |

A checked model refutation depends on its translation and assumptions; it is
not automatically a confirmed source-code bug. Failure to refute is not a proof
of correctness.
Verification likewise applies to the translated model under its assumptions,
not automatically to the source code.

## Contributing and support

Contributions and research feedback are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the Microsoft Contributor License
Agreement, development setup, tests, and pull request guidance, and
[SUPPORT.md](SUPPORT.md) for questions and issue reporting.

This project has adopted the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for resources and contact information.
Report security vulnerabilities privately using [SECURITY.md](SECURITY.md),
not through public GitHub issues.

## License and third-party content

Project code is available under the [MIT license](LICENSE). The paper benchmark
includes programs and derived data from
[microsoft/intent-formalization](https://github.com/microsoft/intent-formalization/tree/main/nl2postcondition-fse2024),
including HumanEval problems via EvalPlus. See [NOTICE.txt](NOTICE.txt),
[dataset attribution](paper/DATA_PROVENANCE.md), and the retained
[upstream license notices](paper/licenses) for attribution and licensing
information for that content.

## Trademarks

This project may contain trademarks or logos for projects, products, or services.
Authorized use of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must
not cause confusion or imply Microsoft sponsorship. Any use of third-party
trademarks or logos are subject to those third-party's policies.
