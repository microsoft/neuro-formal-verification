<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# FAQ

For installation and upgrades, follow [onboarding](ONBOARDING.md).

## Which billing mode should I use?

PAT is the default. Organization billing requires explicit approval, an
organization-owned target, the **Copilot CLI > Allow use of Copilot CLI billed
to the organization** policy and an allowed model. It uses the Actions token,
never a PAT fallback. A Copilot seat alone cannot bill a personal fork to an organization.
See [GitHub's billing setup](https://docs.github.com/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions).

The installer sets `copilot-requests` in three source permission scopes:
the reviewer, its `jobs.repository_context.permissions`, and the source-context
worker. All three must be `none` for PAT or `write` for organization mode.
Preserve the mode on upgrade: package replacement resets it to PAT defaults.
An approved mode change requires updating all three and recompiling both locks.

## What must the installation preserve and check?

Use gh-aw v0.88.7 without automatically replacing a client's compiler. Preserve
shared gh-aw skills/maintenance files byte-for-byte and existing action pins;
reject conflicts and unrelated generated changes.

Read back the model, both package pins, secret names, three billing scopes and
workflow state. Secret names do not prove access. First installation requires
`NFV_RELEASE_SHA` unset, including inherited values; inability to inspect them
blocks setup. Verify source/compiled gates before marking the draft ready;
keep the release unset through merge.

Disabling an unregistered workflow may return 404; `active` does not bypass the
empty-release gate. Post-merge activation disables the registered reviewer,
validates merged configuration, sets/reads back the release, then enables it.
Leave NFV gated off or disabled on delayed registration or failed checks.

## What must an upgrade handoff include?

Inspect both installed package pins on the client's **merged default branch**.
They must contain the same full 40-character SHA. The agent must print these
two commands with the **actual repository and literal verified SHA substituted**:

```text
gh variable set NFV_RELEASE_SHA --repo OWNER/repo --body <SHA>
gh variable get NFV_RELEASE_SHA --repo OWNER/repo
```

These placeholders are not a client handoff: substitute literal values, not
shell variables or the latest NFV branch tip. Say to run them after merge,
before re-enabling. Leave NFV disabled if pins or read-back cannot be verified.

## Repository permission policies

Keep NFV out of required merge checks/CI aggregators. Preserve unrelated CI and
approval protections. Infrastructure failures remain visible, including failing checks.

Compare generated **job-level** permissions with CI allowlists. In gh-aw v0.88.7,
the reviewer's `safe_outputs` and `conclusion` request `issues: write` and
`pull-requests: write`; admission needs `contents: read` and `pull-requests: write`.
Approve any job-specific exceptions and update policy tests/docs, or block setup.
Do not disable policies, hand-edit locks, grant workflow-wide write access or
grant inference jobs repository/comment writes.

Use `safe-outputs.github-token: ${{ secrets.GITHUB_TOKEN }}` in both workflows,
not inference tokens or shared `GH_AW_GITHUB_TOKEN`. For safe-output
**401 Bad credentials**, check older locks for that override and recompile.

## Which PRs are reviewed?

Supported-source PRs from same-repository branches targeting any branch, opened
non-draft or marked ready. Forks, drafts and documentation/configuration/submodule-only
changes are skipped. The workflows must exist in the target branch/merge ref;
installation does not add them to older branches.

Branch contributors must be trusted with workflow changes/secrets:
`pull_request` uses the merge ref. Inspected source is immutable data, never
executed. Reports cover the admitted snapshot, even after merge/closure,
not later commits.

## How do I check a pilot?

Get separate approval for a billable pilot on the installed branch.
Copilot-authored PRs may need **Approve and run workflows**; expired approvals
did not execute NFV.

Check the comment and `nfv-evidence/report.json`: `completed: true`, no global
errors and recorded outcomes. `review-status.json` separates infrastructure
failure from completion; `publication: submitted` means queued, not posted.
Check organization billing attribution too; a green job is insufficient.

## What do proofs, refutations and candidate fixes mean?

Results concern translated models under stated assumptions, not source correctness.
A finding is a **conditional MODEL refutation, never a confirmed source bug**.
Failure to refute is not evidence of correctness. The four property outcomes
are refuted, verified, resources exhausted, and type-check/model-validation failure.
Infrastructure failure is separate.

Shorter properties and candidate fixes get separate model checks without changing
the accepted whole property/outcome. Patches are not applied or source-verified.
Full evidence remains in the artifact when comments are compacted.

## My token expired. What should I update?

Replace the affected Actions secret in the repository running NFV:

| Secret | Purpose and replacement access |
|---|---|
| `COPILOT_GITHUB_TOKEN` | PAT billing only: fine-grained PAT with **Account permissions > Copilot Requests: Read**, from a licensed account with model access |
| `GITHUB_TOKEN` | GitHub supplies this per run; do not create or rotate an Actions secret with this name |

Use the interactive prompt for the affected secret, replacing `OWNER/repo`:

```text
gh secret set COPILOT_GITHUB_TOKEN --repo OWNER/repo
```

Update only the affected secret; never expose its value in chat, commands or logs.

## Does a 401, 403 or 404 always mean expiry?

No. A 401 can mean invalid/revoked credentials; 403 can mean permissions,
organization approval, entitlement, model policy or spending restrictions;
404 can mean a missing repository, unavailable release pin or insufficient access
to the client repository. NFV releases are public and need no separate credential.
Check the failing step and billing mode.

## Does signing into the CLIs refresh Actions secrets?

No. Local sign-in and Actions secrets are separate.

## How do I retry after fixing access?

Ask the agent for a maintainer-approved explicit rerun of the failed workflow.
Retries can cost money and use the original snapshot. Each PR has one automatic
attempt, including failures; pushes/readiness transitions do not retry it.
Preserve admission/report comments; do not reinstall to bypass them.

For legacy failures without either record, a write-access maintainer may post
`<!-- nfv-pr-attempt:OWNER/repo:PR_NUMBER:RUN_ID -->` as the exact first line,
with verified values, then the run link/reason. Exclude runs that never started a review.

## Why is there no review or report?

Check enablement, `NFV_RELEASE_SHA`, `NFV_MODEL`, source eligibility, approval and
admission comments. Installation PRs remain inert. See [report behavior](README.md).

Start with the `agent` job log and download artifacts:

```text
gh run download RUN_ID -n agent
gh run download RUN_ID -n nfv-evidence
```

Full tool output is in `sandbox/agent/logs/process-*.log`. Keep incomplete
evidence; do not publish it as a completed review or manually alter reports.

## What if the evidence artifact has expired?

Download needed evidence before retention expires. Token renewal cannot restore
deleted artifacts; a separately approved review may be needed.

## How should historical replay PRs reference upstream changes?

Use plain change numbers, not upstream references, GitHub links or mentions.
Normalize descriptions; keep originals locally. `redirect.github.com` provides
no-backlink provenance, not history erasure. See [benchmark guidance](MAINTAINER.md#nightly-nfv-micro-bench).
