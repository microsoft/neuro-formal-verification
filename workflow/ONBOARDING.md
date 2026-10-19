# NFV PR audit onboarding

Setup has four steps: **choose or fork a repository, install the NFV Agentic
Workflow template, set the Copilot token, and set the private NFV read token**.
The helper skill is optional; installing it is not a prerequisite for PR audits.

NFV is a prototype implementation. It tries verification, then refutation, and
posts a generated PR comment. Proofs concern translated models under their
assumptions; findings are **conditional MODEL refutations, not confirmed source
bugs**. Failure to refute is not evidence of source correctness.

## A. Choose the repository; fork it if needed

If you have permission to install workflows and configure Actions secrets in
the repository, use it directly. Otherwise, create a fork you control and enable
**GitHub Actions** in that fork.

Install NFV in the repository **where the PRs to audit will be opened**. Installing
it in your fork does not audit PRs opened in the upstream repository.
Examples to try include mimalloc, LiteBox and Garnet; confirm the exact
`OWNER/REPO` for nvx before installing there.

## B. Add NFV as an Agentic Workflow template

From a clone of the chosen repository, have the installer or coding assistant:

- Install the NFV gh-aw package, including both the source-context and PR-review
  workflows.
- Set `NFV_RELEASE_SHA` to the same reviewed, immutable NFV commit used for the
  package, and `NFV_MODEL` to a model available to your Copilot account.
- Prepare the workflow files and generated locks for review and commit.

You do not need to install Dafny, Z3 or Python locally. The workflow provisions
its runtime. Organization policies must allow the generated Actions workflows.

<details>
<summary>Commands for the installer (PowerShell)</summary>

Use an authenticated GitHub CLI with access to the target and the NFV release.
Use gh-aw v0.88.7, the compiler version tested with this template. If it is not
already installed:

```powershell
gh extension install github/gh-aw --pin v0.88.7
```

Replace the placeholders and run from the target repository's clone:

```powershell
$TargetRepo = "OWNER/REPO"
$NFV_RELEASE_SHA = "REVIEWED_40_CHARACTER_COMMIT_SHA"
$NFV_MODEL = "APPROVED_MODEL_ID"
if ($NFV_RELEASE_SHA -notmatch '^[0-9a-f]{40}$') { throw "Set a reviewed full SHA" }
gh variable set NFV_RELEASE_SHA --repo $TargetRepo --body $NFV_RELEASE_SHA
gh variable set NFV_MODEL --repo $TargetRepo --body $NFV_MODEL
gh aw add "microsoft/neuro-formal-verification/workflow@$NFV_RELEASE_SHA"
if ($LASTEXITCODE -ne 0) { throw "Template installation failed" }
```

Review the installed Markdown, then compile:

```powershell
gh aw compile nfv-source-context nfv-pr-review --approve
if ($LASTEXITCODE -ne 0) { throw "Compilation failed" }
git status --short
git diff
```

Commit both workflow `.md` files and both `.lock.yml` files under
`.github\workflows\`, plus the generated package records and action locks under
`.github\aw\`. Review any `.gitattributes` changes. Do not commit unrelated files
or hand-edit generated locks. Keep the existing repair and spending limits.

</details>

## C. Set `COPILOT_GITHUB_TOKEN`

**The installer asks you to enter this token securely**, not paste it into chat.
Create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new?name=COPILOT_GITHUB_TOKEN&user_copilot_requests=read):

- Use an account with an active Copilot license and access to the chosen model.
- Set **Resource owner** to your personal account, not an organization.
- Under **Account permissions**, grant **Copilot Requests: Read**. Repository
  write access is not needed for this token. Set an expiration.

In the target repository, open **Settings > Secrets and variables > Actions >
New repository secret** and save it as **`COPILOT_GITHUB_TOKEN`**.
Alternatively, enter it at the interactive prompt:

```powershell
$TargetRepo = "OWNER/REPO"
gh secret set COPILOT_GITHUB_TOKEN --repo $TargetRepo
```

Replace `OWNER/REPO` with the repository from step A, including your fork's owner
if applicable. The command prompts for the token; do not supply its value as a
command-line argument.

This token authorizes billable model inference. Do not substitute the OAuth
token from `gh auth login`, put the token value in a command argument, or commit
it to a file.

## D. Set `NFV_RELEASE_TOKEN` while the NFV repository is private

This is a **separate read token for
`microsoft/neuro-formal-verification`**, not a read token for the repository
being audited.

Create a fine-grained PAT owned by someone who already has access to NFV.
Select the NFV repository's resource owner, restrict repository access to
**only `microsoft/neuro-formal-verification`**, and grant **Contents: Read-only**.
Complete any organization approval required and set an expiration.

Save it as **`NFV_RELEASE_TOKEN`** in the same target repository's Actions
secrets, using the Settings page or an interactive prompt:

```powershell
gh secret set NFV_RELEASE_TOKEN --repo $TargetRepo
```

Use the same `$TargetRepo` as step C. This command also prompts for the token.
Confirm that both secret names are present without revealing their values:

```powershell
gh secret list --repo $TargetRepo
```

The listing confirms storage, not token permissions or inference access.

The runner uses this token to download the trusted NFV implementation. Your
local `gh` login also needs NFV access to install the template. Once NFV is
public and readable by the runner's normal token, this extra secret is no
longer needed.

**After setup:** merge the installation into the intended base branch, then open
or update a small **non-draft PR in that repository**. NFV should post its
generated audit comment. Check the actual comment and the run's `nfv-evidence`
artifact, not just a green job. Fork-origin PRs may not receive secrets; do not
switch to `pull_request_target` to expose them.

An assistant can use the bundled `nfv-pr-review` skill to carry out installation;
you do not have to copy the skill into every target repository. For pipeline,
budget and evidence details, see the
[workflow README](https://github.com/microsoft/neuro-formal-verification/blob/main/workflow/README.md).
