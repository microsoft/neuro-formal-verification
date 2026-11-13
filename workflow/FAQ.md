<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# FAQ

For installation and upgrades, follow [onboarding](ONBOARDING.md).

## My token expired. What should I update?

Replace the affected Actions secret in the repository running NFV:

| Secret | Purpose and replacement access |
|---|---|
| `COPILOT_GITHUB_TOKEN` | PAT billing only: fine-grained PAT with **Account permissions > Copilot Requests: Read**, from a licensed account with model access |
| `NFV_RELEASE_TOKEN` | Reading private NFV releases: **Contents: Read-only** on NFV, with required organization approval |
| `GITHUB_TOKEN` | GitHub supplies this per run; do not create or rotate an Actions secret with this name |

Use the interactive prompt for the affected secret, replacing `OWNER/repo`:

```text
gh secret set COPILOT_GITHUB_TOKEN --repo OWNER/repo
gh secret set NFV_RELEASE_TOKEN --repo OWNER/repo
```

Update only the secret you need. Never put its value in chat, commands, logs or
issues. Organization billing does not use an inference PAT and never falls back
to one.

## Does a 401, 403 or 404 always mean expiry?

No. A 401 can indicate an invalid, expired or revoked credential. A 403 can also
mean missing permissions, organization approval, Copilot entitlement, model
policy or spending restrictions. A 404 for private NFV can mean missing read
access. Check the failing step and the selected billing mode before rotating
unrelated credentials or broadening permissions.

## Does signing into the CLIs refresh Actions secrets?

No. Local GitHub/Copilot sign-in and repository Actions secrets are separate.
Refreshing one does not refresh the other.

## How do I retry after fixing access?

Ask the agent to inspect the failed run and arrange a maintainer-approved
explicit rerun of that workflow. Retries can incur inference costs and use the
original event snapshot. Each PR has one automatic attempt, including failures:
another push or draft-to-ready transition does not retry it. Do not delete the
admission comment or reinstall NFV to bypass this safeguard.

## Why is there no review or report?

Check workflow enablement, `NFV_RELEASE_SHA`, `NFV_MODEL`, supported source
changes and any pending GitHub workflow approval. Installation PRs must remain
inert. Inspect the admission comment, job logs, `review-status.json` and retained
evidence: a green job alone does not establish completed analysis or publication.
See the [overview](README.md) for report behavior.

## What if the evidence artifact has expired?

Actions artifacts have finite retention, independently of token expiry. Download
needed evidence while available. Renewing a token cannot restore deleted
artifacts; an approved new review may be needed. Do not reconstruct or manually
edit generated reports.
