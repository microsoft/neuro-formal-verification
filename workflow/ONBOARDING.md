# NFV PR audit onboarding

Setup has four steps: **choose or fork a repository, install the NFV Agentic
Workflow template, set the Copilot token, and set the private NFV read token**.
The helper skill is optional; installing it is not a prerequisite for PR audits.

NFV is a prototype implementation. It tries verification, then refutation, and
posts a generated PR comment. Proofs concern translated models under their
assumptions; findings are **conditional MODEL refutations, not confirmed source
bugs**. Failure to refute is not evidence of source correctness.

## Onboard with a coding agent

Give a coding agent the task below, replacing `OWNER/REPO` with your target
repository. It handles the setup; you choose an approved model and enter tokens
securely. The agent needs a clone of the **target repository**, unless it already
has that checkout. A separate local clone of NFV is not required.

```text
Onboard NFV PR review in https://github.com/OWNER/REPO.
Follow the client setup in:
https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/README.md

Clone the target repository if needed. Otherwise verify that the existing
checkout belongs to this target and preserve any local changes. Start from
its default branch; do not modify upstream.

Resolve NFV dev HEAD once to a full immutable SHA. Use that same SHA for
the workflow package and NFV_RELEASE_SHA. Use main after NFV's release.
Ask me which approved Copilot model to use.

Use gh-aw v0.88.7, installing it if missing. Install the NFV package and
compile both workflows. Prepare a draft installation PR targeting this
repository's default branch, including both workflow Markdown files,
generated locks and package/action records. Review any .gitattributes
changes. Do not merge without my approval.

Guide me through secure entry of COPILOT_GITHUB_TOKEN and NFV_RELEASE_TOKEN
using GitHub settings or an interactive CLI prompt. Follow the README's
token permissions. Never request tokens in chat, expose their values or
store them locally. Verify only that the required secret names exist.

Keep the default budgets. Never check out or execute PR code. Do not create
a synthetic test fixture or start billable audits without my approval.
Report what is ready and what remains blocked. After installation reaches
the base branch and I approve a pilot PR, check its actual generated comment
and nfv-evidence artifact, not merely a green Actions job.
```

## A. Choose the repository; fork it if needed

If you have permission to install workflows and configure Actions secrets in
the repository, use it directly. Otherwise, create a fork you control and enable
**GitHub Actions** in that fork.

Install NFV in the repository **where the PRs to audit will be opened**. Installing
it in your fork does not audit PRs opened in the upstream repository.
Examples to try include mimalloc, LiteBox and Garnet; confirm the exact
`OWNER/REPO` for nvx before installing there.

When creating replay PRs, use plain change numbers rather than upstream
issue/PR references or links in titles, descriptions and new commit messages.
GitHub can otherwise add a "mentioned this pull request" event upstream; do not
rely on lack of upstream write access to prevent it. A timeline event does not
prove anyone received an email or inbox notification.
Normalize references in copied descriptions and keep the exact originals in
local evidence. This does not change NFV's generated audit reports or erase
historical mentions.

