# Merged-PR neuro-formal verification

This is a prototype implementation of a GitHub Agentic Workflow that runs when
a pull request is **merged**, extracts up to **ten intended properties** from PR
comments, reviews, review comments, and commit messages, then applies the retained
**NFV Dafny backend** to look for conditional model refutations.

The default path accepts source in **C, C++, Java, C#, Go, JavaScript, TypeScript,
and Python**. It translates imperative computation into Dafny rather than limiting
the workflow to a scalar-expression evaluator. Per the requested iterations:

* **Iter 1:** up to **6 syntax/type repairs** and **10 annotation-only repairs**
  of the negative check—twice the historical 3/5 defaults.
* **Iter 3:** all eight source languages use **Dafny only**. CBMC remains confined
  to the paper component.
* **Iter 5:** the workflow runs only for **merged** PRs; PRs closed without
  merging are skipped before any agent or model call.
* **Iter 4 is not implemented:** there is no educated-guess bug-likelihood
  filter before verification.

## What a finding means

**A finding is a checked conditional MODEL refutation, not a source-confirmed
bug. Source-to-model correspondence is not established.**

The model can contain loops, state updates, branches, arrays/objects, and
body-defined environment helpers. The translator must describe abstractions,
numeric semantics, missing effects, and inferred conditions. Exact source tags
and frozen computation prevent missing provenance and repair-time changes;
they do not prove that the initial translation preserves the source's semantics.
The natural-language claim's formalization is also inferred, so reports show
both the original quote and the exact checked expectation.

Each language receives specific semantic guidance: C/C++ undefined behavior and
widths, Java wrapping, C# checked/unchecked behavior, Go widths/slices/panics,
JavaScript/TypeScript IEEE-754 and runtime coercions, and Python arbitrary-precision
integers, floor division, dynamic types, and aliasing. This guides translation;
it is not a mechanically verified source-language frontend.

Unsupported constructs, unavailable context, invalid witness encodings,
inconclusive checks, or exhausted budgets cause abstention. No path falls back
to proving the original property. Passing an original-property check is never
used as a finding or as evidence of source correctness.

## What developers receive

Merging a PR triggers analysis, including PRs from forks and merges by any
actor, subject to repository/organization Actions policy and AW spending limits.
GitHub has no separate `merged` event, so the workflow listens to
`pull_request_target: closed` and its activation job runs only when
`github.event.pull_request.merged == true`. All later jobs depend on activation,
so unmerged PRs consume no agent or model budget. The collector and publisher
independently reject unmerged identities.

The source identity is the event's **immutable PR head SHA**, with the base SHA
recorded and changed files obtained from the immutable `base...head` comparison.
The merge SHA is recorded and shown, but reports explicitly analyze the saved
PR head, **not** the merge result, so changed-line anchors match the PR diff. No moving branch substitutes for a
missing commit. Reopened, unmerged, or mutated identities during collection are
rejected.

At most **one Discussion** is created, and only when accepted refutations exist.
It is labeled **`NFV-formal-review`**, so findings can be filtered in the
Discussions view. It includes:

* the exact developer quote, intended behavior, and formalized expectation;
* concrete modeled inputs and a conditional model-based explanation;
* an annotated overlay on source lines linked to the immutable commit;
* retained high-level assumptions and explicit source-correspondence limitations;
* **“This issue was found by a neuro-formal-verifier.”**
* an Actions artifact link, with Dafny, repair logs, hashes, and assumption details
  inside a collapsed section.

No accepted findings, errors-only runs, and abstentions-only runs create **no
Discussion**. A mixed run can report accepted findings alongside recorded
abstentions; a run-level collection/proposal-validation error suppresses posting.
AW's own infrastructure diagnostics are not NFV findings.

Discussions are deduplicated by repository, PR number, base SHA, and source SHA.
The template serializes execution per PR, and the publisher enumerates existing
Discussions instead of relying on eventually consistent search. Concurrent manual
publishers or separate installations using different concurrency groups are not
covered by a cross-workflow atomic uniqueness guarantee. Deleting a Discussion
permits recreation.

## Refutation pipeline and gates

1. **Collect as data.** Read regular Git blobs by immutable SHA and verify their
   Git blob hashes. Gather changed code, nearby declarations, precondition/assert
   lines, callee names, quoted includes/companions, and bounded same-language
   sibling/import context. Record omissions and a complete regular-file inventory.
   PR source is never checked out for execution, compiled, imported, or tested.
