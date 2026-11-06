<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR audit onboarding

Already installed? Use the [upgrade procedure](#upgrade-an-existing-installation),
not the first-installation script.

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
   workflow-local release pins and `NFV_MODEL`, and opens a **draft installation PR**.
   It reads back the pins, model, billing permissions and required secret names,
   disables the reviewer and verifies `disabled_manually`; if blocked, leave
   the PR draft and report the failure.
   Check [repository permission policies](REFERENCE.md#repository-permission-policies)
   against the generated jobs. Update approved job-specific allowlists and their
   tests/documentation; do not disable checks or grant workflow-wide write access.

6. **Merge, then enable:** Approve the installation merge **with NFV disabled**;
   never run or require NFV on its own installation PR. Leave unrelated CI enabled.
   Keep NFV advisory after installation too: exclude its jobs from required merge
   checks and required CI aggregators; leave operational failures visible.
   After merge, have the agent verify both workflow-local runtime pins and compiled
   locks against the merged immutable package pins,
   the approved model, consistent billing permissions and required secret names.
   If anything fails, leave NFV disabled. Otherwise enable and verify `active`:

   ```text
   gh workflow enable nfv-pr-review.lock.yml --repo OWNER/repo
   gh api repos/OWNER/repo/actions/workflows/nfv-pr-review.lock.yml --jq .state
   ```

7. **Approve a separate, billable pilot:** Use a small code-change PR based on
   the installed branch. Documentation, configuration and submodule-only PRs
   without a supported source-suffix change do not start NFV.
   Verify the generated comment and `nfv-evidence` report:
   `completed: true`, no global errors and recorded property outcomes, not just
   a green job. For organization mode, confirm attribution in its billing dashboard;
   secret names and configuration alone do not prove token, policy or model access.
   The separate `review-status.json` records driver completion/submission or an
   infrastructure failure; it is not a property verdict. Infrastructure failures
   can still fail Actions checks. Keep NFV advisory, not required for merging.

NFV is a prototype. Findings are **conditional MODEL refutations, never confirmed
source bugs**; failure to refute is not evidence of correctness. Never check out
or execute inspected PR code, edit generated reports or expose secrets through
`pull_request_target`.

See the [configuration reference](REFERENCE.md#configure-github-agentic-workflows)
for permissions, optional settings and installer options.

## Upgrade an existing installation

Ask the agent to upgrade; do not rerun the first-installation script against an
existing installation. Keep the approved model, secrets and budgets. Disable
new audits and wait for all active/queued runs to finish before changing shared
configuration. Preserve the selected billing mode unless a change is approved.

Resolve the trusted NFV release once. Create an upgrade branch named
`nfv-upgrade-<first 12 characters of SHA>`, replace the package with
`gh aw add microsoft/neuro-formal-verification/workflow@<SHA> --force`, and
reapply the approved billing permissions
after package replacement, and retain intentional local customizations.
From the target checkout, run the following using `install.py` from that reviewed
NFV release (replace the script-path placeholder):

```text
python <PATH_TO_REVIEWED_INSTALL_PY> --repository OWNER/repo --pin-release
```

This reads the full immutable package SHA from both installed workflow sources,
writes matching workflow-local runtime pins and recompiles both locks with
gh-aw v0.88.7. It preserves billing/model/budgets, does not commit, and does not
enable NFV. Review and commit the sources, locks and package/action records,
then open the upgrade PR.
Recheck [repository permission policies](REFERENCE.md#repository-permission-policies)
against the regenerated jobs.
After the approved merge, validate the merged package, runtime and compiled pins
and enable NFV using the reviewed installer:

```text
python <PATH_TO_REVIEWED_INSTALL_PY> --repository OWNER/repo --enable-after-merge --pull-request <UPGRADE_PR_NUMBER>
```

The activation check also verifies the model, billing permissions, required secret
names and workflow state. Repeat a separately approved pilot; leave audits paused
on failure.

New workflows do not read the `NFV_RELEASE_SHA` repository variable; an existing
value can remain during migration and cannot override the merged pins. Older
workflow releases still require their original variable until replaced. A bare
`gh aw add`, `gh aw update` or `gh aw upgrade` does not configure NFV's runtime pin:
run `--pin-release` and review/recompile before activation. Raw templates fail
explicitly until pinned. Upgrades and rollbacks replace the reviewed workflow
sources and locks rather than changing a shared runtime variable.
