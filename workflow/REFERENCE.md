<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer configuration

Start with [onboarding](ONBOARDING.md). See the [FAQ](FAQ.md) for operational
checks and the [reviewer guide](README.md) for results and limitations.

## Configure GitHub Agentic Workflows

### Repository variables

| Variable | Default / purpose |
|---|---|
| `NFV_RELEASE_SHA` | Full trusted 40-character NFV commit matching both installed package pins; unset until first-install post-merge activation |
| `NFV_MODEL` | Approved Copilot model ID |
| `NFV_SYNTAX_ROUNDS` | 6 repairs, maximum 20 |
| `NFV_REPAIR_MODEL_VALIDATION` | `true`; retry rejected draft model validation within the syntax-repair budget |
| `NFV_PROOF_ROUNDS` | 10 repairs, maximum 30 |
| `NFV_VERIFIER_TIMEOUT` | 60 seconds per Dafny call |
| `NFV_PROPERTY_SECONDS` | 1800 seconds per property |
| `NFV_SIMPLIFY_REFUTATIONS` | `true`; shorter checked-property follow-up |
| `NFV_CHECK_FIXES` | `true`; model-checked candidate-fix follow-up |

Unset budgets retain defaults. Boolean flags accept `true` or `false`;
unset or empty enables them. Properties run sequentially within a 360-minute
job timeout. Model and budget changes require approval.

Set `NFV_REPAIR_MODEL_VALIDATION=false` to retain source-mapping, contract/helper
metadata and source-scope diagnostics without requesting repairs, then continue
Dafny verification/refutation search experimentally. Integrity checks, syntax/type
checking, backend translation and well-definedness remain mandatory. Ordinary
syntax/type and annotation-only proof repairs keep their existing budgets.

If validation still fails after syntax/type repairs, experimental models and
search results are retained separately as `experimental-translation.json` and
`experimental-search.json` with `accepted: false`. They are not accepted NFV
proofs/refutations: the official property outcome remains a failure or exhausted
search, and benchmark predictions abstain. Validated models follow normal
acceptance. This can reduce repair time and credits, but may reduce accepted
coverage; it does not disable candidate-fix follow-ups for accepted refutations.

```text
gh variable set NFV_MODEL --repo OWNER/repo --body MODEL
gh variable get NFV_MODEL --repo OWNER/repo
```

Set the runtime release only through [activation or upgrade](ONBOARDING.md).
`gh aw update` and `gh aw upgrade` do not update `NFV_RELEASE_SHA`.

### Secrets and billing

| Secret | When needed |
|---|---|
| `COPILOT_GITHUB_TOKEN` | Default PAT billing: licensed account, model access and **Copilot Requests: Read**; not an OAuth (`gho_`) token |
| `GITHUB_TOKEN` | Supplied per job by Actions; do not create a repository secret |

Organization billing is opt-in and has no PAT fallback. For eligibility,
permission scopes and mode changes, see [billing](FAQ.md#which-billing-mode-should-i-use).
NFV releases are public; neither billing mode needs a release credential.
For restrictive CI policies, see [repository permission policies](FAQ.md#repository-permission-policies).

Workflow maintenance and local CLI commands are in the
[maintainer guide](MAINTAINER.md), not needed for ordinary installation.
