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

The installer defaults to the latest published stable release; `--release TAG`
selects an explicit stable release. `--ref` remains an explicit trusted-revision
override, not the default. Client setup follows the single
[release-selection policy](ONBOARDING.md#select-a-release), passing the resolved
SHA explicitly for compatibility with older released installers.
The installer reads the compiler pin as data from the selected immutable
release's installer; it never executes that captured source or falls back to
the development pin if it is unavailable or invalid.

Use **gh-aw v1.2.2** for this checkout's workflows. Client installation uses
the compiler pinned in the selected release, not this development version.
After editing either workflow, compile it and retain the
source, compiled lock and changed package/action records under `.github/aw/`:

```text
gh aw compile nfv-source-context nfv-pr-review --approve
```

For compiler migrations, review upstream breaking changes first, then use
`gh aw upgrade --no-agent --no-mcp --no-skill --no-actions --approve` to preserve
shared agent assets and existing action pins. Recompile all three agentic
workflows with `gh aw compile --strict --approve`, including the benchmark
worker. Keep the installer, package minimum version and setup guidance aligned.
Retain unrelated existing action-lock entries when reviewing generated changes.
Compare generated permissions and authentication, not only compilation results.
`admission` is compiler-owned; NFV's durable-attempt job is `nfv_admission`.
Reusable workers disable only detection tracking issues to avoid an issue-write
requirement in callers; detection and its handoff gates remain enabled.
The benchmark caller additionally grants `pull-requests: read` for generated
safe-output branch checks; both reusable callers need only `actions: read`.
Explicit safe-output timeouts retain 45 minutes
instead of the compiler's implicit 15-minute slim-runner default.
The compiler's daily-credit guardrail now accounts in AIC rather than effective
token estimates; configured credit thresholds and per-run budgets are unchanged.

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

## Local skill and asynchronous runs

The [local NFV skill](../.github/skills/nfv-local/SKILL.md) wraps the existing
review and benchmark engine without requiring GitHub Actions. Install from a
trusted NFV release containing the local CLI, not from the inspected repository:

```powershell
python -m pip install ".[inference]" "./workflow[local]"
nfv-workflow local --help
```

Native execution needs Dafny 4.11.0, bundled Z3 4.12.1 and an authenticated
Copilot account with access to the explicitly selected model. Containers are
optional. The Linux amd64 dependency image includes Python, the pinned SDK,
Dafny/Z3 and the trusted NFV packages; CBMC is not needed by this reviewer:

```powershell
docker build --platform linux/amd64 -t nfv-local:latest .
docker run --rm nfv-local:latest local --help
docker run --rm --entrypoint python nfv-local:latest -m nfv.shared.config
```

Build only from the trusted NFV checkout. The build context excludes Git
metadata, local credentials, datasets and retained results. The base image and
OS packages are not a fully reproducible dependency lock; the Dafny archive is
checksum-verified. Runtime image tags are resolved to image IDs before launch.
For container inference, securely set `COPILOT_GITHUB_TOKEN` in the host
environment and, when account/API lookup needs it, `GH_TOKEN`; pass environment
variable names, never token values in commands. Tokens are not saved in run
metadata. No home directory or inspected checkout is mounted into the worker.
Git and the trusted Python CLI still run on the host to capture the inputs.

The following commands are **billable**. `--authorize-inference` records the
caller's explicit authorization; it is not a billing cap. Native compute avoids
Actions usage but does not provide free Copilot inference or organization billing.
From the repository being inspected:

```powershell
nfv-workflow local start --base dev --head HEAD `
  --intent-file .\intent.md --model APPROVED_MODEL --authorize-inference
```

Use `--runtime docker --image nfv-local:latest` for a detached container instead
of a detached native process. Only committed base/head trees are captured,
not staged, unstaged or untracked changes. A PR can instead use `--event
.\event.json`, optionally with extra explicit intent. Collection is
inspection-only; no subject dependencies, hooks, diff drivers, imports or tests
are executed. `--context-manifest` can name bounded additional paths/symbols
recaptured from the same immutable snapshot.

`start` returns JSON with a run ID, directory and log path. The default output
root is `.nfv-work\runs` under the invoking directory. Use the same directory
on later calls, or set `--runs-dir ABSOLUTE_PATH` before each subcommand:

```powershell
nfv-workflow local status RUN_ID
nfv-workflow local result RUN_ID
nfv-workflow local cancel RUN_ID
```

Workers continue after the conversation closes while the machine/runtime stays
running. `status` reports phases and failure diagnostics; `worker.log` includes
per-property progress. `result` requires a completed error-free report and
returns `evidence\comment.md`, generated by the PR's whole-property renderer
with local source/evidence references. It is not manually edited or published.
Input bundles are hash-bound; container input/config mounts are read-only.
Native evidence checkpoints remain incremental and writable.

Cancellation kills only the recorded worker process tree or its identified
container and preserves interrupted evidence. Native PID reuse is checked
against process creation time. An unexpected exit is failed, not completed.
The default wall-clock limit is six hours; `--timeout-seconds` sets an explicitly
authorized run budget. Repair limits are at most six syntax and ten proof
rounds; verifier/property timeouts are configurable. There is no automatic
resume or notification integration. Retained containers and run directories
are not deleted automatically; stop/retrieve first and remove only the
specific finished run/container you intend to clean up.

### Local nfv-micro-bench

Use the frozen dataset from the trusted NFV checkout and its exact release SHA:

```powershell
nfv-workflow local start --benchmark `
  --dataset TRUSTED_NFV_CHECKOUT\paper\data\nl2post_safe.json `
  --release-sha TRUSTED_NFV_SHA --entries 5 `
  --model APPROVED_MODEL --authorize-inference
```

Five random entries are the default pilot; `--entries 206` requests a full sweep
and needs explicit approval and an appropriate wall-clock budget. Local shards
run sequentially, using the nightly benchmark's label-blinded supplied
properties, native acceptance revalidation and scoring. Labels are excluded
from model requests. This is the PR-review benchmark, not the paper runner;
context collection, minimization and optional fix follow-ups remain disabled.

The final output is `summary\report.md`, `summary\summary.json` and
`benchmark-evidence\` with the native models/checkpoints. Errors retain a partial
summary when native evidence can be validated. Interrupted/cancelled runs
cannot be returned as completed results. Proxy AI-credit accounting is not
available locally; recorded model usage is not an invoice or a monetary cap.
No GitHub publication or nightly dashboard update occurs.

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
`GITHUB_TOKEN` publishes by adding Discussions and comments, never editing them.
No separate publication credential is needed. Missing credentials/category or
oversized budgets fail preflight before model calls.

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

Usage accounting accepts named `*-usage` artifact directories or the flattened
contents produced when `download-artifact` matches exactly one artifact.
Do not mix these layouts in one accounting directory.

Workers checkpoint after each entry. Evidence uploads get two bounded attempts;
the trusted publisher can recover a matching native checkpoint from the main
agent artifact. Recovered shards are identified and never double-counted.
Missing or mismatched evidence fails visibly; upload retries do not rerun inference.
Prepared inputs/native evidence last **seven days**, statistics **30 days**.
Download needed artifacts before expiry. Hashes bind artifacts, not semantic
correspondence or authorship.

### Dashboard and historical archives

The **Nightly nfv-micro-bench evaluation dashboard** Discussion receives generated
dashboard snapshots as new comments. Read its **newest snapshot comment**, not
the original post, for the latest completed full run and recent runs, with metrics,
AIC, model, UTC date, exact NFV commit and evidence links.
Pin the Discussion manually through GitHub's Discussion menu.
Pilots, partial/failed scored sweeps and historical backfills cannot displace
newer completed full runs. Failures before preparation remain in Actions.

Monthly **Nightly nfv-micro-bench evaluation archive - Month Year** Discussions
retain immutable statistics comments per Actions run/attempt independently of
artifact expiry.
Archives are written before the dashboard and indexed across years; existing
per-run reports remain unchanged legacy history. Months/order use the Actions
run creation time in **UTC**, not publication/retry time.

Existing titles, original posts and earlier comments are never edited, including
custom or pinned titles. Keep hidden markers: they identify the trusted bot's
dashboard/archive records. Exact publication replays reuse existing comments.
Different attempts or changed statistics append new comments, identified by
run/attempt and a statistics digest; rescoring clock changes alone reuse the
original statistics. Dashboard snapshots show one selected result per run:
the newest successful attempt, or the newest attempt if none succeeded.
An older or incomplete retry cannot downgrade success. Within an attempt, the
newest changed statistics are selected, and replaying an earlier snapshot cannot
replace them. Months remain bound to the original run across retries.
Duplicate snapshot identities or incomplete pagination fail.
Non-cancelling coordinator concurrency serializes publication. A failed
publication may already have written the archive: **retry only publication**
while evidence is available, not paid inference. Retrying a failed worker
reruns that shard; there is no automatic best-of search.

### README statistics card (GitHub Pages)

The root README links a deterministic SVG to the dashboard. The separate
`nfv-benchmark-pages.yml` workflow reads the same validated, Actions-owned monthly
archives and uses the dashboard's latest completed full 206-entry run selection.
Pilots, partial failures, old attempts and backfills cannot downgrade that result.
Missing or invalid archived statistics fail the build without deploying.
It makes no inference calls, needs no PAT, edits no Discussions and pushes no
repository changes. Archive statistics survive evidence-artifact expiry.

For `microsoft/neuro-formal-verification`, a repository administrator must enable
**Settings > Pages > Build and deployment > Source: GitHub Actions**. The existing
`github-pages` environment permits the `dev` branch; preserve that restriction
and any required approvals. Organization Actions/Pages policy must permit Pages,
the pinned official upload/deploy actions, and deployment-job `pages: write` /
`id-token: write`. A maintainer account without admin access cannot enable the
Pages resource; resolve policy blocks with an administrator, not another token
or a broader environment.

Before enabling, confirm no other workflow/site owns Pages: a deployment replaces
the entire site, not just one file. This workflow owns the card-only Pages site;
integrate other content into its artifact before sharing that site. After merging
to `dev` and configuring Pages, activate and bootstrap without inference:

```text
gh variable set NFV_BENCH_PAGES_ENABLED --body true --repo microsoft/neuro-formal-verification
gh workflow run nfv-benchmark-pages.yml --ref dev --repo microsoft/neuro-formal-verification
gh run list --workflow nfv-benchmark-pages.yml --repo microsoft/neuro-formal-verification --limit 5
gh api repos/microsoft/neuro-formal-verification/pages --jq .html_url
curl --fail https://microsoft.github.io/neuro-formal-verification/benchmark.svg
```

The URL is **proposed until the first deployment succeeds**. Successful benchmark
workflow completions on `dev` refresh it; relevant merged generator/workflow
changes also refresh it. Manual dispatch rebuilds from persistent archives and
does not restart a benchmark. GitHub image caching can delay README refreshes.
To stop future deployments, set `NFV_BENCH_PAGES_ENABLED=false`; the last deployed
card remains available until an administrator disables the site.