For a clickable link without a backlink, GitHub documents replacing
`github.com` with `redirect.github.com`; see
[avoiding backlinks](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/autolinked-references-and-urls#avoiding-backlinks-to-linked-references).
Avoid explicit user/team mentions in replay metadata too.

## B. Add NFV as an Agentic Workflow template

From a clone of the chosen repository, have the installer or coding assistant:

- Install the NFV gh-aw package, including both the source-context and PR-review
  workflows.
- Resolve the current HEAD of NFV's `dev` branch to a full SHA, use it for both
  the package and `NFV_RELEASE_SHA`, and set `NFV_MODEL` to a model available to
  your Copilot account. Switch to `main` after release.
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

Replace the target repository and model placeholders, then run from the target
repository's clone. No manual SHA selection is needed:

```powershell
$TargetRepo = "OWNER/REPO"
$NFV_MODEL = "APPROVED_MODEL_ID"
$NFV_REF = "dev" # Use "main" after release
$NFV_RELEASE_SHA = gh api "repos/microsoft/neuro-formal-verification/commits/$NFV_REF" --jq .sha
if ($LASTEXITCODE -ne 0 -or $NFV_RELEASE_SHA -notmatch '^[0-9a-f]{40}$') {
    throw "Could not resolve the NFV revision"
}
gh variable set NFV_RELEASE_SHA --repo $TargetRepo --body $NFV_RELEASE_SHA
if ($LASTEXITCODE -ne 0) { throw "Could not set NFV_RELEASE_SHA" }
gh variable set NFV_MODEL --repo $TargetRepo --body $NFV_MODEL
if ($LASTEXITCODE -ne 0) { throw "Could not set NFV_MODEL" }
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

The resolved SHA stays pinned even if `dev` moves later; the workflow does not
auto-update. Use the [upgrade commands below](#upgrade-an-existing-installation)
to update an existing installation. Do not repeat forking or token setup.

</details>

<details>
<summary>Optional: test another NFV revision</summary>

Before running the installation block, change its `$NFV_REF = "dev"` line to:

```powershell
$NFV_REF = "TRUSTED_TEST_REF" # Or a trusted full 40-character commit SHA
```

Use a test repository or fork. The same commands resolve that revision once
and use the resulting SHA for both the package and runtime. Do not use a
revision supplied by the PR being audited.

For an existing installation, use the upgrade procedure below with the trusted
test revision. To return to the current development default, use
`$NFV_REF = "dev"`. After release, use `$NFV_REF = "main"` instead.

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

## Upgrade an existing installation

**Repeat only the template installation and runtime pin update.** Keep the
existing repository, secrets, model and budget settings. Replace a token only
if it expired, was revoked, or needs different permissions.

Run from a clean clone of the target repository in the same PowerShell session.
First pause new audits and list existing runs:

```powershell
$TargetRepo = "OWNER/REPO"
gh workflow disable nfv-pr-review.lock.yml --repo $TargetRepo
if ($LASTEXITCODE -ne 0) { throw "Could not pause NFV" }
gh run list --repo $TargetRepo --workflow nfv-pr-review.lock.yml --limit 100
```

**Wait until all existing NFV runs have completed**, including queued/waiting
runs. Disabling a workflow does not cancel them. Do not continue while an old
run could still read the shared release variable.

Resolve the new release once and refresh the installed package:

```powershell
$NFV_REF = "dev" # Use "main" after release; a trusted full SHA also works
$NFV_RELEASE_SHA = gh api "repos/microsoft/neuro-formal-verification/commits/$NFV_REF" --jq .sha
if ($LASTEXITCODE -ne 0 -or $NFV_RELEASE_SHA -notmatch '^[0-9a-f]{40}$') {
    throw "Could not resolve the NFV revision"
}
git switch -c "nfv-upgrade-$($NFV_RELEASE_SHA.Substring(0, 12))"
if ($LASTEXITCODE -ne 0) { throw "Could not create the upgrade branch" }
gh aw add "microsoft/neuro-formal-verification/workflow@$NFV_RELEASE_SHA" --force
if ($LASTEXITCODE -ne 0) { throw "Template upgrade failed" }
gh aw compile nfv-source-context nfv-pr-review --approve
if ($LASTEXITCODE -ne 0) { throw "Compilation failed" }
git status --short
git diff
```

`--force` replaces the installed templates, so review and deliberately reapply
any local customizations. Commit the two workflow Markdown files, both generated
locks and changed package/action records described in step B. Push the branch
and merge it through your repository's normal review process. Do not advance
the runtime pin before those files reach the base branch used by your PRs.

**After that merge**, set the matching runtime pin and resume audits:

```powershell
gh variable set NFV_RELEASE_SHA --repo $TargetRepo --body $NFV_RELEASE_SHA
if ($LASTEXITCODE -ne 0) { throw "Could not update the NFV runtime pin" }
gh workflow enable nfv-pr-review.lock.yml --repo $TargetRepo
if ($LASTEXITCODE -ne 0) { throw "Could not resume NFV" }
```

Use the **same SHA resolved above**, not a fresh lookup of the branch. If using a new
shell, restore `$TargetRepo` and that exact `$NFV_RELEASE_SHA` first. If any step
fails, leave audits paused until the problem is resolved. Open or update a small
non-draft PR to check the upgraded installation.

This updates NFV, not the gh-aw compiler. A standalone `gh aw update` or
`gh aw upgrade` does not perform NFV's matching runtime-variable update.

An assistant can use the bundled `nfv-pr-review` skill to carry out installation;
you do not have to copy the skill into every target repository. For pipeline,
budget and evidence details, see the
[workflow README](https://github.com/microsoft/neuro-formal-verification/blob/main/workflow/README.md).
