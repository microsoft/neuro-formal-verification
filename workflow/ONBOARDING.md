# NFV PR audit onboarding

Follow this single flow for any target repository. The agent installs and
configures NFV, creates the installation PR, verifies the configuration and
enables the reviewer after merge. You choose the repository and model, enter
tokens securely, approve the merge and authorize a separate pilot audit.
The helper skill is optional; no local NFV clone or verifier is required.

The default release is the latest commit on NFV's default branch. The agent
resolves it once and pins the same revision for the package and runtime.
You do not need to choose a branch or SHA. Existing installations remain pinned
until explicitly upgraded; another trusted revision is an optional override.

NFV is a prototype implementation. Findings are **conditional MODEL refutations,
not confirmed source bugs**. Verification concerns translated models under their
assumptions; failure to refute is not evidence of source correctness.

## Onboard with a coding agent

### 1. Install tools and authenticate

Install **Git, GitHub CLI (`gh`) and Copilot CLI**, then authenticate both CLIs
with your GitHub account. Python 3.10+ is also needed if the agent uses the
standalone installer. The workflow provisions its own Python, Dafny and Z3.

### 2. Choose and clone the target repository

Use a repository where you can push an installation branch, open a PR, configure
Actions secrets/variables and disable or enable workflows. Otherwise, create an
approved fork or standalone test copy. Enable Actions there as needed, after
reviewing inherited workflows and organization policies.

While NFV is private, your local authenticated `gh` account also needs read access
to `microsoft/neuro-formal-verification`. This is separate from the runner's
`NFV_RELEASE_TOKEN` below.

Clone **`OWNER/repo`**, the repository where audit PRs will be opened, and start
from its clean default branch. Installing in a fork does not audit upstream PRs.
An independent copy does not reproduce GitHub's fork permissions.

### 3. Create and store the required tokens

Store these as **Actions secrets in `OWNER/repo`**, not in NFV:

| Secret | Token permissions |
|---|---|
| `COPILOT_GITHUB_TOKEN` | Fine-grained PAT from an account with an active Copilot license and access to the approved model. Personal resource owner; **Account permissions > Copilot Requests: Read**. Repository write access is not needed. |
| `NFV_RELEASE_TOKEN` | While NFV is private: a separate fine-grained PAT owned by someone with NFV access. Select NFV's resource owner, restrict access to **only `microsoft/neuro-formal-verification`**, and grant **Contents: Read-only**. Complete required organization approval. |

Set expirations. Use **Settings > Secrets and variables > Actions > New
repository secret**, or enter each value at the secure CLI prompt:

```text
gh secret set COPILOT_GITHUB_TOKEN --repo OWNER/repo
gh secret set NFV_RELEASE_TOKEN --repo OWNER/repo
```

Never paste tokens into agent chat, command arguments or local files. Do not
substitute the `gh auth login` OAuth token for the Copilot PAT. The agent may
list secret names, never values. A stored name does **not** establish token
validity, permissions, organization approval or model access.

### 4. Start the agent and request installation

Start a fresh Copilot session **inside the target checkout**:

```text
git clone https://github.com/OWNER/repo.git
cd repo
copilot
```

If already cloned, enter that checkout instead. Paste:

```text
Install NFV following workflow/ONBOARDING.md in
https://github.com/microsoft/neuro-formal-verification.

Target repository: OWNER/repo
Approved Copilot model ID: APPROVED_MODEL_ID
NFV release: Default
```

The agent must confirm the target, model and selected release before making
changes. Ask for missing choices rather than assuming a model or repository.
Use the supplied approved model without asking again. The default release is
selected automatically; do not make the user choose a branch or SHA.

### 5. Agent creates and verifies the installation PR

The agent must:

- Verify both `origin` fetch/push URLs, the target's default branch, a clean
  checkout matching the remote, NFV read access and required secret names.
  Preserve local changes; never modify upstream.
- Use **gh-aw v0.88.7**. Resolve the trusted NFV release once, install the package
  at that SHA and compile **both** `nfv-source-context` and `nfv-pr-review`.
  Keep default budgets and unrelated CI unchanged.
- Set **repository Actions variables** `NFV_RELEASE_SHA` and `NFV_MODEL`, read
  them back and compare them with the selected release and approved model.
  Variables and secret names are safe to display; secret values are not.
- Commit and push only installation files, then **open a draft PR** targeting
  the target's default branch. Include both workflow Markdown files, both
  generated locks and package/action records; review `.gitattributes` changes.
  Do not assume a fixed number of changed files or leave PR creation to the user.
- Disable `nfv-pr-review.lock.yml` and verify **`disabled_manually`** before the
  PR can be marked ready or merged. If registration or disabling fails, leave
  the PR draft and report the block. Do not require an NFV check on this PR.

Before asking for merge approval, the agent must report observed values:

| Check | Required confirmation |
|---|---|
| Target and base | Exact `OWNER/repo`, default branch and installation PR URL |
| `NFV_RELEASE_SHA` | Read-back value equals the chosen full SHA |
| `NFV_MODEL` | Read-back value equals the approved model ID |
| Package pins | Both installed workflow `.md` files have `source:` pins ending in `workflow@` followed by that same SHA |
| Secrets | Required names present; token validity/model access not yet proven |
| Reviewer | `disabled_manually`; installation PR still draft |

Check package pins from the installation PR's captured head before merge.
Never check out or execute inspected PR code.