2. **Extract intention, not suspicion.** Exact comment/review/commit quotes and
   changed-line anchors are mandatory. Deduplicate and select at most ten
   intended properties. No likelihood-based triage is applied.
3. **Translate and resolve.** The tool-free NFV `CopilotChat` client produces a
   canonical Dafny model, original `ensures`, source mappings, concrete witness,
   environment descriptions, and inferred input conditions. Up to six repair
   requests may fix syntax/types before freezing computation. Each attempt uses
   the real NFV `type_check` / `dafny resolve` interface—**not verification of the
   original property**. The recorded goal and metadata cannot change during repair.
4. **Freeze and invert.** Reuse NFV's `invert_entry_spec` to negate the conjunction
   of original postconditions mechanically. Exact input equalities are installed
   mechanically. The complete translated computation, declarations, goals,
   original conditions, and witness are frozen.
5. **Check the negative model.** A trusted parameterless `NfvWitnessCheck` method
   constructs each concrete input and calls the entry. The complete negative
   model and this call must verify. The call checks that the witness satisfies
   the input conditions; contradictory requirements cannot yield acceptance.
   Dafny must establish termination; `decreases *` is prohibited.
6. **Repair annotations only.** Up to ten requests return a JSON insertion plan
   containing only single-line `assert`, `invariant`, or `decreases` annotations.
   No full-model replacement, computation, witness, declarations, or condition
   changes are permitted. Trusted code reconstructs the model from the frozen
   artifact; NFV's source-freeze and integrity checks are also reapplied.
7. **Check returning behavior.** Reuse NFV's returning-case probe construction.
   An inconclusive probe, or a successful impossible-return condition, rejects
   the result. A failure of that probe alone is **never** treated as a finding:
   the full negative model and concrete call must already have verified.
8. **Minimize and recheck.** Optional conditions are removed one at a time only
   if the negative model, concrete-call consistency, and returning-case check
   still pass. Required conditions stay fixed. Final checks run again before
   acceptance. This is greedy dependency minimization, not globally minimum
   assumptions or certification of translation fidelity.

Only optional entry conditions are minimized. Body-defined environment choices
and helper contracts stay frozen and remain explicit; they are not described as
minimized. Each property uses one proposed concrete witness, frozen before the
negative-repair stage, rather than restarting witness search to force a result.

Every generated computation line must have an exact
`// src: path:line | source excerpt` tag. All significant lines in the declared
source slice and changed anchors must be covered. Synthetic tags are allowed only
on structural syntax and verification annotations. Required files and every
environment helper's captured anchors are checked. This is **provenance coverage,
not semantic equivalence**.

Helpers must have checked bodies and explicit source/inferred environment
descriptions. No axioms, `assume`, `expect`, bodyless callable contracts,
verification-disabling attributes, includes/imports, opaque definitions, or
termination escapes are allowed. No model-authored shell command is executed.
Canonical callable signatures/contracts are complete single lines and their
opening body brace is on its own line. Certain advanced Dafny constructs, such
as subset/newtype aliases and uninterpreted constants, conservatively abstain.

The current concrete-call bridge supports named **`int`, `bool`, `seq<int>`,
and `seq<bool>` input/return values**. Heap/graph initial state may be modeled
through these value parameters; arbitrary object identity or constructor-based
witnesses are not silently guessed. This is still a bounded modeling frontend,
not a complete compiler for any of the eight source languages.

## Install from an immutable revision

Requirements: Python 3.11+, the core from the same reviewed revision,
`github-copilot-sdk==1.0.4` for the general path, authenticated Copilot CLI,
and Dafny **4.11.0** with Z3 **4.12.1**. Offline tests use fake model responses;
they do not make inference calls.

For a fresh checkout, replace the placeholder with a reviewed full SHA containing
the workflow implementation (it need not be on `main`):

```powershell
$NFV_RELEASE_SHA = "REVIEWED_40_CHARACTER_COMMIT_SHA"
if ($NFV_RELEASE_SHA -notmatch '^[0-9a-f]{40}$') { throw "Set a full reviewed SHA" }
git clone --no-checkout https://github.com/microsoft/neuro-formal-verification.git nfv-release
git -C nfv-release checkout --detach $NFV_RELEASE_SHA
Set-Location nfv-release
python -m pip install ".[inference]" .\workflow
```

