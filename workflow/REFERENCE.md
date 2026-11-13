<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer configuration and commands

Start with [onboarding](ONBOARDING.md). See the [FAQ](FAQ.md) for operational
checks and the [reviewer guide](README.md) for results and limitations.

## Configure GitHub Agentic Workflows

### Repository variables

| Variable | Default / purpose |
|---|---|
| `NFV_RELEASE_SHA` | Full trusted 40-character NFV commit matching both installed package pins; unset until first-install post-merge activation |
| `NFV_MODEL` | Approved Copilot model ID |
| `NFV_SYNTAX_ROUNDS` | 6 repairs, maximum 20 |
| `NFV_PROOF_ROUNDS` | 10 repairs, maximum 30 |
| `NFV_VERIFIER_TIMEOUT` | 60 seconds per Dafny call |
| `NFV_PROPERTY_SECONDS` | 1800 seconds per property |
| `NFV_SIMPLIFY_REFUTATIONS` | `true`; shorter checked-property follow-up |
| `NFV_CHECK_FIXES` | `true`; model-checked candidate-fix follow-up |

Unset budgets retain defaults. Boolean flags accept `true` or `false`;
unset or empty enables them. Properties run sequentially within a 360-minute
job timeout. Model and budget changes require approval.

```text
gh variable set NFV_MODEL --repo OWNER/repo --body MODEL
gh variable get NFV_MODEL --repo OWNER/repo
```

Set the runtime release only through [activation or upgrade](ONBOARDING.md).
`gh aw update` and `gh aw upgrade` do not update `NFV_RELEASE_SHA`.

### Secrets and billing

| Secret | When needed |
|---|---|
| `COPILOT_GITHUB_TOKEN` | Default PAT billing: licensed account, model access and **Copilot Requests: Read**; not an OAuth (`gho_`) token |
| `NFV_RELEASE_TOKEN` | Private NFV releases: **Contents: Read-only** and any required organization approval |
| `GITHUB_TOKEN` | Supplied per job by Actions; do not create a repository secret |

Organization billing is opt-in and has no PAT fallback. For eligibility,
permission scopes and mode changes, see [billing](FAQ.md#which-billing-mode-should-i-use).
For restrictive CI policies, see [repository permission policies](FAQ.md#repository-permission-policies).

## Installer and workflow maintenance

The Linux-first [installer](install.py) needs Python 3.10+, Git and authenticated
`gh`. Its help lists bootstrap verification and post-merge activation options:

```text
python install.py --help
```

Use **gh-aw v0.88.7**. After editing either workflow, compile it and retain the
source, compiled lock and changed package/action records under `.github/aw/`:

```text
gh aw compile nfv-source-context nfv-pr-review --approve
```

Maintainer invariants and sandbox guidance are in [AGENTS.md](../AGENTS.md);
agent setup guidance is in the [NFV skill](../.github/skills/nfv-pr-review/SKILL.md).

## Local commands and model configuration

Use a trusted NFV checkout at the intended immutable release, Python 3.11+,
an authenticated Copilot CLI, **Dafny 4.11.0 / Z3 4.12.1**, and the release's
`github-copilot-sdk==1.0.4`. From that checkout:

```powershell
python -m pip install ".[inference]" .\workflow
nfv-workflow --help
```

Collection reads PR source as immutable data; never check out or execute PR code.
The default collector rejects superseded snapshots; `--event-snapshot` explicitly
keeps the event's snapshot, as the installed workflows do.

```powershell
nfv-workflow collect --event event.json --out .nfv-work\bundle.json
```

The following extraction and analysis commands make **billable model calls**:

```powershell
$env:NFV_MODEL = "APPROVED_MODEL"
nfv-workflow extract --bundle .nfv-work\bundle.json --model $env:NFV_MODEL --out .nfv-work\properties.json
nfv-workflow run --bundle .nfv-work\bundle.json --properties .nfv-work\properties.json --model $env:NFV_MODEL --out .nfv-work\evidence
```

`run` and general `evaluate` accept `--syntax-rounds`, `--proof-rounds`,
`--verifier-timeout`, `--[no-]simplify-refutations` and `--[no-]check-fixes`.
`evaluate` collects fresh source and checks an AW submission; `check` is the
offline, restricted scalar path. Use each command's `--help` for arguments.

To refresh an accepted report's source presentation without rerunning proofs
or publishing, use this **billable** command:

```powershell
nfv-workflow present --report .\nfv-evidence\report.json `
  --bundle .\nfv-evidence\bundle.json --model APPROVED_MODEL `
  --evidence-url EVIDENCE_URL --out .\nfv-presentation
```

It writes separate hash-bound `presentation.json` and generated `comment.md`;
accepted evidence is unchanged. Never manually edit generated reports.

Publication of trusted local evidence is preview-only unless `--apply` is given:

```powershell
nfv-workflow publish --bundle .nfv-work\bundle.json `
  --report .nfv-work\evidence\report.json `
  --evidence-url EVIDENCE_URL --category General
```

Use a real uploaded artifact URL or immutable commit path. `publish` creates a
Discussion when applied; it is separate from the installed PR-comment reviewer.
`--repository OWNER/repo` chooses its destination. Saved `followups.json` and
`presentation.json` are read beside the report, or supplied with `--followups`
and `--presentation`; rendering does not rerun proofs.

For maintainer replay and benchmark commands, see [BENCHMARK.md](BENCHMARK.md).
