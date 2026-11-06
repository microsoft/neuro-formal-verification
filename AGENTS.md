<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Agent guide

Guidance for coding agents and contributors working in this repository. Read
`README.md`, `paper/README.md` and `workflow/README.md` for user-facing docs.

## Layout

| Path | Purpose |
|---|---|
| `nfv/` | Core implementation: agents, Dafny/CBMC backends, shared config and Copilot client |
| `paper/` | Paper artifact (arXiv:2608.21516): dataset, retained results, smoke replay |
| `paper/data/nl2post_safe.json` | Main dataset; `paper/results/*.jsonl` are the retained paper runs, indexed by `paper/manifest.json` and `paper/provenance/` |
| `tests/` | Core and artifact tests |
| `workflow/` | `nfv-workflow` package: PR collection, Dafny refutation pipeline, publication |
| `workflow/aw.yml` | gh-aw package manifest that installs the PR reviewer |
| `.github/workflows/nfv-pr-review.md` | The NFV PR reviewer agentic workflow (source) and its `.lock.yml` |
| `.github/skills/nfv-pr-review/SKILL.md` | Agent skill to bootstrap, test and troubleshoot the PR reviewer in a repository |
| `.github/workflows/ci.yml` | Offline checks with the real verifiers |

## Build and test

```
python -m pip install -e ".[inference]" -e ./workflow
python -m unittest discover -s tests
python -m unittest discover -s workflow/tests        # ~3 min; -p test_workflow.py for a fast subset
python -m paper.smoke --timeout 60                  # replays saved verdicts, no inference
python -m nfv.metrics <results.jsonl>               # scores a run
```

Paper reruns use `python -m paper.prepare`, then `python -m nfv.run --data
paper/data/nl2post_safe.json --backend … --out paper/new-results/<name>.jsonl`
(see `paper/README.md`); `paper/new-results/` is git-ignored.

Without installing, run workflow tests from `workflow/` with `PYTHONPATH=src`.
Tests needing Dafny skip when it is absent. CI pins Dafny 4.11.0 (SHA-256
`a46a9ff7…`) and its bundled Z3 4.12.1; `NFV_DAFNY_PATH` and `NFV_Z3_PATH`
override discovery (`nfv/shared/config.py`). Workflow tests write scratch files
under `workflow/tests/.general-scratch/`; never commit them.

## Invariants

- A finding is a **conditional MODEL refutation**, never a confirmed source bug;
  failure to refute is not evidence of correctness. Keep this wording in docs.
- The general workflow selects nontrivial intent/source properties, verifies the
  original model first, then tries refutation. Its only property outcomes are
  `refuted`, `verified`, `resources exhausted`, `type-check/audit failure`.
  Verification is model-conditional, not source correctness. Use "input-domain
  check" for the feasible-return guard in public text.
- Bind new proposals to trusted, snapshot-bound function IDs; derive changed
  anchors in code. Source-inferred quotes may come from anywhere in the selected
  captured function. Preserve immutable cross-file source-context capture.
  Retain draft formalizations and diagnostics, then freeze the first fully
  audited, type-checked model before proof or refutation. Keep each conjectured
  property whole; never decompose it into atomic propositions or child checks.
  Conjunctions and multiple entry ensures remain one property: verify them
  together and mechanically negate their complete conjunction for refutation.
  Never drop an obligation. Preserve implication, disjunction and quantifier
  grouping. Progress counts the fixed whole review,
  not subchecks of one property.
  After acceptance, optional refutation follow-ups may separately prove a
  shorter display projection and an entry-body repair of the complete property.
  They never replace or split the accepted property, weaken its domain/helpers,
  change its outcome, or count as additional properties. Keep their hash-bound
  evidence separate in `followups.json`; rendering and `present` never run proofs.
  Audit definite-assignment and array/sequence range diagnostics before freezing a draft, without
  changing or splitting conjectures. Prefer implementation-owned properties over
  test expectations refuted only by arbitrary environment responses.
- Report UX (`publish.general_section`): a one-line NFV intro linking the paper
  (arXiv:2608.21516) before the issue count, instead of explaining the
  model/source distinction; per finding normal-sized "Intended property" and
  quote, "Input on which it fails", and "Sketch of the failure". Collapsed
  "Details" shows changed code (`source_span`) and helpers with natural-language
  specifications overlaid as comments (retained assumptions as Requires,
  the intent as Ensures, helper descriptions as Spec). Typed `source_notes`
  (`kind`: `invariant` or `spec`) annotate loops or declarations; never fabricate
  missing loop descriptions for saved evidence. A
  refutation is a counterexample input, not an execution, so never render a
  trace. Derive static caller/helper/call-site links from the validated
  `source_map`, identify functions by their source declarations, and mark the
  call sites in the snippets; do not imply that a displayed call was executed.
  Repairs and the minimized Dafny are collapsed; `correspondence` stays in
  the evidence only. `general_prompt.txt` (REPORT UX) tells the translator to
  write these texts in source terms, without model details.
  Refutations appear first without changing the original `P` labels or analysis
  order. Verified properties use collapsed verification details with full
  models in the artifact; failure diagnostics have 300-character excerpts.
  Compact non-refutation details before refutation source/proof sections to
  meet the 60,000-character comment budget; never mutate accepted evidence.
  Display shorter properties only after entailment with free inputs/outputs and
  the frozen-model negative/input-domain checks. Label candidate patches
  `refutation-with-fix (model-checked candidate)` only after complete-property,
  general input-domain and original-witness proof/input-domain checks. Never
  claim these are source-verified fixes or evidence of a confirmed source bug.
