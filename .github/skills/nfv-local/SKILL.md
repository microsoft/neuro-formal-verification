---
name: nfv-local
description: Run NFV locally on a committed change with explicit intent, a PR snapshot, or the nfv-micro-bench benchmark. Start asynchronous native or container workers, inspect status, retrieve local Markdown reports and cancel runs without GitHub Actions or automatic publication.
---

<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Local NFV

Use the trusted `nfv-workflow local` CLI, not an agent-authored proof pipeline.
The local runtime uses the PR review engine: whole-property intent extraction,
model-validation, Dafny verification followed by witnessed model refutation.
Results are conditional MODEL refutations or model-conditional proofs, never
confirmed source bugs or source-correctness claims.

User-facing commands and dependency setup are in
[the local maintainer guide](../../../workflow/MAINTAINER.md#local-skill-and-asynchronous-runs).
If this skill is copied into another repository, read that guide from the same
trusted NFV revision as the installed runtime. Never install dependencies from
the repository being inspected.

## Before starting

1. Confirm the requested mode: committed base/head change plus explicit intent,
   an existing PR event snapshot, or the frozen benchmark. Committed changes
   exclude staged, unstaged and untracked files; do not claim otherwise.
2. Use a trusted NFV release containing this CLI. Follow NFV's stable-release
   selection policy, pinning the exact release SHA. A development checkout is
   only for explicitly authorized maintainer testing; never silently substitute
   a branch tip for a published release.
3. Confirm the model and billing account. Local compute avoids Actions minutes,
   not Copilot inference credits. Do not switch from personal to organization
   billing or invent a credential fallback. Never receive, print, store in
   command arguments, or commit token values. Container inference uses runtime
   environment credentials, not a mount of the user's entire home directory.
4. Obtain explicit authorization for the scope, model and billable run before
   passing `--authorize-inference`. A prior user delegation covering those
   limits is sufficient for agent use; skill invocation alone is not.
5. Confirm the native or Docker runtime and wall-clock/repair limits.
   Containers are optional dependency packaging, not permission to execute
   inspected code. Native execution requires Dafny 4.11.0, bundled Z3 4.12.1
   and the release's Copilot SDK. Failures must remain visible.

## Start and return immediately

From the inspected repository, using the separately installed trusted CLI:

```powershell
nfv-workflow local start --base dev --head HEAD `
  --intent-file .\intent.md --model APPROVED_MODEL --authorize-inference
```

Add `--runtime docker --image TRUSTED_IMAGE` for the dependency container.
`start` resolves base/head to immutable commits and captures Git blobs as data.
It never checks out, imports, builds or executes the inspected source.
Optional `--context-manifest` is a bounded JSON `files` list, each item containing
only `path` and `symbols`; the runtime binds and recaptures those hints.
Never use agent-authored context prose as a contract.

For a PR, use `--event .\event.json` instead of base/head; this is an actual
GitHub PR event JSON, collected through the read-only API. An optional
`--intent-file` supplies additional explicit intent. Do not fabricate PR
identities, check out PR-head code or bypass automatic-review admission records.
A local manual review is separate from the installed Actions workflow.

`start` returns JSON with the run ID, output directory and log path. Record
those exact values and return control to the user; do not keep an outer model
session polling or restart inference just to retrieve results.
Workers survive closing the conversation while the machine/runtime stays
running. Native jobs are separate processes; Docker jobs are detached containers.
There is no automatic notification integration.

## Inspect, retrieve and cancel

```powershell
nfv-workflow local status RUN_ID
nfv-workflow local result RUN_ID
nfv-workflow local cancel RUN_ID
```

Use the same working directory, or the same explicit `--runs-dir` before the
subcommand, on every invocation. Status is running, completed, failed or
cancelled, with the phase and diagnostic when available. Logs contain
per-property progress; saved native evidence is retained incrementally.
Cancellation targets only the run's owned worker/container, preserving evidence.
Machine loss or termination without a final checkpoint is not successful.

For completed reviews, `result` returns `evidence\comment.md`: the same
whole-property renderer as the PR comment, with local snapshot/evidence
references instead of nonexistent public links. Display or attach that file
verbatim; never manually edit generated reports or infer acceptance from the
worker's exit code alone. Incomplete/error reports cannot be returned as final.
Do not publish, open a PR, or upload evidence unless the user separately asks.

## Local benchmark

Benchmarks require separate explicit authorization: a five-entry random pilot
or the complete 206-entry nfv-micro-bench dataset. The default is five, not a
full sweep. Use the unchanged checksum-pinned dataset from the trusted NFV
checkout and supply that checkout's exact release SHA.

```powershell
nfv-workflow local start --benchmark `
  --dataset TRUSTED_NFV_CHECKOUT\paper\data\nl2post_safe.json `
  --release-sha TRUSTED_NFV_SHA --entries 5 `
  --model APPROVED_MODEL --authorize-inference
```

This reuses the nightly benchmark's label-blinded supplied properties, native
acceptance checks and scoring, not the paper runner or PR-intent extraction.
Shards run sequentially locally. Repository-context collection, minimization
and candidate-fix follow-ups remain disabled. Do not expose dataset labels or
the scoring manifest to model requests.

`result` returns `summary\report.md` and the native benchmark evidence directory.
Failed benchmarks retain a partial summary when evidence can be validated;
partial/cancelled runs never become completed reports. Missing AI-credit
accounting is unavailable, not zero. Local runs do not update the nightly
dashboard. Never start a full sweep, raise limits, or retry failed inference
without authorization; a wall-clock limit is not a monetary cap.
