# NFV PR audit onboarding

1. **Authenticate:** Install Git, GitHub CLI (`gh`) and Copilot CLI; authenticate
   both CLIs. For Microsoft-provided Copilot access, sign in with the account
   assigned that seat. CLI policy and model access must permit `gpt-5.6-sol`
   (default), or another model you explicitly approve.

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
   `OWNER/repo` and using `Billing: organization` only to opt in:

   ```text
   Install NFV following:
   https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/ONBOARDING.md

   Target repository: OWNER/repo
   Model: gpt-5.6-sol
   Billing: pat
   NFV release: Default
   ```

   The agent uses `workflow/install.py` (Python 3.10+, gh-aw v0.88.7), pins both
   workflows and runtime to one latest trusted NFV default-branch SHA, sets
   `NFV_RELEASE_SHA` and `NFV_MODEL`, and opens a **draft installation PR**.
   It reads back the pins, model, billing permissions and required secret names,
   disables the reviewer and verifies `disabled_manually`; if blocked, leave
   the PR draft and report the failure.
   Check [repository permission policies](README.md#repository-permission-policies)
   against the generated jobs. Update approved job-specific allowlists and their
   tests/documentation; do not disable checks or grant workflow-wide write access.

6. **Merge, then enable:** Approve the installation merge **with NFV disabled**;
   never run or require NFV on its own installation PR. Leave unrelated CI enabled.
   After merge, have the agent verify both package pins against `NFV_RELEASE_SHA`,
   the approved model, consistent billing permissions and required secret names.
   If anything fails, leave NFV disabled. Otherwise enable and verify `active`:

   ```text
   gh workflow enable nfv-pr-review.lock.yml --repo OWNER/repo
   gh api repos/OWNER/repo/actions/workflows/nfv-pr-review.lock.yml --jq .state
   ```

7. **Approve a separate, billable pilot:** Use a small code-change PR based on
   the installed branch. Verify the generated comment and `nfv-evidence` report:
   `completed: true`, no global errors and recorded property outcomes, not just
   a green job. For organization mode, confirm attribution in its billing dashboard;
   secret names and configuration alone do not prove token, policy or model access.

NFV is a prototype. Findings are **conditional MODEL refutations, never confirmed
source bugs**; failure to refute is not evidence of correctness. Never check out
or execute inspected PR code, edit generated reports or expose secrets through
`pull_request_target`.

Already installed? Use the [upgrade procedure](README.md#upgrade-an-existing-installation),
not the first-installation script. Existing installations do not switch billing
automatically. See the [configuration reference](README.md#configure-github-agentic-workflows)
for permissions, optional settings and installer options.
