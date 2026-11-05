<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer

A prototype GitHub Agentic Workflow based on
[*Neuro-Formal Verification: Agentic Language-Agnostic Formal Program Reasoning*](https://arxiv.org/abs/2608.21516).

For each eligible PR, NFV selects up to ten intended properties, translates
relevant source into Dafny, and attempts verification and refutation. It posts
one evidence-linked comment without executing PR code.

Results concern translated models under stated assumptions. Findings are
**conditional MODEL refutations, not confirmed source bugs**; failure to refute
does not establish correctness.

**Installation:** [Onboarding](ONBOARDING.md).

<a id="upgrade-an-existing-installation"></a>

**More:** [Configuration](REFERENCE.md#configure-github-agentic-workflows),
[upgrades](REFERENCE.md#upgrade-an-existing-installation), and
[technical reference](REFERENCE.md).
