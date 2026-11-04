# NFV PR audit onboarding

1. **Install and authenticate:** Install Git, GitHub CLI (`gh`) and Copilot CLI.
   Authenticate both CLIs with your GitHub account.

   For Microsoft-provided Copilot access, use the account assigned that Copilot
   seat. Start `copilot`, use `/login` to sign in and `/model` to select
   `gpt-5.6-sol` if available. Organization policies must allow CLI access and
   the model. A Microsoft-owned checkout alone does not select Microsoft billing.

2. **Confirm NFV access:** While NFV is private, your account needs read access
   to `microsoft/neuro-formal-verification`.

3. **Choose and clone your target:** Use a repository where you can manage
   Actions, secrets and variables, or create an approved fork/test copy. Clone
   `OWNER/repo` locally and start from its clean default branch. Audit PRs must
   be opened in this repository; a fork's installation does not audit upstream PRs.

4. **Choose billing and set the required secrets:** **PAT billing is the
   default**, including for personal repositories. Create `COPILOT_GITHUB_TOKEN`,
   a fine-grained PAT from a licensed Copilot account with **Account permissions >
   Copilot Requests: Read** and access to your chosen model:

   ```text
   gh secret set COPILOT_GITHUB_TOKEN --repo OWNER/repo
   ```

   To opt into **organization billing**, the target must be organization-owned
   and an administrator must enable **Copilot CLI > Allow use of Copilot CLI
   billed to the organization** and permit the model. Select `organization`
   in step 5; no `COPILOT_GITHUB_TOKEN` is needed or used for inference, and
   there is no automatic PAT fallback.

   **Both modes**, while NFV is private, need a separate `NFV_RELEASE_TOKEN`
   with **Contents: Read-only** access to private NFV and any required
   organization approval:

   ```text
   gh secret set NFV_RELEASE_TOKEN --repo OWNER/repo
   ```

   Alternatively, use **Settings > Secrets and variables > Actions > New
   repository secret**. Never paste token values into agent chat.

   See [GitHub's organization-billing setup](https://docs.github.com/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions).

5. **Ask the agent to install:** Start `copilot` inside the target checkout
   and paste the following, replacing `OWNER/repo`:

   ```text
   Install NFV following:
   https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/ONBOARDING.md

   Target repository: OWNER/repo
   Model: gpt-5.6-sol (default)
   Billing: pat (default; use organization to opt in)
   NFV release: Default
   ```

   Use `gpt-5.6-sol` unless you approve another model; the selected billing
   identity must have access. The agent uses `workflow/install.py` (Python 3.10+)
   with `--billing organization` only when explicitly selected; otherwise it
   retains PAT billing. It resolves the latest NFV default-branch commit
   once, pins the package and runtime to it, and installs both workflows using
   gh-aw v0.88.7. It creates and pushes a **draft installation PR**, sets and
   reads back `NFV_RELEASE_SHA` and `NFV_MODEL`, confirms both package pins,
   billing permissions and required secret names, and disables the reviewer.
   If it cannot verify `disabled_manually`, it must leave the PR draft and
   report the block.
   Secret-name presence does not prove token validity or model access.

6. **Enable the reviewer:** Review and approve merging the installation PR
   **with NFV still disabled**; never run NFV on its own installation PR or
   require its check. Leave unrelated CI enabled. After merge, ask the agent to
   confirm both merged package pins match `NFV_RELEASE_SHA`, `NFV_MODEL` matches
   your approved model, both workflows agree on billing and the required secret
   names for that mode exist. Leave NFV disabled if any check fails. Then
   enable and verify:

   ```text
   gh workflow enable nfv-pr-review.lock.yml --repo OWNER/repo
   gh api repos/OWNER/repo/actions/workflows/nfv-pr-review.lock.yml --jq .state
   ```

   Expected state: **`active`**. Alternatively, use **GitHub > Actions >
   NFV PR reviewer > Enable workflow**, then verify the state above.

7. **Approve a separate pilot:** Authorize a small code-change PR, parented on
   the installed base branch, and its billable audit. Have the agent check the
   actual generated NFV comment and `nfv-evidence` report with `completed: true`,
   no global errors and recorded property outcomes, not merely a green Actions
   job. Findings are **conditional MODEL refutations, never confirmed source
   bugs**; failure to refute is not evidence of correctness.
   For organization billing, also confirm usage attribution in the organization's
   billing dashboard; configuration checks do not prove policy or model access.

NFV is a prototype implementation. Never check out or execute inspected PR code,
edit generated reports or expose secrets through `pull_request_target`.
For optional manual installation and upgrades, see the
[workflow reference](README.md#configure-github-agentic-workflows).
