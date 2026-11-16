<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer onboarding

Already installed? Use [upgrades](#upgrade-an-existing-installation).
See [FAQ](FAQ.md) for access and troubleshooting, [configuration](REFERENCE.md#configure-github-agentic-workflows)
for variables, and [PRIVACY](../PRIVACY) for data handling.

1. **Prepare.** Install Git, GitHub CLI, Copilot CLI and Python 3.10+.
   Authenticate both CLIs and choose an approved model available to your account.
   Start in a clean checkout of the target repository's default branch, with
   permission to manage Actions, secrets and variables. NFV releases are publicly
   downloadable; no separate release credential is needed.

2. **Choose billing.** PAT billing is the default. Set
   `COPILOT_GITHUB_TOKEN` to a fine-grained PAT with **Account permissions >
   Copilot Requests: Read**, from a licensed account with model access.
   Organization billing is explicit opt-in; see [requirements](FAQ.md#which-billing-mode-should-i-use).
   For PAT billing, enter the secret interactively:

   ```text
   gh secret set COPILOT_GITHUB_TOKEN --repo OWNER/repo
   ```

   Skip the inference PAT in organization mode. Do not paste token values into chat.

3. **Install.** Start `copilot` in the target checkout and paste:

   ```text
   Install NFV following:
   https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/ONBOARDING.md

   Target repository: OWNER/repo
   Model: MODEL
   Billing: pat
   NFV release: latest stable
   ```

   Replace the repository and model; change billing only if approved. You may
   name an explicit published stable release tag instead of `latest stable`.
   The agent follows [release selection](#select-a-release), uses the selected
   release's `workflow/install.py --ref <SHA>` and its required gh-aw version,
   sets `NFV_MODEL` and opens a **draft installation PR**.
   Keep it draft until both source/compiled inference gates and an unset
   `NFV_RELEASE_SHA` are verified. Leave that variable unset through marking
   ready and merging; never run or require NFV on its own installation PR.
   See [installation checks](FAQ.md#what-must-the-installation-preserve-and-check).

4. **Merge and activate.** After approval and merge, use the installer's printed
   `--enable-after-merge` command. It disables the registered reviewer, validates
   the merged configuration and matching package pins, sets and reads back
   `NFV_RELEASE_SHA`, then enables and verifies `active`. Leave it gated off or
   disabled if any check fails. Keep unrelated CI enabled and NFV advisory,
   never a required merge check.

5. **Approve a pilot separately.** A code-change PR based on the installed branch
   can incur inference charges when opened non-draft or marked ready. Do not
   create or start one without approval. Check its generated comment and completed
   `nfv-evidence`, not just a green job; see [pilot checks](FAQ.md#how-do-i-check-a-pilot).

## Upgrade an existing installation

Ask the agent to upgrade, not rerun first installation. Disable new reviews and
wait for active/queued runs to finish. Preserve the approved model, billing mode,
secrets, budgets and local customizations.

Follow [release selection](#select-a-release), then replace the package with
`gh aw add microsoft/neuro-formal-verification/workflow@<SHA> --force`, reapply
the approved billing permissions, compile both workflows and open an upgrade PR.
Use the selected release's required gh-aw version and the same
[installation checks](FAQ.md#what-must-the-installation-preserve-and-check).

After the approved merge, verify both installed package pins on the client's
default branch agree. Update and read back `NFV_RELEASE_SHA` **before** enabling
NFV; the [handoff requirements](FAQ.md#what-must-an-upgrade-handoff-include)
specify the exact commands the agent must provide. Leave reviews disabled if
pins or read-back cannot be verified. Any pilot needs separate approval.

## Select a release

For installation and upgrades, default to GitHub's latest published stable NFV
release (`gh api repos/microsoft/neuro-formal-verification/releases/latest`).
For an explicit tag, use `releases/tags/<TAG>` instead. Require `draft: false`,
`prerelease: false` and a nonempty `tag_name` matching any requested tag.
URL-encode tags in API paths.

Resolve that exact tag once through `git/ref/tags/<TAG>` in the same repository;
follow annotated tag objects through `git/tags/<OBJECT_SHA>` until the object is
a commit with a full 40-character hexadecimal SHA. Do not use the release's
`target_commitish`, which may name a moving branch. Missing releases/tags,
invalid objects or failed resolution block setup; never fall back to `dev`,
the default branch, `HEAD` or a moving `latest` ref.

Show the selected **version tag and resolved SHA** before installation or upgrade.
Read the installer and release-specific guidance from that SHA, and pass the
literal SHA explicitly to the installer (`--ref <SHA>`) or package command.
Use its `workflow/install.py` `COMPILER` pin, not the current development pin:
NFV v0.1.1 uses gh-aw v0.88.7; the development workflows use v1.2.2.
Do not silently replace a different installed compiler.
This also works with older released installers whose omitted `--ref` defaults
to a branch tip. Keep this release-selection policy when using older copied
skills or instructions. Pin both workflow packages and, after merged-pin
verification, `NFV_RELEASE_SHA` to that same SHA; do not resolve again mid-setup.