A SHA pins bytes; it is **not a signature or proof of trusted authorship**.
Review provenance and verify release signatures if provided. No signed-release
guarantee is claimed. Do not install a floating branch or PR-supplied revision.
For an existing checkout, verify its HEAD and modifications against the chosen
revision first.

The workflow wheel includes its trusted prompts. Core Python dependencies are
installed separately through the `inference` extra. The direct scalar backend
path does not require the model client.

## Configure GitHub Agentic Workflows

1. Enable Discussions and choose an existing category, e.g. `General`. Create the
   `NFV-formal-review` label once, e.g.
   `gh label create NFV-formal-review --repo OWNER/REPO --description "Neuro-formal verification findings"`.
   The workflow applies, but does not create, this label. If it is missing, the
   Discussion is still created, unlabeled, and the publish step logs a warning.
2. Configure protected repository variables:
   * `NFV_RELEASE_SHA`: reviewed full commit of
     `microsoft/neuro-formal-verification`, containing these files.
   * `NFV_DISCUSSION_CATEGORY`: existing category name.
   * `NFV_MODEL`: explicitly approved model ID for translation/repair.
3. Configure an approved **`COPILOT_GITHUB_TOKEN`** secret. The outer AW agent
   uses normal AW authentication; the separate NFV translation/repair client
   needs this dedicated Copilot credential. Org-billed AW inference by itself
   does not automatically authenticate the separate SDK sessions.
4. Copy `templates\nfv-merged-pr.md` to
   `.github\workflows\nfv-merged-pr.md`, run `gh aw compile nfv-merged-pr`, and
   review/commit both Markdown and generated lock through your normal process.
   The checked-in template lock is compiled with **gh aw v0.88.7**.
5. Ensure the target token can read the chosen NFV revision. Until that repository
   is public, cross-private-repository checkout needs an explicitly scoped read
   credential; the target's default `GITHUB_TOKEN` cannot read another private
   repository. Same-repository checkout is different.

The intentional `pull_request_target: types: [closed]` trigger, gated to merged
PRs, emits AW's elevated-context warning. **Never change its trusted release checkout to the PR
head**, execute PR scripts, or enable PR builds. All actors are enabled explicitly.

The isolated safe-output job runs on **Ubuntu 22.04**, installs the trusted
release plus SDK, and pins Copilot CLI **1.0.80**. It downloads official
`dafny-4.11.0-x64-ubuntu-22.04.zip` and checks SHA-256
`a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179`.
It verifies the Dafny version, selects the unique bundled `z3-4.12.1`, checks its
version, and exports the exact **`NFV_Z3_PATH`**. The verifier worker preserves
this variable, not repository/model tokens. There is no host-solver fallback.

The agent can submit only intended-property JSON, not a verdict, repository,
category, report, or filename. The safe job recollects source independently,
runs tool-free SDK translation/repair sessions, and executes only generated
Dafny through a credential-stripped worker. Publication uses a separate step and
trusted reconstructed evidence. Source-correspondence caveats cannot be removed
by agent prose. This is **not an OS-level sandbox against verifier vulnerabilities**:
use ephemeral GitHub-hosted runners, not privileged shared runners.

## Local commands and model configuration

Authenticate Copilot CLI with an approved credential, then select a model ID
available to that credential. For GitHub Actions, configure `NFV_MODEL` and the
`COPILOT_GITHUB_TOKEN` secret as described above.

```powershell
# Read-only collection of a real merged-PR event payload.
nfv-workflow collect --event event.json --out .nfv-work\bundle.json

# Explicitly billable extraction and general refutation; replace placeholders.
$env:NFV_MODEL = "YOUR_APPROVED_MODEL"
nfv-workflow extract --bundle .nfv-work\bundle.json --model $env:NFV_MODEL --out .nfv-work\properties.json
nfv-workflow run --bundle .nfv-work\bundle.json --properties .nfv-work\properties.json --model $env:NFV_MODEL --syntax-rounds 6 --proof-rounds 10 --out .nfv-work\evidence
```

Never commit credentials or local environment files. Model access and billing
follow the configured credential's permissions and service policy.

