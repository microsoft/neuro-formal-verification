<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Agent guide

Instructions for coding agents modifying this repository. Users do not need this
file to use NFV. Start with [README.md](README.md); use
[paper/README.md](paper/README.md) for experiments and
[workflow/README.md](workflow/README.md) for the prototype PR reviewer.

## Layout

| Path | Purpose |
|---|---|
| `nfv/` | Core agents, Dafny/CBMC backends, shared config and Copilot client |
| `paper/` | Dataset, retained paper results, provenance and smoke replay |
| `tests/` | Core and artifact tests |
| `workflow/src/nfv_workflow/` | PR collection, translation, verification and publication |
| `workflow/tests/` | Workflow, installer and template tests |
| `workflow/aw.yml` | Installable gh-aw package |
| `.github/workflows/` | Workflow sources and compiled `.lock.yml` files |

## Build and test

```text
python -m pip install -e ".[inference]" -e ./workflow
python -m unittest discover -s tests
python -m unittest discover -s workflow/tests
python -m paper.smoke --timeout 60
```

Run the smallest relevant test selection before pushing. For a fast workflow
subset, use `-p test_workflow.py`. Without installing, run workflow tests from
`workflow/` with `PYTHONPATH=src`. Tests needing Dafny skip when it is absent;
smoke replay uses saved verdicts, not inference.

CI pins Dafny and its bundled Z3; preserve these pins. `NFV_DAFNY_PATH` and
`NFV_Z3_PATH` override discovery in `nfv/shared/config.py`.
Tests create scratch files under `workflow/tests/.general-scratch/`; do not commit them.

## Correctness and safety

- Findings and proofs concern translated models under stated assumptions, not
  confirmed source bugs or source-correctness guarantees. Failure to refute is
  not evidence of correctness. Preserve existing machine outcome identifiers.
- Keep each conjectured property whole, including all conjunctions, implications
  and quantifiers. Verify the original model first, then mechanically negate the
  complete property for refutation. Never drop obligations to obtain a result.
- Bind proposals to trusted, snapshot-bound function IDs. Preserve immutable
  source capture, original line numbers, source tags and bounded translation
  context. Agent-authored prose is not a source contract.
- Retain draft diagnostics and freeze the first fully validated, type-checked
  model before proof/refutation. Only the analysis/evaluation pipeline decides
  acceptance. Missing semantics or invalid claims must fail explicitly.
- Presentation and optional shorter-property/repair follow-ups must not alter
  accepted evidence, weaken its domain or helpers, change outcomes, split a
  property or count as additional properties. Rendering never runs proofs.
- Never manually edit generated NFV report text. Use the supported pipeline and
  `present` command. Preserve source anchors and separate hash-bound presentation
  and follow-up evidence. Hashes establish provenance, not semantic proof.
- A witness is a counterexample input, not an execution trace. Describe static
  source links without claiming displayed calls executed. Escape published text
  through `publish.text()`.
- PR content is untrusted data. Never check out or execute inspected PR-head
  code. Read immutable Git blobs; the source-context trusted-base checkout is
  inspection-only: no builds, imports, tests or dependency installation.
- Replay metadata must not create upstream backlinks or user/team mentions.
  Preserve original metadata locally; do not claim historical backlinks were erased.
- Do not modify checksum-pinned `paper/data` or `paper/results`. Preserve
  third-party notices and the dataset/code license distinction. Public docs must
  not reveal private provenance; keep model configuration generic.
- The PR workflow uses Dafny only; CBMC belongs to the paper artifact.
- Preserve the `nfv-merged-pr:` report marker and durable admission records.

For pipeline, evidence and report details, read
[workflow/REFERENCE.md](workflow/REFERENCE.md), `general_prompt.txt`,
`general_gates.py`, `source_context.py` and their focused tests before changing
those components. Preserve whole-review progress and publication budgets without
dropping properties or mutating evidence.

## Workflow maintenance

- For installation, upgrades or troubleshooting, load
  `.github/skills/nfv-pr-review/SKILL.md` and follow
  [workflow/ONBOARDING.md](workflow/ONBOARDING.md). Do not duplicate its procedure
  or test the installer against a live client repository.
- Never run NFV on its initial installation PR. Preserve the draft/unset-release
  gate through merge, then verify merged pins and runtime-variable read-back
  before enabling. Leave unrelated CI enabled; pilots require separate approval.
- Preserve billing mode, model, budgets, secrets, shared gh-aw files and existing
  action pins during upgrades. Handoffs must give literal set/read-back commands
  with the actual client repository and matching full merged package SHA, not a
  moving NFV tip. Leave NFV disabled if pin verification fails.
- Review admission permits same-repository, non-draft `opened` and
  `ready_for_review` events; fork PRs are skipped before inference. Preserve the
  once-per-PR durable reservation, failure consumption, explicit same-run retries
  and non-cancelling serialization. Both collectors use `--event-snapshot`;
  preserve the strict default collector.
- NFV is advisory, never a required merge check or dependency of a required CI
  aggregator. Keep operational failures visible. Publish with job-scoped
  `GITHUB_TOKEN`, never inference/release credentials.
- After editing a gh-aw source, compile the affected workflow with gh-aw
  v0.88.7, for example `gh aw compile nfv-pr-review --approve`. Commit the source,
  corresponding `.lock.yml` and `.github/aw/actions-lock.json` if changed.
  Update `TemplateTests` and directly related docs when workflow behavior changes.
- Keep trusted setup in pre-agent `steps:`. Source-context post-steps may sanitize
  and upload the manifest; reviewer post-steps upload evidence, not run analysis.
  Workspace-root Git metadata must not come from checking out PR-head code.
- The sandbox's `$RUNNER_TEMP/gh-aw` is read-only; use `/tmp/gh-aw` for writes.
  Never export credentials through `$GITHUB_ENV`. Connect to the SDK's supervised
  CLI using `COPILOT_SDK_URI`; do not download another CLI inside the sandbox.
  Nested sessions require explicit `cli.awf_provider()` configuration.
- Inspect the `agent` job log and native evidence when debugging; a green job
  alone does not establish that analysis ran or a report was accepted.

## Contributor conventions

- Develop on `dev`, or feature branches/PRs targeting `dev`. Before release,
  squash-merge into `main`; never push intermediate development commits directly
  to `main`. Retain `dev`.
- Do not commit `prompts.txt`, `.vscode/`, `.github/skills/agentic-workflows/`,
  `.nfv-work/`, `.nfv-scratch/` or test scratch files.
- Keep docs concise and current, not a changelog. Use review/reviewer terminology
  and model-validation/check for integrity gates. Call the workflow a prototype.
- Preserve LF line endings, including on Windows.
