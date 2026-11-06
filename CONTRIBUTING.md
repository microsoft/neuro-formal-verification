<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Contributing

This project welcomes contributions and suggestions. Most contributions require
you to agree to a Contributor License Agreement (CLA) declaring that you have the
right to, and actually do, grant us the rights to use your contribution. For
details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, the Microsoft CLA bot automatically determines
whether an agreement is required and reports its status. Follow the bot's
instructions; you will only need to agree once across repositories using our
CLA. Maintainers must not merge contributions until the `license/cla` check
confirms that all CLA requirements
are met. If that check is missing, maintainers must resolve the integration
before accepting contributions.

This project has adopted the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the
[Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with additional
questions or comments.

## Getting started

The repository contains a paper artifact and a prototype GitHub Agentic Workflow
reviewer. Read the relevant setup guide:

- [Paper artifact and experiments](paper/README.md), including [tool setup](paper/SETUP.md).
- [Prototype reviewer](workflow/README.md) and its [onboarding guide](workflow/ONBOARDING.md).

For development, use Python 3.11 or later. From the repository root:

```shell
python -m pip install -e ".[inference]" -e workflow
python -m unittest discover -s tests
python -m unittest discover -s workflow/tests
```

Tests that require Dafny skip when it is unavailable. Follow the component setup
guides for verifier installation and inference configuration before running
experiments; live inference can incur usage charges.

## Pull requests

Branch from `dev` and target `dev`. Keep changes focused, include relevant tests,
and update the affected user-facing documentation. See [AGENTS.md](AGENTS.md)
for layout, coding conventions, verifier invariants, and workflow compilation
instructions.

Add the language-appropriate Microsoft copyright and MIT license header to
Microsoft-authored source files. Preserve all existing copyright and license
notices; do not add Microsoft ownership notices to third-party contributions.

Preserve the checksum-pinned dataset and retained results under `paper/data`
and `paper/results`; use separate output files for new experiments. A checked
model refutation is conditional on its translation and assumptions, not a
confirmed source-code bug. Model verification is not proof of source correctness.
Keep those distinctions in code, tests, and documentation.

Do not include credentials, personal files, confidential content, or generated
scratch artifacts in a contribution. Do not execute inspected PR-head code when
working on the reviewer. Report security vulnerabilities privately as described
in [SECURITY.md](SECURITY.md), not in a public issue or pull request.

Preserve the required notices in `LICENSE.TXT`, `NOTICE.txt` and `PRIVACY`.
Keep the compatibility copy `LICENSE` identical to `LICENSE.TXT`. Do not commit
pre-built executables, libraries or packages, Microsoft product icons, or
unauthorized branding. Discuss substantial changes in an issue before starting;
see [SUPPORT.md](SUPPORT.md) for the project's support expectations.

## Before publishing a release

Maintainers must obtain naming clearance, authoring-team permission for included
code or data from other Microsoft teams, and applicable legal/IP, privacy and
announcement approvals before publication. Keep approval records in the approved
review system, not in this public repository.

Review the exact release snapshot for sensitive information, internal-only
comments, unauthorized branding and pre-built binaries. Check third-party notices,
license copies, telemetry opt-outs and the CLA integration, including scanner
coverage for otherwise skipped file types. Preserve dataset and result bytes;
record any tooling changes in the manifest without replacing original provenance.

Publish only from a Microsoft organization repository. Promote reviewed `dev`
changes to `main` by squash merge, not by exposing intermediate development
commits. Documentation, license notices and passing CI are not release approval.
