<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV code-review onboarding

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

5. **Install post-merge review:** NFV reviews eligible PRs after they merge into
   the default branch and opens a linked issue only when model refutations exist.
   It does not audit PR updates before merge.

   Start `copilot` in the target checkout and paste this, replacing `OWNER/repo`
   and `MODEL`. Use
   `Billing: organization` only to opt in:

   ```text
   Install NFV following:
   https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/ONBOARDING.md

   Target repository: OWNER/repo
   Model: MODEL
   Billing: pat
   NFV release: Default
   ```

   The agent uses `workflow/install.py` (Python 3.10+, gh-aw v0.88.7), pins both
   selected reviewer, shared source-context helper and runtime to one latest
   trusted NFV default-branch SHA, sets
   `NFV_RELEASE_SHA` and `NFV_MODEL`, and opens a **draft installation PR**.
   It reads back the pins, model, billing permissions and required secret names,
   disables the selected reviewer and, for post-merge, its push trigger, and
   verifies `disabled_manually`; if blocked, leave
   the PR draft and report the failure.
   Check [repository permission policies](REFERENCE.md#repository-permission-policies)
   against the generated jobs. Update approved job-specific allowlists and their
   tests/documentation; do not disable checks or grant workflow-wide write access.

6. **Merge, then enable:** Approve the installation merge **with NFV disabled**;
   never run or require NFV on its own installation PR. Leave unrelated CI enabled.
   Keep NFV advisory after installation too: exclude its jobs from required merge
   checks and required CI aggregators; leave operational failures visible.
   After merge, have the agent verify all selected package pins against `NFV_RELEASE_SHA`,
   the approved model, consistent billing permissions and required secret names.
   If anything fails, leave NFV disabled. Otherwise enable and verify `active`:

   Use the installer's `--enable-after-merge --pull-request NUMBER` phase.
   It rejects mixed-mode files or an active unselected reviewer, creates the
   post-merge issue label when needed, and enables only the selected reviewer
   and its trigger. The installation PR itself is never audited.

7. **Approve a separate, billable pilot:** Use a small code-change PR based on
   the installed branch. Documentation, configuration and submodule-only PRs
   without a supported source-suffix change do not start NFV.
   In post-merge mode, approve merging the pilot before its billable audit.
   A trusted default-branch push trigger dispatches the merged PR's immutable
   head snapshot; fork-origin PR code is never checked out or executed.
   Verify the selected run and `nfv-evidence` report:
   `completed: true`, no global errors and recorded property outcomes, not just
   a green job. Post-merge runs with accepted refutations must publish one linked
   issue; completed runs without refutations retain evidence and publish nothing.
   For organization billing,
   confirm attribution in its billing dashboard;
   secret names and configuration alone do not prove token, policy or model access.
   The separate `review-status.json` records driver completion/submission or an
   infrastructure failure; it is not a property verdict. Infrastructure failures
   can still fail Actions checks. Keep NFV advisory, not required for merging.

NFV is a prototype. Findings are **conditional MODEL refutations, never confirmed
source bugs**; failure to refute is not evidence of correctness. Never check out
or execute inspected PR code, edit generated reports or expose secrets through
`pull_request_target`.

Already installed? Use the [upgrade procedure](REFERENCE.md#upgrade-an-existing-installation)
and installer `--upgrade`. It disables and removes legacy PR-review workflows.
The existing model, billing and budgets are preserved unless explicitly changed.
See the [configuration reference](REFERENCE.md#configure-github-agentic-workflows)
for permissions, optional settings and installer options.
