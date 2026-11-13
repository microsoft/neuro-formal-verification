<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Nightly nfv-micro-bench statistics

This maintainer-only prototype evaluates the current PR audit classifier on all
206 frozen `nfv-micro-bench` entries. It is separate from the client installation
package: client workflows, onboarding, READMEs and deployed settings are unchanged.

For each supplied complete postcondition, an accepted verification proof predicts
**correct**; otherwise an accepted witnessed-refutation proof predicts
**incorrect**; otherwise the prediction is **abstain**. Existing translation,
repair, frozen-model, audit and input-domain checks remain mandatory. Benchmark
source is parsed as text, never executed.

Malformed generated Dafny annotations use the bounded proof-repair budget.
Exhausting that budget abstains on the entry and allows the shard to continue;
provider failures and verifier infrastructure errors still stop the worker.

The runner calls the same `nfv-workflow audit` engine as PR review, without report
presentation inference. Repository-context collection, optional-assumption
minimization, shorter-property generation and model-checked fix generation are
disabled. The benchmark supplies specifications instead of evaluating PR intent
extraction; it does not reproduce the paper pipeline.

## Host configuration

The workflows belong in this NFV repository or its maintained NFV fork, **not in
client repositories**. They must be merged to the host's actual default branch
for cron to run. Scheduled runs resolve trusted `dev` once per sweep. Manual runs
use the selected workflow revision, allowing reviewed maintainer branches to be
tested before merge. Each run records that immutable SHA, the configured
`NFV_MODEL`, repair/time budgets and dataset checksum. There is no separate
benchmark model or billing fallback.

Use the existing `COPILOT_GITHUB_TOKEN` personal-credit PAT and
`NFV_RELEASE_TOKEN` with read access to the private NFV source. Enable Discussions
and configure an existing `NFV_DISCUSSION_CATEGORY` (default `General`).
Publication uses only the publisher job's `GITHUB_TOKEN`; neither the inference
PAT nor the release token can publish. Missing credentials, an unavailable
Discussion category or oversized time limits fail preflight before model calls.

Set **`NFV_BENCH_ENABLED=true`** to opt in after review and configuration.
Without that flag, scheduled and manual coordinator runs do not start a sweep.
The schedule is **08:17 UTC daily**. Manual dispatch defaults to the full dataset,
or accepts `entries=5` for a single-shard, five-entry pilot:

```
gh workflow run nfv-micro-bench.yml --ref dev -f entries=5 --repo OWNER/REPO
```

The pilot randomly samples five blinded entries from the same frozen dataset,
uses the same model and budgets, and publishes explicitly labeled pilot
statistics with a denominator of five. It is not a full-dataset accuracy
estimate. Scheduled runs always evaluate all 206 entries.
Adding these files does not enable the flag, change secrets or start inference.

The coordinator uses 26 reusable agentic-worker shards of at most eight entries,
with at most ten active workers. Each worker has a six-hour job timeout and
uses the reusable PR reviewer's audit budgets. Preparation rejects budgets that cannot
fit the worker deadline. Calls and repairs are bounded, but a full sweep may
consume substantial personal credits; there is no monetary-credit cap.

Preparation requires GitHub Actions to be reported operational by GitHub Status
before starting inference. This avoids starting during a known outage, but
cannot prevent a service failure later in the run.

## Results and publication

Preparation shuffles the frozen reference/mutant ordering, assigns opaque random
case IDs and separates labels, original IDs and class-leaking provenance from
tool-free model requests. Model task inputs contain captured source,
documentation and the complete assertion, never benchmark labels or
class-identifying metadata. The trusted scorer retains the label map.
The dataset and retained paper results are never modified.

After each entry, workers checkpoint the native audit evidence. A deterministic
publisher revalidates proof acceptance, the selected whole property, captured
inputs and run identity before calculating:

- Overall accuracy: label-matching decisive predictions divided by **206**.
- Coverage: decisive predictions divided by **206**.
- Accuracy among decisive predictions, or **N/A** when none are decisive.
- Label-by-prediction counts, abstention reasons, timestamps and recorded usage.

