<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer maintainer guide

For developers and maintainers working on NFV or running its CLI locally.
Installing NFV in a repository? Use [onboarding](ONBOARDING.md) instead.
Repository variables and secrets are in [configuration](REFERENCE.md).

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

## Nightly nfv-micro-bench

For NFV maintainers operating the benchmark in this repository or a maintained
NFV fork, **not client repositories**. It checks all 206 frozen entries using
the PR review engine with supplied specifications, not PR-intent extraction or
the paper pipeline. Benchmark source is text, never executed; the dataset and
retained paper results are unchanged.

### Configure and dispatch

Merge the benchmark workflows to the host's actual default branch for cron.
Scheduled runs resolve trusted `dev` once per sweep; manual runs use the selected
workflow revision. Runs record that immutable SHA, dataset checksum and existing
`NFV_MODEL` and repair/time budgets; there is no separate benchmark model.

Use the personal-credit `COPILOT_GITHUB_TOKEN` PAT. Source checkout and repository
API operations use the job-scoped `GITHUB_TOKEN`; no release credential is needed.
Enable Discussions and set `NFV_DISCUSSION_CATEGORY`
to an existing category (default `General`). Only the publisher's job-scoped
`GITHUB_TOKEN` publishes. Missing credentials/category or oversized budgets
fail preflight before model calls.

Set **`NFV_BENCH_ENABLED=true`** to opt in. Both scheduled and manual sweeps
require it. The schedule is **08:17 UTC daily**; dispatch defaults to all 206
entries. A billable five-entry random pilot uses:

```text
gh workflow run nfv-micro-bench.yml --ref dev -f entries=5 --repo OWNER/REPO
```

Pilots report a denominator of five, not full-dataset accuracy. A full sweep
uses 26 shards of at most eight entries, with up to ten active six-hour workers.
It can consume substantial personal credits; there is **no monetary cap**.
Preparation requires GitHub Actions to be operational on GitHub Status, but
cannot prevent later outages. No flag or secret is enabled by adding workflow files.

### Results, billing and evidence

Accepted verification predicts **correct**, accepted witnessed refutation
predicts **incorrect**, otherwise **abstain**. Existing proof/model-validation
and input-domain checks apply. Labels and identifying provenance are withheld
from model requests. Exhausted annotation repairs abstain; provider/verifier
infrastructure failures stop the worker. This benchmark omits presentation
inference, repository-context collection and optional minimization/fix follow-ups.

The publisher revalidates native evidence before scoring label-matching decisive
predictions (accuracy) and decisive predictions (coverage) over all 206 entries.
**Precision** means accuracy among decisive results, or **N/A** if none, not
incorrect-class precision. Unevaluated entries stay in the denominator;
interrupted sweeps publish **PARTIAL** statistics and remain unsuccessful.
Model-conditional verdicts are not source-correctness claims; original dataset
labels are not independently proved facts.

Reports retain confusion counts, abstentions, failures, timestamps, model,
release SHA and evidence links. **AI credits (AIC)** sum per-response proxy
ledger credits, including failed attempts, separately for inference and threat
detection and combined. Missing accounting is unavailable, not zero; malformed
usage fails. Recorded credits are not an invoice.

To add accounting to a previously published completed run without inference or
changing predictions, acceptance or completion:

```text
python -m nfv_workflow.benchmark account --summary <saved-summary.json> --usage <downloaded-usage-artifacts> --out <accounting-directory>
```

Workers checkpoint after each entry. Evidence uploads get two bounded attempts;
the trusted publisher can recover a matching native checkpoint from the main
agent artifact. Recovered shards are identified and never double-counted.
Missing or mismatched evidence fails visibly; upload retries do not rerun inference.
Prepared inputs/native evidence last **seven days**, statistics **30 days**.
Download needed artifacts before expiry. Hashes bind artifacts, not semantic
correspondence or authorship.

### Dashboard and historical archives

The **Nightly nfv-micro-bench evaluation dashboard** Discussion shows the latest
completed full run and recent runs, with metrics, AIC, model, UTC date, exact
NFV commit and evidence links. Pin it manually through GitHub's Discussion menu.
Pilots, partial/failed scored sweeps and historical backfills cannot displace
newer completed full runs. Failures before preparation remain in Actions.

Monthly **Nightly nfv-micro-bench evaluation archive - Month Year** Discussions
retain full statistics comments per Actions run independently of artifact expiry.
Archives are written before the dashboard and indexed across years; existing
per-run reports remain unchanged legacy history. Months/order use the Actions
run creation time in **UTC**, not publication/retry time.

Custom titles, including pinned titles, are preserved. Keep hidden body markers:
they identify the trusted bot's dashboard/archive records. Publication retries
update the same run comment across month boundaries; older/incomplete retries
cannot downgrade success. Duplicate markers or incomplete pagination fail.
Non-cancelling coordinator concurrency serializes publication. A failed
publication may already have written the archive: **retry only publication**
while evidence is available, not paid inference. Retrying a failed worker
reruns that shard; there is no automatic best-of search.
