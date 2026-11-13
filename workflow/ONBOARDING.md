<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR audit onboarding

Already installed? Use the [upgrade procedure](#upgrade-an-existing-installation),
not the first-installation script.

For expired tokens and other common problems, see the [FAQ](FAQ.md).

NFV audits non-draft, supported-source PRs from branches in the onboarded
repository targeting any branch. Fork PRs are skipped. Audits start on opening
or draft-to-ready transitions, once automatically per PR, not later pushes.
The `pull_request` workflow runs from the PR merge ref, so contributors allowed
to push repository branches must be trusted with workflow changes and repository
secrets. NFV installs its pinned release and never executes inspected PR source.
The workflows must exist in the target branch/merge ref; installation does not
add them to older branches.

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
   workflows to one latest trusted NFV default-branch SHA, sets `NFV_MODEL`,
   and opens a **draft installation PR**. Leave `NFV_RELEASE_SHA` unset until
   post-merge activation: both inference sessions are gated off while it is
   empty. The installer refuses a repository or inherited organization runtime
   pin rather than deleting it; inability to inspect inherited variables also
   blocks bootstrap. It verifies the source and compiled gates, package pins, model, billing
   permissions and required secret names before handoff.
   The version check accepts gh-aw's version on stdout or stderr. Existing
   gh-aw skills and maintenance schedules are preserved byte-for-byte; the
   shared action lock retains existing pins and only adds missing entries.
   Conflicting pins or other unexpected changes block the install.
   Check [repository permission policies](REFERENCE.md#repository-permission-policies)
   against the generated jobs. Update approved job-specific allowlists and their
   tests/documentation; do not disable checks or grant workflow-wide write access.

6. **Merge, then enable:** Approve the installation merge **with inference gated off**;
   never run or require NFV on its own installation PR. Leave unrelated CI enabled.
   Keep NFV advisory after installation too: exclude its jobs from required merge
   checks and required CI aggregators; leave operational failures visible.
   GitHub may not register the reviewer until it reaches the default branch;
   a pre-merge `gh workflow disable` can therefore return 404. Registration as
   `active` does not bypass the empty-release gate.
   After merge, use the installer's `--enable-after-merge` phase. It disables
   the now-registered reviewer, validates the merged gates and matching package
   pins, model, billing and required secret names, then sets `NFV_RELEASE_SHA`
   to the installed release and reads it back before enabling and verifying
   `active`. If registration is delayed or any check fails, leave NFV gated
   off or API-disabled and retry activation; do not set the runtime pin early.

7. **Approve a separate, billable pilot:** Use a small code-change PR based on
   the installed branch, opened non-draft or transitioned from draft to ready.
   The admission comment reserves the PR's single automatic attempt, even if it
   fails. Later pushes and repeated readiness transitions do not cancel or
   restart it. Retry a failed attempt only through an explicit workflow rerun.
   The completed report is posted even if the PR has merged. Documentation,
   configuration and submodule-only PRs
   without a supported source-suffix change do not start NFV.
   Copilot-authored PRs may require a maintainer to **Approve and run workflows**
   after reviewing the change, even on same-repository branches. Approval-expired
   runs never executed NFV. Keep GitHub's approval protection enabled; request
   and approve a fresh eligible attempt rather than treating expiry as a model failure.
   Verify the generated comment and `nfv-evidence` report:
   `completed: true`, no global errors and recorded property outcomes, not just
   a green job. For organization mode, confirm attribution in its billing dashboard;
   secret names and configuration alone do not prove token, policy or model access.
   The separate `review-status.json` records driver completion/submission or an
   infrastructure failure; it is not a property verdict. Infrastructure failures
   can still fail Actions checks. Keep NFV advisory, not required for merging.
   Both collectors analyze the admitted event's immutable source snapshot;
   later PR state or source changes do not silently discard the attempt.

NFV is a prototype. Findings are **conditional MODEL refutations, never confirmed
source bugs**; failure to refute is not evidence of correctness. Never check out
or execute inspected PR source, edit generated reports or expose secrets.

See the [configuration reference](REFERENCE.md#configure-github-agentic-workflows)
for permissions, optional settings and installer options.

## Optional public dashboard

Anyone using NFV can report their onboarded public repository by opening
a [Discussion](https://github.com/microsoft/neuro-formal-verification/discussions)
with this exact title:

```text
NFV PR Audit onboarded on OWNER/repo
```

Only validated public repositories are listed on the
[existing dashboard](https://shuvendu-lahiri.github.io/nfv_dashboard/registered/).
While NFV is private, creating this Discussion requires access to NFV.
Registration does not install or activate NFV, start an audit, or change billing.
No target-repository admin permission or maintainer approval is required.
Registrations are self-reported, not verified owner endorsement or proof of a
healthy installation. Describe problems or ask for help in the Discussion replies;
the dashboard tracks observed workflow health and summary availability.
Close all matching registration Discussions to withdraw.

The dashboard's [README](https://github.com/shuvendu-lahiri/nfv_dashboard#optional-public-repository-registration)
describes nightly activation and collector-owned read access. No reporting secret
or cross-repository write permission is needed in the audited repository.
The dashboard repository can remain private; Pages publication is separately
opt-in and is not required for collection or local viewing. A private source
repository does not itself provide private Pages access.
See [PRIVACY](../PRIVACY#optional-public-dashboard) before opting in.

## Upgrade an existing installation

Ask the agent to upgrade; do not rerun the first-installation script against an
existing installation. Keep the approved model, secrets and budgets. Disable
new audits and wait for all active/queued runs to finish before changing shared
configuration. Preserve the selected billing mode unless a change is approved.
Disabling a workflow does not cancel its runs; let them finish rather than
cancelling an audit that has started.

Resolve the trusted NFV release once, replace the package with
`gh aw add microsoft/neuro-formal-verification/workflow@<SHA> --force`, recompile
both workflows and open an upgrade PR. Reapply the approved billing permissions
after package replacement, and retain intentional local customizations.
The admission job needs only `contents: read` and `pull-requests: write`; keep its
reservation writes separate from both inference jobs. Preserve the existing
organization billing permissions in the reviewer, reusable caller and worker.
Recheck [repository permission policies](REFERENCE.md#repository-permission-policies)
against the regenerated jobs.

Existing completed NFV bot reports prevent another automatic audit. For a
legacy failed attempt without a report or admission record, a maintainer with
repository write access can record it before enabling the upgraded workflow.
Post a PR comment with this exact first-line format, substituting the repository,
PR number and verified historical Actions run ID:
`<!-- nfv-pr-attempt:OWNER/repo:PR_NUMBER:RUN_ID -->`.
Include a run link and the reason below it. The admission gate verifies a
non-bot record author's current write permission. Do not seed records for runs
that never started an audit merely because GitHub created a skipped run.

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
back `active`. Repeat a separately approved pilot; leave audits paused on failure.
`gh aw update` and `gh aw upgrade` do not update NFV's runtime variable.