`evaluate` is the trusted AW entrypoint: fresh collection plus general execution.
Its defaults are `--mode general --syntax-rounds 6 --proof-rounds 10`.
`--verifier-timeout` defaults to 60 seconds. Each model request defaults to
240 seconds, with a 30-minute per-property wall limit checked between operations.
The ordinary maximum is **17 logical model requests per property**
(one translation + six syntax repairs + ten annotation repairs), plus extraction.
The retained SDK may retry transport failures up to four times per request.
Model choices and budgets must be approved before enabling the workflow.

Publication is preview-only unless `--apply` is supplied:

```powershell
nfv-workflow publish --bundle .nfv-work\bundle.json --report .nfv-work\evidence\report.json --evidence-url https://github.com/OWNER/REPO/actions/runs/RUN_ID/artifacts/ARTIFACT_ID --category General
```

Use an actual uploaded artifact URL, never a fabricated example URL. Only consume
trusted local checker output, not an externally supplied report/bundle. The
publisher replays static goal/witness/frozen-model/provenance reconstruction and
checks accepted verifier evidence. In Actions it also reads staged mode from the
executing workflow's immutable `GITHUB_WORKFLOW_SHA`: gh aw v0.88.7 does not
propagate this flag to custom safe jobs. Missing staged metadata fails closed.
Staged mode suppresses mutation even with `--apply`.

## Optional stricter scalar source-bound path

The previous scalar implementation remains an **optional stricter path**, not
the default or the sole implementation:

```powershell
nfv-workflow check --bundle workflow\examples\scalar\bundle.json --proposals workflow\examples\scalar\proposals.json --out .nfv-work\scalar
```

`propose` generates that path's restricted expression schema; `check` consumes it
without inference. `evaluate --mode scalar` is available for explicitly prepared
scalar proposals. Its publication gate independently parses exact scalar return
functions and conservatively resolves preceding same-file helpers or a unique
cross-file leaf through a quoted header and same-basename implementation.
Source-derived callee definitions are distinct from inferred conditions. It
rejects unsupported methods, control-flow statements, macros, overloads,
alternate implementations, and missing context. Even this path does not certify
NL formalization, external caller preconditions, or linker behavior.

## Evidence, limits, and validation

* Tests cover every source language using fake translator responses and **real
  Dafny checks**, actual type and annotation repair, doubled repair bounds,
  altered goals/computation, fabricated provenance, inconsistent witnesses,
  unsafe/bodyless models, and returning-case failures. No inference is required.
  Run `python -m unittest discover -s workflow\tests -v`.
* [Scalar](examples/scalar/README.md) and
  [cross-file](examples/cross-file/README.md) examples contain actual positive
  synthetic source-bound checks, clearly labelled non-GitHub fixtures.
* [General imperative replay](examples/general-model/README.md) runs an actual
  failed negative check and annotation-only repair using a handwritten fake
  proposer and the real NFV/Dafny checker, with persistent evidence and no
  inference calls.
* [Z3 example](../examples/z3/README.md) replays the general pipeline on merged
  Z3 PR #11123 with a handwritten translation and real Dafny checks. It yields
  one conditional model refutation: padding is skipped for length bounds above
  65,536. A live read-only collection on unmerged PR #11007 is rejected.
* Source collection: up to 300 changed files, 200 KB per blob, 6 MB source,
  40 context files, and 500 commentary signals/1 MB. Per-signal truncation and
  source omissions are recorded. Deleted-only files and missing diff anchors
  cannot yield a finding. Context gathering is lexical/bounded, not a complete
  language server or build-system resolver.
* General modeling: 600,000-character context, 160,000-character Dafny model,
  1,000-line source span, 20 input conditions, 30 environment helpers, and a
  bounded concrete-value witness bridge. Unrepresentable semantics abstain.
* Artifact access follows repository permissions and expires after 30 days by
  default. Download evidence before expiry.
* Paper core, historical model prompts, scoring, and experiment budgets are
  unchanged by this package. The workflow owns its language prompts and repairs.
* One bounded live model smoke (`gpt-5.6-sol`, one call) on the synthetic
  general example abstained at the goal-integrity gate; see
  [live-smoke.json](examples/general-model/live-smoke.json). No end-to-end
  GitHub Actions run has been recorded yet.