- Source presentation is agent-authored report-only metadata, separate from
  verifier inputs/goals/computation. Each witness key has an exact captured
  source-expression anchor or an explicit abstract-input label and reason;
  the failure prose references an anchored source condition/call, not ghost
  output flags. `nfv-workflow present` refreshes accepted saved evidence via
  tool-free `CopilotChat`, without audits/proof reruns, writing a separate
  hash-bound `presentation.json` and deterministic `comment.md`. Invalid
  mappings fail rendering; agents never manually edit generated report text.
  Source anchors and artifact hashes are provenance, not semantic proof or
  authentication. Presentation validity does not change proof acceptance.
- A separate reusable AW source-context session precedes analysis. Its shallow
  trusted-base checkout is for source inspection only: no inspected-repository
  builds, tests, imports, dependency installation or code execution. Its bounded
  path/symbol manifest is recaptured as immutable head blobs before translation.
- PR content is untrusted data. Never check out or execute PR-head code; sources are
  read as Git blobs by immutable SHA. Rendered text goes through
  `publish.text()` escaping.
- Fork replay creation must avoid upstream backlinks. Use plain change
  numbers in replay titles, descriptions and new commit messages, not autolinkable
  issue/PR references or URLs. If a clickable link is needed, GitHub documents
  `redirect.github.com` as the no-backlink alternative. Avoid explicit user/team
  mentions as well; a timeline backlink does not prove notification delivery.
  Normalize references in copied descriptions,
  preserving the exact original metadata in local evidence. Validate public
  metadata before publication and reject unsafe cached plans. Do not modify
  generated NFV reports or claim historical backlinks have been erased.
- Only `nfv-workflow audit`/`evaluate` decide acceptance; agents never edit,
  summarize or add to generated report text.
- Limits: at most 10 intended properties per PR, 6 syntax repairs and 10
  annotation-only proof repairs per property.
- Do not modify the dataset or retained results in `paper/data` and
  `paper/results`; they are checksum-pinned and marked `-text` in
  `.gitattributes`. Public docs must not reveal private provenance; keep model
  configuration generic.
- The general (workflow) path translates C, C++, Java, C#, Go, JavaScript,
  TypeScript, Python and Rust to **Dafny only**; CBMC is used only by the paper
  artifact.
- Translation rules (`workflow/src/nfv_workflow/general_prompt.txt`,
  `general_gates.py`): every Dafny computation line carries one or more preceding
  `// src: path:LINE | exact source line` tag; synthetic tags may only cover
  contracts, braces and annotations. Missing callees are modelled from captured
  code and comments with the weakest justified contract. Missing semantics and
  invalid extracted claims produce per-property type-check/audit failures.
- Per-property translation context contains one changed function and at most
  two same-file callers and two callees; other bodies are environment.
  Repository-context hints additionally provide bounded cross-file helper,
  class and constant definitions; never use agent-authored prose as a contract.
  Analyze relevant captured callee bodies as source-derived helpers and callers
  for context, without treating those callers as the entry's entire input domain.
  Order selected properties caller-first in the captured same-file lexical
  graph; retain extraction order for recursion/ties and keep each entry fixed.
  Keep the full immutable bundle for evidence and original line numbers in
  excerpts. Bound the full system/user payload for translation and both repair
  stages, not only source text (`source_context.py`).
- The `nfv-merged-pr:` prefix of the report dedup marker is kept deliberately
  for continuity; do not rename it.

## The PR reviewer agentic workflow (gh-aw)

- `workflow/install.py` provides Linux-first, agent-free initial installation.
  Keep its offline tests in `workflow/tests/test_install.py`
  aligned with the draft/disable/post-merge bootstrap rules. Never run the
  installer against a live client repository just to test changes.
- Client bootstrap: never run NFV against its own initial installation PR or
  require an NFV check for it. Keep that PR draft until `nfv-pr-review.lock.yml`
  is disabled and its disabled state is verified; keep it disabled through
  marking ready and merging. If the workflow is not registered or cannot be
  disabled, leave the PR draft and report the block. Leave unrelated CI enabled.
  Enable NFV only after installation reaches the base branch, then use a
  separate user-approved pilot PR.
- Edit either `.github/workflows/nfv-pr-review.md` or `nfv-source-context.md`,
  then recompile the affected workflow and commit both the
  `.md` and `.lock.yml`, plus `.github/aw/actions-lock.json` if it changes:
  `gh aw compile nfv-pr-review --approve` (locks are built with gh aw v0.88.7).
