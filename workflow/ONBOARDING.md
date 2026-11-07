<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR audit onboarding

Already installed? Use the [upgrade procedure](#upgrade-an-existing-installation),
not the first-installation script.

NFV audits non-draft, supported-source PRs into the onboarded repository's default
branch, including forks and external contributors. Audits start on opening or
draft-to-ready transitions, not later pushes. External contributors can trigger
billable inference under the configured limits. The `pull_request_target`
workflow runs trusted base-branch code and never executes inspected PR code.

Review [data handling and telemetry opt-outs](../PRIVACY) before using hosted
inference or enabling the workflows.

1. **Authenticate:** Install Git, GitHub CLI (`gh`) and Copilot CLI; authenticate
   both CLIs. For Microsoft-provided Copilot access, sign in with the account
   assigned that seat. Select an approved model ID permitted by that account's
   CLI policy and model access.

2. **Confirm access:** While NFV is private, you need read access to
   `microsoft/neuro-formal-verification`.

3. **Clone the target:** Start from the clean default branch of `OWNER/repo`,
   where you can manage Actions, secrets and variables. An approved fork is
   fine, but its installation audits PRs in that fork, not upstream.

4. **Choose billing and enter secrets:**

   | Mode | Target and prerequisites | Inference secret |
   |---|---|---|
   | `pat` (default) | Personal or organization repository; licensed Copilot account with model access | `COPILOT_GITHUB_TOKEN`: fine-grained PAT with **Account permissions > Copilot Requests: Read** |
   | `organization` (opt-in) | Organization-owned repository; administrator enables **Copilot CLI > Allow use of Copilot CLI billed to the organization** and permits the model | None; uses the per-run Actions token, ignores inference PATs and never falls back to one |

   Both modes also need `NFV_RELEASE_TOKEN` while NFV is private: a token with
   **Contents: Read-only** access to NFV and any required organization approval.
   Enter each required secret using `gh secret set SECRET --repo OWNER/repo`
   or **Settings > Secrets and variables > Actions**; never paste tokens into chat.

   Organization ownership does not opt in automatically. A personal fork cannot
   bill Microsoft directly, even with a Microsoft-provided seat.
   See [GitHub's billing setup](https://docs.github.com/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions).

5. **Install:** Start `copilot` in the target checkout and paste this, replacing
   `OWNER/repo` and `MODEL`, and using `Billing: organization` only to opt in:

   ```text
   Install NFV following:
   https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/ONBOARDING.md

   Target repository: OWNER/repo
   Model: MODEL
   Billing: pat
   NFV release: Default
   ```

   The agent uses `workflow/install.py` (Python 3.10+, gh-aw v0.88.7), pins both
   workflows and runtime to one latest trusted NFV default-branch SHA, sets
   `NFV_RELEASE_SHA` and `NFV_MODEL`, and opens a **draft installation PR**.
   It reads back the pins, model, billing permissions and required secret names,
   and sets and verifies `NFV_ENABLED=false` before publishing the PR.
   Registered reviewers are also disabled and checked for `disabled_manually`.
   On first installation, GitHub may return HTTP 404 until merge; the verified
   pause gate keeps NFV inactive anyway. Other failures block publication.
   Check [repository permission policies](REFERENCE.md#repository-permission-policies)
   against the generated jobs. Update approved job-specific allowlists and their
   tests/documentation; do not disable checks or grant workflow-wide write access.

6. **Merge, then enable:** Approve the installation merge **with NFV paused**;
   never run or require NFV on its own installation PR. Leave unrelated CI enabled.
   Keep NFV advisory after installation too: exclude its jobs from required merge
   checks and required CI aggregators; leave operational failures visible.
   After merge, have the agent verify both package pins against `NFV_RELEASE_SHA`,
   the approved model, consistent billing permissions and required secret names.
   If anything fails, leave NFV disabled. Otherwise enable and verify `active`:

   ```text
   gh workflow enable nfv-pr-review.lock.yml --repo OWNER/repo
   gh api repos/OWNER/repo/actions/workflows/nfv-pr-review.lock.yml --jq .state
   gh variable set NFV_ENABLED --repo OWNER/repo --body true
   gh variable get NFV_ENABLED --repo OWNER/repo
   ```

   `workflow/install.py --enable-after-merge --pull-request NUMBER --repository OWNER/repo`
   performs the merged-snapshot checks and activation. Enabling the workflow
   alone does not clear the installer's pause latch.

7. **Approve a separate, billable pilot:** Use a small code-change PR based on
   the installed branch, opened non-draft or transitioned from draft to ready.
   Later pushes do not start another audit; toggle draft to ready for an explicit
   rerun. Documentation, configuration and submodule-only PRs
   without a supported source-suffix change do not start NFV.
   Verify the generated comment and `nfv-evidence` report:
   `completed: true`, no global errors and recorded property outcomes, not just
   a green job. For organization mode, confirm attribution in its billing dashboard;
   secret names and configuration alone do not prove token, policy or model access.
   The separate `review-status.json` records driver completion/submission or an
   infrastructure failure; it is not a property verdict. Infrastructure failures
   can still fail Actions checks. Keep NFV advisory, not required for merging.
   An outdated snapshot records `status: superseded` with `completed: false`,
   not an audit verdict or a successful pilot; it posts no report.

NFV is a prototype. Findings are **conditional MODEL refutations, never confirmed
source bugs**; failure to refute is not evidence of correctness. Never check out
or execute inspected PR code, edit generated reports or expose secrets through
`pull_request_target`.

See the [configuration reference](REFERENCE.md#configure-github-agentic-workflows)
for permissions, optional settings and installer options.

## Upgrade an existing installation

Ask the agent to use the installer's explicit upgrade mode, not its
first-installation mode:

```text
python workflow/install.py --repository OWNER/repo --upgrade --ref <SHA>
```

Run this trusted-release script in a clean target default-branch checkout.
It uses gh-aw v0.88.7, reads the existing model and billing mode, preserves shared
gh-aw skill/maintenance files and other action-pin records, and opens a paused
draft upgrade PR. It does not change the runtime pin before merge.
Keep the approved model, secrets and budgets. Disable
new audits and wait for all active/queued runs to finish before changing shared
configuration. Preserve the selected billing mode unless a change is approved.

Resolve the trusted NFV release once. The upgrade mode replaces the package with
`gh aw add ... --force` and recompiles both workflows with the existing billing
permissions. Retain intentional NFV workflow customizations and review the diff.
Do not use bare `gh aw add/compile` in a mixed-version repository: they can
rewrite shared skills, maintenance schedules and action locks.
Recheck [repository permission policies](REFERENCE.md#repository-permission-policies)
against the regenerated jobs.
After the approved merge, update `NFV_RELEASE_SHA` and read it back. Replace
`OWNER/repo` with the target repository and `<SHA>` with the same full trusted
40-character SHA used by both merged workflow package pins:

```text
gh variable set NFV_RELEASE_SHA --repo OWNER/repo --body <SHA>
gh variable get NFV_RELEASE_SHA --repo OWNER/repo
```

These are generic documentation examples, not a completed client handoff.
After the approved upgrade merge, the agent must inspect both installed workflow
package pins on the client's default branch and confirm they contain the same
full 40-character SHA. Then show both commands above in one copy/paste-ready
code block with the actual client repository and verified SHA substituted.
Do not leave `OWNER/repo`, `<SHA>` or shell-variable substitutions for the user
to resolve, and do not use the latest NFV branch tip instead of the installed
release. If the pins disagree or cannot be verified, report the block and leave
NFV disabled rather than guess. State that the commands are for the merged
upgrade and must run before NFV is re-enabled.

Verify the returned SHA matches both merged package pins, and verify the model,
billing permissions and required secret names before enabling NFV and reading
back `active`. Clear and read back `NFV_ENABLED=true` only after those checks;
the installer's `--enable-after-merge --pull-request NUMBER` mode verifies the
merged upgrade, aligns the runtime pin and activates it.
Repeat a separately approved pilot; leave audits paused on failure.
`gh aw update` and `gh aw upgrade` do not update NFV's runtime variable.