Dedicated evidence uploads have two attempts of at most five minutes each.
The read-only checkpoint handoff and trusted publisher can also use the native
checkpoint retained in the main agent artifact. Primary and backup copies
must agree; recovered evidence passes the same input, budget and proof checks
and is never counted twice. Reports identify recovered shards. Missing copies
remain explicit failures; upload retries never rerun model inference.
Statistics uploads also have one bounded retry. Missing usage artifacts leave
AIC unavailable rather than blocking publication; malformed usage still fails.

Reports include **AI credits (AIC)**, separating benchmark inference from the
workflow's threat detection and showing their combined total. Accounting sums
`ai_credits_this_response` from each shard's gh-aw proxy usage ledgers, including
failed attempts; it never sums cumulative counters or usage-cache snapshots.
Missing ledgers or credit fields are explicitly unavailable, not zero. These
are recorded workflow credits, not an invoice.

For a completed run already published by an older revision, the maintainer-only
`python -m nfv_workflow.benchmark account --summary <saved-summary.json>
--usage <downloaded-usage-artifacts> --out <accounting-directory>` command
regenerates its report with AIC, without inference or changing its saved
predictions, acceptance or completion status. It also retains the original
summary's hash for provenance; this is not semantic proof or authentication.

Unevaluated entries remain visible in the denominator, separately identified as
not evaluated. Interrupted sweeps publish explicitly **PARTIAL** statistics and
remain unsuccessful in Actions. Malformed or mismatched evidence fails scoring
rather than producing success-shaped results.

The stable **Nightly nfv-micro-bench evaluation dashboard** Discussion shows the latest completed
full 206-entry run and a recent-runs table. Each row includes accuracy,
**Precision** (accuracy among decisive results, or **N/A**), coverage, AIC, model,
UTC date, evidence links and a short **NFV SHA** linked to the exact full commit.
Precision is not incorrect-class precision. A maintainer should pin the dashboard
using GitHub's Discussion menu after its first publication; GitHub's public API
does not support pinning Discussions.

One **Nightly nfv-micro-bench evaluation archive - Month Year** Discussion retains a full statistics
comment per Actions run, including the confusion matrix, verdict/abstention
counts and reasons, operational failures, metrics, usage, full release SHA,
timestamps and run/artifact links. The archive is written **before** the dashboard
so interrupted publication does not lose statistics. These comments persist
independently of expiring Actions artifacts. The dashboard indexes every monthly
archive across year boundaries and links existing per-run reports as unchanged
legacy history.

Archive months and dashboard ordering use the Actions run creation timestamp
normalized to **UTC**, not publication time or a retry's preparation date.
Pilots, partial sweeps and failed sweeps with scored statistics are archived but
never replace the latest completed full run. Failures before preparation remain
visible in Actions and cannot publish scored statistics.
Historical/backfill runs cannot displace newer completed runs.
Discussion titles may be customized, including for a pinned dashboard:
publication identifies Discussions by hidden body markers and updates existing
bodies without changing their titles. Keep those markers intact.
GraphQL failures report the API reason; an earlier archive write may have succeeded.
Publication retries update the same run comment, including across month
boundaries; older attempts and incomplete retries cannot downgrade an archived
success. Markers must belong to the trusted Actions bot; duplicate markers or
incomplete pagination fail visibly rather than creating duplicate reports.
Publication relies on the coordinator's non-cancelling concurrency group to
serialize dashboard/archive writes.

Retry only the failed publication job to avoid restarting paid inference.
A failed worker retry reruns that shard; there is no automatic best-of search
or deterministic replay requirement.

Prepared inputs and native evidence are retained for seven days, statistics for
30 days. Retry publication while the required artifacts remain available.
Hashes bind saved inputs and artifacts; they do not prove source/model
correspondence or authenticate model semantics.

Results concern translated models under stated assumptions, not confirmed source
bugs or proofs of source correctness. Ground-truth labels are the original
benchmark labels, not independently proved semantic facts; disagreements need
triage. Nightly outcomes may vary.
