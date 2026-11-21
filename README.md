<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Neuro-Formal Verification

Read the blog: [Neuro-Formal Code Review: Can Proofs Help Enforce Intent at Scale?](https://risemsr.github.io/blog/2026-11-18-neuro-formal-code-review/).

Neuro-Formal Verification (NFV) is a set of techniques to answer questions about code with high
empirical accuracy and proof-based verdicts (verification as well as refutation)
by combining neuro-symbolic agents that translate intent, code and environment
into formal models with program verifiers that reason over those models.
NFV's underlying principle is to use AI agents as translators rather than
arbiters of correctness, leaving proof-based reasoning to formal verifiers.

This repository provides two artefacts:

- **Paper and artifact:** [NFV paper](https://arxiv.org/abs/2608.21516)
  and [paper artifact](paper/README.md), including the nfv-micro-bench dataset,
  retained results and experiment instructions.
- **Neuro-formal code reviewer:** a research prototype that investigates whether
  a code change implements its intended behavior through (a)
  [intent formalization](https://arxiv.org/abs/2603.17150), turning informal intent
  into checkable properties, drawing on research that uses semantic quality
  metrics to assess formalizations, and (b) neuro-formal verification, seeking proofs or
  counterexamples in translated models. Built with GitHub Agentic Workflows.
  See the [neuro-formal code reviewer guide](workflow/README.md) and
  [setup](workflow/ONBOARDING.md).

As described in the [paper](https://arxiv.org/html/2608.21516#S1.SS1), the NFV
framework spans a spectrum from purely neural translation of intent, code and
environment to combinations with language-specific symbolic code translation
(such as [Verus for Rust](https://verus-lang.github.io/verus/guide/)) and can
target verification backends such as Dafny or Lean.
[Intent-formalization agents](https://arxiv.org/abs/2603.17150) can use tests
and specification-quality metrics to ground informal-to-formal intent translation.
In the limit, NFV reduces to formal verification: checking a formal assertion
about a closed program with no external environment in a language with a trusted
logical encoding.

The code and artifacts are intended for research reproduction, authorized
experimentation, and feedback on neuro-formal reasoning. The prototype
neuro-formal code reviewer is not a substitute for source review, testing, or a
security assessment.

See the [code and dataset transparency note](TRANSPARENCY.md) for intended
uses, dataset provenance, evaluation, and limitations.

A checked model refutation depends on its translation and assumptions; it is
not automatically a confirmed source-code bug. Failure to refute is not a proof
of correctness.
Verification likewise applies to the translated model under its assumptions,
not automatically to the source code.

## Latest completed full run

[![nfv-micro-bench: latest completed full 206-entry run](https://microsoft.github.io/neuro-formal-verification/benchmark.svg)](https://github.com/microsoft/neuro-formal-verification/discussions/76)

The card refreshes from archived benchmark statistics without repository commits.
See the linked dashboard's newest snapshot for evidence and metric definitions.

## Citation

If you use this work, please cite the [NFV paper](https://arxiv.org/abs/2608.21516):

```bibtex
@misc{lahiri2026neuroformalverification,
  title         = {Neuro-Formal Verification: Agentic Language-Agnostic Formal Program Reasoning},
  author        = {Shuvendu K. Lahiri},
  year          = {2026},
  eprint        = {2608.21516},
  archivePrefix = {arXiv},
  primaryClass  = {cs.SE},
  doi           = {10.48550/arXiv.2608.21516},
  url           = {https://arxiv.org/abs/2608.21516}
}
```

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

See [PRIVACY](PRIVACY) for data handling and telemetry opt-out instructions.

## License and third-party content

Project code, including dataset reconstruction tooling, is available under the
[MIT license](LICENSE.TXT). The **nfv-micro-bench dataset** in `paper/data` is
released under the [CDLA-Permissive-2.0 license](paper/DATA_LICENSE.md).
Include that license when sharing the dataset. Incorporated upstream material
retains its applicable licenses and attribution; CDLA does not replace them.
The paper benchmark includes programs and derived data from
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