### 6. Enable the reviewer

Review the installation PR and approve its merge. The agent may mark it ready
and merge only with your approval, keeping NFV disabled throughout. A draft
alone does not protect the `ready_for_review` event. You can also merge it
yourself and tell the agent to continue.

After merge, the agent must verify the merge is retained on the actual default
branch, **not an assumed `main` branch**. Read back the two variables and required
secret names again. Check both package pins from the merged default-branch
snapshot, compare the model with the user's approval, then enable NFV and
verify **`active`**.

The standalone installer's `--enable-after-merge` command performs the merge,
package/runtime, model-presence and secret-name checks before activation; the
agent must also compare the observed model with the approved model. Report
the target, base SHA, release/model values, matching package pins, secret-name
presence and final workflow state. Do not claim activation from a successful
command alone. If any check fails, leave NFV disabled and explain the block.

### 7. Approve a separate pilot and its billable audit

After activation, approve a **separate small implementation-change PR**, parented
on the installed base branch, and its billable inference. An empty commit or
documentation-only dummy PR is not a meaningful audit pilot.

The agent checks the actual generated PR comment and the run's `nfv-evidence`
artifact: `completed: true`, no global errors and recorded property outcomes
with native evidence. A green Actions job alone is insufficient. Report verified,
refuted, resources exhausted and type-check/audit failure outcomes honestly;
do not increase budgets or claim source correctness.

For the first pilot, use a branch in the target repository. External fork-origin
PRs may not receive secrets; never switch to `pull_request_target` to expose them.
Never manually edit generated NFV report text.

## Install directly, without an agent

<details>
<summary>Optional manual reference: standalone installer and activation</summary>

Use [install.py](install.py) with Python 3.10+, Git and authenticated `gh`; no extra
Python packages are needed. From the clean target default-branch checkout, after
storing secrets, download the installer outside the checkout at the selected
immutable release. Linux/bash example:

```bash
TargetRepo="OWNER/repo"
Model="APPROVED_MODEL_ID"
Release="$(gh api repos/microsoft/neuro-formal-verification/commits/HEAD --jq .sha)" &&
    [[ "$Release" =~ ^[0-9a-f]{40}$ ]] &&
    Installer="$(mktemp)" &&
    gh api "repos/microsoft/neuro-formal-verification/contents/workflow/install.py?ref=$Release" \
        --jq '.content | @base64d' > "$Installer"
```

Stop if downloading fails. Review the script, then run in the same shell:

```bash
python3 "$Installer" --repository "$TargetRepo" --model "$Model" --ref "$Release"
```

The installer installs the pinned compiler if absent, sets and reads back the
variables, compiles both workflows, commits/pushes installation files, opens a
draft PR and disables the reviewer. It does not merge or launch an audit.
It refuses an existing installation or dirty checkout and does not replace a
different installed compiler. Preserve partial work and resolve reported errors;
do not blindly reinstall.

If workflow registration blocked disabling, retain the draft and retry only:

```bash
python3 "$Installer" --repository "$TargetRepo" --verify-bootstrap --pull-request 123
```

Replace `123` with the installation PR number. Verify the step 5 checklist, then
approve and merge while NFV remains disabled. After merge:

```bash
python3 "$Installer" --repository "$TargetRepo" --enable-after-merge --pull-request 123
```

Keep the installer until activation is complete. On Windows, use `python` instead
of `python3` if appropriate. For an explicit trusted test release, replace the
initial `HEAD` lookup with that trusted revision; never use a PR-supplied ref.

The following read-only checks work in PowerShell or bash:

```text
gh variable list --repo OWNER/repo
gh secret list --repo OWNER/repo
gh api repos/OWNER/repo/actions/workflows/nfv-pr-review.lock.yml --jq .state
```

For package pins, read `.github/workflows/nfv-source-context.md` and
`.github/workflows/nfv-pr-review.md` at the captured installation head before
merge and the actual default-branch SHA afterward. Compare each `source:` revision
to `NFV_RELEASE_SHA`; do not assume `main` or reuse a SHA from an example.

</details>

## Historical replay PRs

Use plain change numbers in replay titles, descriptions and new commit messages,
not upstream issue/PR references or direct GitHub links. Normalize copied
descriptions, retain exact originals in local evidence and avoid user/team
mentions. For clickable original-PR links, use GitHub's documented
[`redirect.github.com` no-backlink URL](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/autolinked-references-and-urls#avoiding-backlinks-to-linked-references).
This does not make public replays private, edit generated reports or erase
historical references. Lack of upstream write access does not prevent backlinks.

## Upgrade an existing installation

Ask the agent to upgrade using this guide. Keep the existing repository, secrets,
approved model and budgets; replace tokens only when necessary. Do not rerun
the first-installation script against an existing installation.

The agent disables NFV and waits for **all active/queued runs to finish** before
changing shared configuration. It resolves the trusted release once, replaces
the installed package with `gh aw add ... --force`, recompiles both workflows
and opens an upgrade PR. Review and retain intentional local customizations.

After the approved merge, update `NFV_RELEASE_SHA` to that same resolved SHA,
verify both merged package pins, model and required secret names, enable NFV
and read back `active`. Repeat the separate approved pilot. Leave audits paused
on any failure. A standalone `gh aw update` or `gh aw upgrade` does not update
NFV's runtime variable.

For pipeline, budgets and evidence details, see the [workflow README](README.md).