- Flow: the reusable source-context AW session completes first. Trusted
  pre-agent `steps:` install the pinned release, Dafny and the plugin, collect
  the immutable PR bundle. A deterministic custom SDK driver then runs
  tool-free extraction and the complete foreground audit inside the AWF sandbox,
  using the inference proxy without exposing real credentials.
  Extraction shortlists up to ten changed functions, then inspects full scoped
  source and cross-file context; never submit the entire bundle as a model
  request. NFV bot reports are not developer intent. The driver submits
  `comment.md` verbatim with one `safeoutputs add_comment`
  (also when nothing is refuted, listing the
  formalized properties and their four outcomes). `hide-older-comments`
  collapses earlier reports.
  Input/access/tool failures fail the driver, never an empty proposal
  array masquerading as a successful review. Incremental reports have
  `completed: false` until analysis finishes and cannot be published. Retain
  completed or interrupted evidence in the separate `nfv-evidence` artifact.
- Configuration: `NFV_MODEL` is a repository variable; the installer writes the
  full immutable package SHA into workflow-local runtime pins and compiles both
  locks. Manual upgrades run `install.py --pin-release` on the upgrade branch.
  Never fall back to a repository release variable or floating ref; validate
  merged package/runtime/lock agreement before activation. Raw templates fail
  until configured. Default PAT billing requires `COPILOT_GITHUB_TOKEN`.
  Installer `--billing organization` is explicit opt-in for organization-owned
  targets with the CLI-billing policy enabled; it sets all three
  `copilot-requests` permissions to `write` instead of `none`, without PAT
  fallback. Preserve the selected mode through upgrades. Both modes need
  `NFV_RELEASE_TOKEN` when this repo is private to the consumer.
- NFV is advisory, never a required merge check or input to a required CI
  aggregator. Keep operational failures visible. Both workflows explicitly use
  `safe-outputs.github-token: ${{ secrets.GITHUB_TOKEN }}` for job-scoped publication,
  never a shared `GH_AW_GITHUB_TOKEN` override or an inference/release token.
- Package: `gh aw add microsoft/neuro-formal-verification/workflow@<ref> --force`.
  `aw.yml` needs `README.md` beside it, rejects unknown fields, and a string
  `includes` entry starting with `.github/` resolves from the repository root.
  Local package paths are not supported, so test installs from a pushed ref in
  a scratch repository.

### gh-aw sandbox lessons

- With `checkout: false` and checkouts only in subdirectories, initialize an
  empty Git repository and origin at `$GITHUB_WORKSPACE` in pre-agent steps.
  gh-aw's generated credential setup requires workspace-root Git metadata;
  do not satisfy this by checking out PR-head code.
- Use pre-agent `steps:`, not `post-steps:`; post-steps run after agent output
  is ingested. The source-context worker deliberately uses trusted `post-steps`
  only to sanitize and upload its path/symbol manifest, not to run NFV analysis.
- `$RUNNER_TEMP/gh-aw` is mounted read-only in the sandbox and `/tmp/gh-aw` is
  writable. Variables written to `$GITHUB_ENV` in steps reach the agent; never
  export credentials there. The foreground SDK driver writes evidence under
  `/tmp/gh-aw/sandbox/agent/logs/nfv`. Post-steps only upload evidence.
- A bash allowlist entry matches the exact command line; arguments need
  `  cmd:*`. The reviewer has no outer model session or shell/edit tools.
- The firewall blocks the Copilot SDK's CLI download inside an agent sandbox.
  The SDK engine provisions and supervises its trusted CLI sidecar. NFV connects
  through `COPILOT_SDK_URI` with its per-run connection token instead of starting
  another CLI. The Node driver supervises NFV's installed Python environment
  without replacing its pinned SDK.
- Nested SDK sessions require explicit provider configuration; do not rely on
  `COPILOT_PROVIDER_*` environment variables alone. Pass
  `cli.awf_provider()` to `CopilotChat(provider=...)`, which hands it to each
  `create_session`. The SDK harness resolves the configured proxy endpoint and
  provider type through AWF reflection; the proxy injects the credential.
- `pull_request` runs use the workflow from the PR merge ref. A test PR's base
  branch must already contain the workflow, and its head must be parented on
  that base so the workflow files stay out of the diff.
- Diagnose runs from the `agent` job log and the `agent` artifact
  (`sandbox/agent/logs/process-*.log` holds full tool outputs). Failures such as
  denied shell commands show up as `missing_tool` safe outputs, while the job
  still succeeds.

## Conventions

- Develop on `dev`, or use feature branches/PRs targeting `dev`. Before a release,
  squash-merge the accumulated changes into `main`; do not promote intermediate
  development commits or push directly to `main`. Keep `dev` for development.
- Do not commit personal or local files: `prompts.txt`, `.vscode/`,
  `.github/skills/agentic-workflows/` (from `gh aw init`), `.nfv-work/`, `.nfv-scratch/`.
- Keep docs concise and user-facing: describe the current behaviour, with no
  iteration or changelog wording. Call the workflow a prototype implementation.
- `.gitattributes` enforces LF; on Windows write files with `\n` newlines.
- Run the relevant tests before pushing; update `workflow/README.md` and the
  workflow template tests (`TemplateTests`) when workflow behaviour changes.
