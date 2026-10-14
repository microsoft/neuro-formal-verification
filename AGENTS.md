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
- PR content is untrusted data. Never check out or execute PR code; sources are
  read as Git blobs by immutable SHA. Rendered text goes through
  `publish.text()` escaping.
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
  `general_gates.py`): every Dafny computation line carries a preceding
  `// src: path:LINE | exact source line` tag; synthetic tags may only cover
  contracts, braces and annotations. Missing callees are modelled from captured
  code and comments with the weakest justified contract; abstain only when the
  behaviour is undocumented. Invalid extracted claims become per-claim
  abstentions instead of failing the batch.
- Per-property translation context contains one changed function and at most
  two same-file callers and two callees; other bodies are environment.
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

- Edit `.github/workflows/nfv-pr-review.md`, then recompile and commit both the
  `.md` and `.lock.yml`, plus `.github/aw/actions-lock.json` if it changes:
  `gh aw compile nfv-pr-review --approve` (locks are built with gh aw v0.88.7).
- Flow: trusted pre-agent `steps:` install the pinned release, Dafny and the
  plugin and collect the PR bundle; the agent reads the non-secret
  `nfv-input.json` index and `nfv-src/` text, writes `nfv-proposals.json`, runs
  `nfv-workflow audit --proposals nfv-proposals.json`, and posts the generated
  `comment.md` with one `add-comment` (also when nothing is refuted, listing the
  formalized intents). `hide-older-comments` collapses earlier reports.
  Input/access/tool failures stop extraction with an explicit `noop` error,
  never an empty proposal array masquerading as a successful review.
- Configuration: repository variables `NFV_RELEASE_SHA` (40-hex commit of this
  repo) and `NFV_MODEL`; secret `COPILOT_GITHUB_TOKEN`; optional
  `NFV_RELEASE_TOKEN` when this repo is private to the consumer.
- Package: `gh aw add microsoft/neuro-formal-verification/workflow@<ref> --force`.
  `aw.yml` needs `README.md` beside it, rejects unknown fields, and a string
  `includes` entry starting with `.github/` resolves from the repository root.
  Local package paths are not supported, so test installs from a pushed ref in
  a scratch repository.

### gh-aw sandbox lessons

- Use pre-agent `steps:`, not `post-steps:`; post-steps run after agent output
  is ingested.
- `$RUNNER_TEMP/gh-aw` is mounted read-only in the sandbox and `/tmp/gh-aw` is
  writable. Variables written to `$GITHUB_ENV` in steps reach the agent.
  Evidence goes to `/tmp/gh-aw/sandbox/agent/logs/…`, which the `agent`
  artifact uploads.
- A bash allowlist entry matches the exact command line; arguments need
  `cmd:*`. The reviewer uses `bash: ["*"]`.
- The firewall blocks the Copilot SDK's CLI download; set `COPILOT_CLI_PATH` to
  gh-aw's `$RUNNER_TEMP/gh-aw/bin/copilot`.
- The agent's shell does not inherit the harness's Copilot provider settings or
  token, and the SDK-launched CLI ignores `COPILOT_PROVIDER_*` variables. Pass
  `cli.awf_provider()` (an SDK `ProviderConfig` for the AWF API proxy at
  `http://api-proxy:10002`, placeholder key) to `CopilotChat(provider=...)`,
  which hands it to each `create_session`; the proxy injects the credential.
- `pull_request` runs use the workflow from the PR merge ref. A test PR's base
  branch must already contain the workflow, and its head must be parented on
  that base so the workflow files stay out of the diff.
- Diagnose runs from the `agent` job log and the `agent` artifact
  (`sandbox/agent/logs/process-*.log` holds full tool outputs). Failures such as
  denied shell commands show up as `missing_tool` safe outputs, while the job
  still succeeds.

## Conventions

- Branch from and target `dev` with feature branches/PRs; never push to `main`.
- Do not commit personal or local files: `prompts.txt`, `.vscode/`,
  `.github/skills/agentic-workflows/` (from `gh aw init`), `.nfv-work/`, `.nfv-scratch/`.
- Keep docs concise and user-facing: describe the current behaviour, with no
  iteration or changelog wording. Call the workflow a prototype implementation.
- `.gitattributes` enforces LF; on Windows write files with `\n` newlines.
- Run the relevant tests before pushing; update `workflow/README.md` and the
  workflow template tests (`TemplateTests`) when workflow behaviour changes.
