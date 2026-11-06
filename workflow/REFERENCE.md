<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer reference

See the [overview](README.md) or [installation guide](ONBOARDING.md) to get started.

This is a prototype implementation of a GitHub Agentic Workflow that reviews a
pull request when it opens non-draft or is marked ready, selects up to **ten nontrivial properties** from the
PR description, PR conversation comments, and commit messages (not review
comments or reviews), and from changed code/comments. A separate source-context
AW session first inspects a shallow trusted-base checkout and changed-head
blobs as text. It never builds or executes the inspected repository. NFV then
translates the properties into **Dafny**, tries verification first and, if that
does not succeed, tries conditional model refutation. A deterministic gh-aw SDK
driver owns extraction, the complete audit and verbatim report submission inside
the AWF sandbox. No outer model session supervises background shell commands.

The default path accepts source in **C, C++, Java, C#, Go, JavaScript, TypeScript,
Python, and Rust** and translates the code each property is about, including loops
and branches, into the verifier's language.

Rust function capture supports fixed-size and nested array types in parameters
and return types while preserving the original source line numbers.

* **Verifier backends:** only **Dafny** (the retained NFV Dafny backend) is
  currently supported, and all source languages use it. Other verifiers (e.g.,
  Lean) can be added as backends. CBMC is used only by the paper artifact.
  Verifier-specific details below (constructs, annotations, versions) describe
  the Dafny backend.
* Each property gets up to **6 syntax/type repairs** and **10 annotation-only
  repairs** of the negative check.
  Draft audit diagnostics identify source-tag errors and missing helper metadata
  together. Definite-assignment and sequence-bound errors must be repaired before
  the model is frozen. Drafts rejected by Dafny's Boogie resolution stage use the
  same bounded draft-repair budget; they are never accepted without a successful
  verifier run. Other verifier/tool failures still interrupt the audit.
* Audits start on non-draft `opened` and `ready_for_review` events targeting the
  onboarded repository's default branch, including fork PRs and external
  contributors, whose changed paths include a supported source suffix.
  The `pull_request_target` workflow runs trusted base-branch code; PR-head code
  is captured only as immutable data, never executed. External contributors can
  trigger billable inference under the repository's configured limits.
  Each draft-to-ready transition
  explicitly requests an audit of the latest head. Reopening does not.
  Drafts and documentation/configuration/submodule-only PRs are skipped before
  either inference session starts. Keep NFV advisory, not a required merge check.
  `synchronize` events participate only in per-PR workflow concurrency: a push
  cancels an older attempt but starts neither inference session nor a new audit.
  These cancellation-only Actions runs may appear skipped. Later changes are not
  automatically re-audited; toggle draft to ready to request another audit.

The source-context agent selects only paths from a captured, supported-source
inventory and emits a files/symbols list, not snapshot metadata. Trusted code
binds it to the captured PR identity, validates every path and identifier, then
uploads the manifest. The handoff transfers it only after successful validation,
without trying to download an absent artifact after a preparation failure.
The analysis independently validates the identity and recaptures helper bodies
by immutable blob SHA. Invalid hints remain explicit
preparation failures; they never become an empty successful formal review.

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
integers, floor division, dynamic types, and aliasing, and Rust debug/release
overflow, panics, Option/Result, and ownership/borrows. This guides translation;
it is not a mechanically verified source-language frontend.

Every selected property has exactly one outcome: **refuted**, **verified**,
**resources exhausted**, or **type-check/audit failure**. Verification means
the translated model satisfies the property under its stated input and
environment assumptions, not that the source program is correct. A verified
property gets an explicit note and checked postconditions. Resource limits and
invalid models have separate diagnostics, not additional outcome categories.

Accepted refutations have two independent, **default-on** follow-ups:
**a shorter checked property** and **a model-checked candidate patch**. The shorter
claim is displayed only when the complete property implies it with inputs and
outputs free, and its negation and input-domain check pass on the frozen model
and accepted counterexample. This is bounded projection search, not a guarantee
of a globally minimal property or a proof of the natural-language wording.

A candidate fix changes only the entry body: the complete property's goals,
input interface, retained assumptions, helper contracts and helper bodies stay
fixed. The repaired model must verify that complete property, pass the general
input-domain check, and verify with the original witness and its input-domain
check. Only then does the report say **refutation-with-fix (model-checked
candidate)** and show the suggested source patch. The patch must replace a
unique span inside the captured entry; it is data, never applied, built or
executed. This strengthens the model-level evidence, **not** evidence of a
confirmed source bug or a source-verified repair.

There are at most two tool-free follow-up proposals per accepted refutation,
with a separate time budget of up to 600 seconds (bounded by the property budget)
and the configured per-call verifier limit. Each follow-up can succeed without
the other. Unestablished follow-ups have explicit diagnostics and leave the
accepted refutation unchanged; inference or verifier infrastructure failures
stop publication. Native outcomes and property counts never change.
Separate `followups.json` evidence binds to the unchanged report and source
bundle and retains proposals, checks and patch/model hashes. Hashes bind
provenance, not authentication or source/model semantic correspondence.

## What developers receive

The source identity is the event's **immutable PR head SHA**, with the base SHA
recorded and changed files obtained from the immutable `base...head` comparison.
A PR whose head or base moves during collection is marked superseded, without
an audit verdict or report. Pushes request cancellation of older workflow runs,
not an audit of the new head. Cancellation and collection checks are not a
race-free guarantee of detecting every change after collection.

The agent posts **one PR comment** (never a
review approval or change request). It opens with a one-line description of NFV
linking the [paper](https://arxiv.org/abs/2608.21516), before the issue count,
the analyzed head and base, and the run link. Property sections use short
headers (`P1`, `P2`, ...) with the full intended or inferred property statement
in normal-sized text below the header, or the shorter separately checked
statement for an enriched refutation. Refutations appear first; property labels
retain their original analysis numbering. Each finding shows **Intended
property** in normal-sized text and its quote, **Input on which it fails**, and
**Sketch of the failure**. A collapsed **Details** section shows the relevant
code and call sites with natural-language loop invariants and specifications
overlaid as comments: retained assumptions as Requires, the intended property
as Ensures, and each helper's behavior as Spec. The minimized Dafny and repair
information follow in a nested collapsed section. Modeling caveats stay in the
evidence artifact. Linked call relationships identify the caller,
the helper definition, and the call-site line, which is also marked in the
source snippet. These are static call sites, not an execution trace; a displayed
call may be skipped by a branch or short-circuit condition.
Saved reports without typed natural-language invariant/spec notes still show
the retained input conditions, intended property, and helper specifications;
missing loop descriptions are not fabricated during publication.
Verification and diagnostic details are compacted before any refutation details
to fit the 60,000-character comment budget. If refutation excerpts still exceed
that budget, the report links to their full evidence in the run artifact.
Every property, outcome, counterexample, retained condition and evidence hash
remains visible.
If a refutation's source-presentation metadata is missing or invalid, `audit`
records the diagnostic and requests a separate, hash-bound presentation from
a tool-free session. Accepted proof evidence is unchanged and never rerun;
invalid refreshed mappings still stop publication.
Presentation requests include the captured entry and accepted source anchors
with nearby rows, not entire files; the complete system/user payload must fit
the same bounded request budget as analysis.
`audit`, general `run` and `evaluate` generate the enabled follow-ups after
acceptance. `present` remains proof-free, and `publish` only validates saved
evidence. Both automatically read `followups.json` beside the report, or accept
`--followups PATH`; `publish` similarly accepts `--presentation PATH`.
The original complete property and all accepted proof evidence remain in the
artifact even when the visible property is shortened. Candidate patches are
compacted to artifact references only if required by the comment-size budget.
Draft array/sequence range errors are repaired before freezing, using the
verifier's actual diagnostics to establish justified domains or annotations.
An ordinary unproved postcondition instead proceeds to refutation; it is not
treated as a syntax failure.
The configured verifier timeout also sets Dafny's per-obligation time limit;
its internal timeouts are retained as resource outcomes rather than ordinary
postcondition failures.
Property extraction prefers changed implementations, using tests as intent
evidence rather than refuting test expectations with invented solver/API
responses. Caller-owned logic remains eligible when its property does not
depend on unspecified callee correctness.

The translator also supplies report-only **source presentation** metadata. It
reverse-maps each witness key to an expression copied from captured source,
with exact line anchors, and explains the failure using a source condition or
call instead of ghost Boolean results. The verifier's parameter names, values,
computation, goal and acceptance do not change. State without a justified direct
mapping is explicitly labeled **abstract input**, with a reason; it is not
presented as a reconstructed source object or concrete source value. Captured
anchors establish provenance, not semantic equivalence. Invalid presentation
metadata stops rendering rather than silently dropping inputs or mappings.
Source-expression values come first; unmappable values use short source-oriented
labels under **abstract state**. Their reasons and verifier-name mappings stay
with the nested minimized-Dafny evidence. Reports without this metadata retain
their original presentation.
Sequences summarizing separate source predicate queries use anchored
**abstract per-element results**, not a claim that one source call returns that
sequence.

The comment summarizes the four outcomes and lists every selected property.
Verified properties put the checked Dafny `ensures`, assumptions and evidence in
collapsed **Verification details**, with full verified models in the artifact
rather than inline. Under size pressure, verification details link to the full
artifact while retaining the assumptions and evidence hash.
Failures show their category and a collapsed diagnostic excerpt of at most
300 characters; full diagnostics remain in the artifact. Source-inferred
properties are labeled separately from quoted intentions. Each new push hides
the previous NFV comment. Verifier models, repair logs and hashes are in the run's
`nfv-evidence` artifact. Interrupted audits retain incremental evidence marked
incomplete; it cannot be published as a finished review.
Provider/SDK failures and verifier infrastructure failures stop the audit,
retain incomplete evidence, and fail the job without submitting a review.
Completed analyses submit the generated comment for all four property outcomes,
including resource exhaustion and property type-check/audit failures, even when
there are no refutations. These outcomes do not fail the job.

The sandbox driver records its attempt separately in `review-status.json` in
`nfv-evidence`, and emits the same status in its logs. A completed attempt has
`status: completed`, `completed: true`, outcome counts, and
`publication: submitted`: the comment intent was queued, not confirmation that
the later safe-output job posted it. A driver-observed setup, input, extraction,
audit or submission failure instead records `status: infrastructure_failure`,
`completed: false`, the stage and diagnostic, and the captured identity when
available. Submission errors leave publication `unconfirmed`; they are not retried.
`analysis_completed` distinguishes an accepted completed analysis from a later
submission failure without changing the saved proof report.
Infrastructure failure is an attempt status, never a fifth property outcome or
a synthetic successful review. When evidence cannot be written, the diagnostic
remains in logs. Failures before the Python driver starts and later safe-output
failures remain in their Actions job logs.

An obsolete collection records `status: superseded`, `completed: false`,
`analysis_completed: false`, `publication: not_needed`, its captured identity and
a diagnostic. The driver acknowledges a no-op instead of extracting properties,
running an audit or submitting a report. An already-started source-context
session may still use a model call to hand off the skipped status. Supersession is an operational skip,
never a completed audit, proof verdict, empty successful review or property
outcome. Other collection failures and unsuccessful no-op submissions still fail.
The default `nfv-workflow collect` CLI remains strict; its workflow-only
`--status-out` option distinguishes typed supersession from genuine failures.

Permissions and other failure exit codes are unchanged. NFV remains
advisory and must not be required for merging, but infrastructure failures can
still produce a red Actions check; there is no no-red-cross guarantee.

An individual model-request timeout remains a property resource-exhaustion
outcome. The sandbox allows at most 500 inference requests and 500 consecutive
cache misses: independent sessions are not required to hit a prompt cache.
The existing AI-credit and wall-time budgets still apply.

## Verification/refutation pipeline and gates

1. **Collect as data.** Read regular Git blobs by immutable SHA and verify their
   Git blob hashes. Gather changed code, nearby declarations, precondition/assert
   lines, callee names, quoted includes/companions, and bounded same-language
   sibling/import context. Context is fetched nearest-first: files named after
   identifiers on changed lines, then direct includes and imports, then their
   dependencies, with headers ahead of implementations and same-directory neighbours
   last. Retain this immutable collection for evidence, with omissions and a
   complete regular-file inventory.
   PR source is never checked out for execution, compiled, imported, or tested.
   Before analysis, a separate source-only AW session shallowly checks out the
   immutable base and inspects changed-head text. It selects at most 16 source
   paths and eight identifiers per file. C++ qualified names and operator
   hints are normalized to unqualified lexical lookup names.
   Trusted code validates this manifest,
   fetches the corresponding head blobs and verifies their hashes; agent prose
   is never a semantic contract. Additional source is limited to 2 MB.
2. **Postulate nontrivial properties.** Trusted code identifies changed functions
   and supplies snapshot-bound function IDs and changed anchors. The proposer
   shortlists up to ten functions from PR intent and bounded declaration previews,
   then reads each selected function's full scoped source and captured cross-file
   context in a separate tool-free session. Requests share a ten-property budget;
   selected and omitted functions are recorded in `extraction.json`. Previous
   NFV bot reports are excluded from developer-intent signals.
   The proposer selects an ID instead of inventing line numbers. Exact intent quotes or
   captured-source quotes from the selected function are mandatory; a source
   quote need not itself be a changed line. Skip properties obvious from a
   single unconditional operation. Deduplicate and select at most ten
   properties. Process the selected properties caller-first in the
   captured same-file lexical call graph, including paths through unchanged
   functions. Recursive groups and equal-depth properties retain extraction
   order. This orders requests; it does not change a property's entry function
   or claim anchors, and is not a resolved cross-file/runtime call order.
   Each property belongs to one changed entry function and retains its whole
   behavioral statement. Do not decompose intentions into atomic propositions:
   related bounds, state updates and return-value conditions may remain together
   as one property. Select distinct conjectures within the ten-property budget.
3. **Translate and resolve.** The tool-free NFV `CopilotChat` client produces a
   canonical verifier model (Dafny), original `ensures`, source mappings,
   environment descriptions, and inferred input conditions. Each property's
   context contains one identifiable changed function, at most two direct
   same-file callers and two callees, and bounded declaration/comment excerpts
   for other calls and referenced types/constants. These are lexical candidates,
   supplemented by bounded cross-file definitions from the context session,
   not resolved overloads or receiver types. It does not recursively expand the
   call graph. Rust function candidates require a `fn` declaration; visibility
   qualifiers and function-pointer types are not function definitions. Rust
   string and byte-string continuations are masked while preserving source
   offsets and line numbers. Original repository line numbers are preserved;
   omitted bodies are environment. Relevant captured callee bodies are translated as checked,
   source-derived helpers. Captured callers inform intent and applicability,
   without automatically restricting the entry's input domain to those calls.
   Ambiguous boundaries, anchors spanning separate functions, or a changed
   function exceeding the 32,000-byte source-context budget produce the
   corresponding audit-failure or resource-exhaustion outcome.
   Omission notices are compacted with an explicit count when needed to keep
   an otherwise bounded source context within the same budget.
   Every translation and repair request checks the entire system and user
   payload against an 80,000-byte UTF-8 budget, leaving output headroom.
   Large verifier diagnostics are explicitly shortened in repair prompts;
   full logs and frozen models remain in the evidence. Oversized immutable
   repair requests first deduplicate source rows already quoted by exact model
   provenance tags, retaining references and whitespace to reconstruct the full
   captured text. Unquoted cross-file source remains in the request; budgets are
   not increased. Requests still too large exhaust their budget before inference.
   Up to six repair requests may correct draft syntax, types and contracts while
   keeping the source property fixed. Each draft and its diagnostics are saved. Trusted code
   extracts the draft's actual postconditions rather than trusting a duplicate
   model-authored copy. Each attempt runs `dafny resolve`—**not verification of
   the original property**. The first fully audited, type-correct model is frozen.
   The workflow permits Dafny warnings, retaining them in evidence, without
   suppressing errors, integrity gates or proof obligations. This policy is
   confined to `workflow/`; the paper's `nfv/` implementation is unchanged.
4. **Verify each whole property first, then invert.** Each selected conjecture
   retains its own ID, source context, outcome and counterexample request. Its
   entry may contain conjunctions and multiple `ensures`. Check all obligations
   together as one property; no child properties are created and no conjunct
   is discarded. Implications, disjunctions and
   quantified bodies retain their grouping. Progress reports
   `properties_completed`, `properties_total` and `properties_remaining` for
   the whole selected review, including rejected proposals; the total never
   grows during translation. Interrupted properties are not logged as completed.
   Check each original model with
   Dafny. An accepted proof plus a successful feasible-input guard records
   **verified**, without requiring a counterexample. Otherwise ask for a
   concrete witness if the translation did not already supply one. Reuse NFV's
   `invert_entry_spec` to negate the conjunction of all its original postconditions
   mechanically. Exact input equalities are installed
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
environment helper's captured anchors are checked. New translations cannot cite
computation or environment contracts outside their supplied excerpts. This is
**provenance coverage, not semantic equivalence**.

Helpers must have checked bodies and explicit source/inferred environment
descriptions. Code the slice calls but does not translate is environment: the
translator models it from the callee's captured implementation, declaration and
comments, preferring the weakest contract those lines justify, and leaves
undocumented results as entry inputs. Omitted implementations are not
reconstructed; the captured declarations/comments justify the weakest
environment contracts, or the property has a type-check/audit failure when needed behavior is
undocumented. No axioms, `assume`, `expect`, bodyless callable contracts,
verification-disabling attributes, includes/imports, opaque definitions, or
termination escapes are allowed. No model-authored shell command is executed.
Canonical callable signatures/contracts are complete single lines and their
opening body brace is on its own line. Certain advanced Dafny constructs, such
as subset/newtype aliases and uninterpreted constants, fail the model audit.

The current concrete-call bridge supports named **`int`, `bool`, `seq<int>`,
and `seq<bool>` input/return values**. Heap/graph initial state may be modeled
through these value parameters; arbitrary object identity or constructor-based
witnesses are not silently guessed. This is still a bounded modeling frontend,
not a complete compiler for any of the nine source languages.

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

For installation, upgrades, authentication, required secrets and the
draft/disable/merge sequence, use the [onboarding guide](ONBOARDING.md).
This section is the configuration reference, not a second installation procedure.
The [standalone installer](install.py) requires Python 3.10+, Git and authenticated
`gh`; `python install.py --help` lists installation, bootstrap-verification and
post-merge activation options. Workflow locks use **gh-aw v0.88.7**. Commit both
workflow sources and their `.lock.yml` files, plus generated package records and
action locks under `.github/aw/`.

### Repository variables

Configure these as protected Actions repository variables:

| Variable | Default / purpose |
|---|---|
| `NFV_RELEASE_SHA` | Full trusted NFV commit; both installed package pins and runtime must use this same SHA |
| `NFV_MODEL` | Copilot model ID selected with installer `--model MODEL`. The billing identity and its policies must permit the selected model |
| `NFV_SYNTAX_ROUNDS` | 6 repairs, maximum 20 |
| `NFV_PROOF_ROUNDS` | 10 repairs, maximum 30 |
| `NFV_VERIFIER_TIMEOUT` | 60 seconds per Dafny call |
| `NFV_PROPERTY_SECONDS` | 1800 seconds per property |
| `NFV_SIMPLIFY_REFUTATIONS` | `true`; enable the shorter checked-property follow-up |
| `NFV_CHECK_FIXES` | `true`; enable the model-checked candidate-fix follow-up |

Unset budget variables retain their defaults. Properties run sequentially within
the 360-minute job timeout. The two independent Boolean flags accept only `true`
or `false`; unset or empty means enabled. Local `audit`, `run` and general
`evaluate` also accept `--no-simplify-refutations` and `--no-check-fixes` (or
their positive forms). Activation and upgrades retain the approved model.

**Historical replay PRs:** Use plain change numbers, not upstream issue/PR
references, direct GitHub links or user/team mentions. Normalize copied
descriptions and retain exact originals in local evidence. Clickable provenance
can use GitHub's documented `redirect.github.com` no-backlink URLs; these do not
make replays private or erase historical references. Never edit generated reports.

### Repository permission policies

When installing or upgrading, compare the generated locks' **job-level**
permissions with the target repository's CI tests and workflow-policy allowlists.
An existing rule that only one workflow may write issues can reject NFV even
when its workflows compile correctly.

With gh-aw v0.88.7, both of these generated reviewer jobs request `issues: write`
and `pull-requests: write`:

* `nfv-pr-review.lock.yml:safe_outputs`
* `nfv-pr-review.lock.yml:conclusion`

These jobs publish the PR comment and report failures, missing tools and
incomplete runs. If the repository restricts writers, obtain approval for these
specific exceptions, retain existing approved writers and keep rejecting all
others. Update the policy's tests and documentation and run the affected checks
before merging. This is separate from `copilot-requests` inference billing.

Do not disable permission tests, grant workflow-wide write access, give the
analysis jobs issue/PR write permissions or strip generated permissions to make
CI pass: doing so weakens isolation or can remove reporting. Review the actual
locks for the pinned compiler; do not hand-edit generated files. If the required
exceptions cannot be approved, leave installation blocked.

### Advisory checks and publication credentials

NFV is advisory: do not make its workflows or jobs required merge checks or
include their results in a required CI aggregator. Keep unrelated required CI,
reviews and merge-queue policies unchanged. Operational failures must remain
visible; do not force success or use `continue-on-error` to disguise them.

Both workflows explicitly select `safe-outputs.github-token: ${{ secrets.GITHUB_TOKEN }}`.
Comment publication uses the short-lived Actions token for the current job,
with the generated job-level issue/PR permissions above, not an inference PAT
or the private-release read token. This also prevents a shared
`GH_AW_GITHUB_TOKEN` secret from overriding publication credentials.

For `safe_outputs` HTTP 401 **Bad credentials**, inspect the compiled
`Process Safe Outputs` step's token expression, without exposing token values.
Older locks prefer `GH_AW_GITHUB_TOKEN` when present; an invalid override can
break publication even when inference succeeds. Recompile both installed
sources with the explicit job-token setting. A green agent job alone does not
establish publication or a completed audit.

### Inference billing

Select `--billing pat` (default) or explicitly opt into `--billing organization`;
eligibility, policies and secrets are covered in [onboarding](ONBOARDING.md).
The installer configures `copilot-requests` in **all three** permission scopes
before compiling both workflows:

* `.github/workflows/nfv-source-context.md`: top-level `permissions`.
* `.github/workflows/nfv-pr-review.md`: top-level `permissions` and
  `jobs.repository_context.permissions`.

Organization mode requires `write` in all three scopes; gh-aw uses the per-run
Actions token and ignores inference PATs. PAT mode requires `none` and uses
`COPILOT_GITHUB_TOKEN`; OAuth (`gho_`) tokens are rejected. There is no automatic
switch or fallback, including when a PAT is absent. The AWF proxy keeps real
credentials outside the sandbox; none are exported through `GITHUB_ENV` or
given to the driver.

Activation reads the mode from immutable installed sources, rejects disagreement
and checks the corresponding secret names; it does not accept a billing override.
For a reviewed mode change, update all three scopes and recompile both locks.
Package replacement resets permissions to PAT defaults, so reapply the approved
mode **before compilation**.

### Runtime and evidence

Trusted pre-agent steps check out the pinned release, install it, download
official `dafny-4.11.0-x64-ubuntu-22.04.zip` (SHA-256
`a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179`), select its
unique bundled `z3-4.12.1`, and collect the PR source with the read-only
`GITHUB_TOKEN`. Dafny and the bundle live under `$RUNNER_TEMP/gh-aw`, which the
AWF sandbox mounts **read-only**, and are exported as `NFV_DAFNY_PATH`,
`NFV_Z3_PATH` and `NFV_BUNDLE`. Trusted collection also writes `nfv-input.json`
in the workspace: a non-secret extraction index containing PR signals,
changed-line anchors, captured source paths, and omissions, without duplicating
the source bodies. Tool-free sessions receive bounded source contexts, not a shell
or repository tools; trusted Python code invokes the actual type checker and
verifier inside the sandbox. Input/access/tool failures fail the driver, not an empty audit.
An empty proposal list is reserved for completed inspection that finds no
grounded nontrivial property.

The custom SDK driver runs extraction, then `audit`, as one foreground process
inside gh-aw's AWF sandbox. Its small Node.js supervisor launches the installed
Python environment, forwards cancellation and emits liveness heartbeats; NFV
keeps its own pinned Python SDK. The supervisor removes platform-authentication
variables before starting NFV; Python retains a fail-closed credential guard.
Automatic whole-driver retries are disabled. gh-aw provisions the trusted CLI before sandbox
launch and resolves provider endpoints through AWF reflection. NFV connects to
that harness-owned sidecar using `COPILOT_SDK_URI` and its per-run connection
token; it never downloads or starts another CLI inside the sandbox. Tool-free
sessions receive the provider explicitly, with a placeholder key; the proxy
injects the actual credential. There is no supervising or publishing LLM.

Evidence is written under `/tmp/gh-aw/sandbox/agent/logs/nfv` and uploaded as
`nfv-evidence`, including interrupted work. Only after extraction and the complete
audit succeed does the driver pipe the unchanged `comment.md` as JSON to
`safeoutputs add_comment`. A failed or interrupted execution is never restarted
in the same evidence directory, preventing duplicate write intents. Post-agent
steps only retain evidence; they never perform analysis. gh-aw threat detection
screens that output before the `safe_outputs` job, the only job with write access,
posts the comment. Never check out the PR head or execute PR scripts. Extraction,
translation and verifier processes run within the additional AWF isolation and
network restrictions on an ephemeral GitHub-hosted runner; this is not a claim
that the sandbox or verifier is invulnerable.

## Local commands and model configuration

Authenticate Copilot CLI with an approved credential, then select a model ID
available to that credential. For GitHub Actions, configure `NFV_MODEL` and
the selected [inference billing mode](#inference-billing) as described above.

To refresh the presentation of an existing accepted general report, without
rerunning an audit or verifier:

```powershell
nfv-workflow present --report .\nfv-evidence\report.json `
  --bundle .\nfv-evidence\bundle.json --model APPROVED_MODEL `
  --evidence-url https://github.com/OWNER/REPO/actions/runs/RUN_ID `
  --out .\nfv-presentation
```

This billable, tool-free agent pass validates the saved evidence before making
at most one request per finding (maximum ten; `--model-timeout` defaults to 240
seconds per request, bounded at 600). It writes a separate `presentation.json`
bound to hashes of the complete report and bundle, records agent-output hashes
and call provenance, and deterministically generates `comment.md`. The original
report and bundle are untouched; no source is executed, no proof is rerun, and
nothing is published. Invalid or unsupported mappings fail the command.
The agent receives the files used by the checked source mappings and helper
anchors; other captured context is omitted explicitly.
The artifact can be rendered again without inference using
`nfv_workflow.publish.render(report, bundle, evidence_url, presentation=artifact)`;
the trusted renderer revalidates its bindings, source anchors and coverage.
Use only evidence and artifacts produced by trusted tooling: hashes detect
mismatches but do not authenticate authorship or prove reverse-mapping fidelity.

```powershell
# Read-only collection of a real PR event payload (open or merged). Optional --sources-dir
# also writes the captured sources as plain files with repository line numbers.
nfv-workflow collect --event event.json --out .nfv-work\bundle.json

# Explicitly billable extraction and general refutation; replace placeholders.
$env:NFV_MODEL = "YOUR_APPROVED_MODEL"
nfv-workflow extract --bundle .nfv-work\bundle.json --model $env:NFV_MODEL --out .nfv-work\properties.json
nfv-workflow run --bundle .nfv-work\bundle.json --properties .nfv-work\properties.json --model $env:NFV_MODEL --syntax-rounds 6 --proof-rounds 10 --out .nfv-work\evidence
```

Never commit credentials or local environment files. Model access and billing
follow the configured credential's permissions and service policy.

Before/after analysis of the same merged PR (for example a bug fix) snapshots the
pre-merge base with the head's context files, anchors on removed lines, and moves
head-side claims to the removed lines of the same diff block. Base-side bundles are never
publishable:

```powershell
nfv-workflow collect --side base --event event.json --out .nfv-work\bundle-base.json
nfv-workflow reanchor --bundle .nfv-work\bundle-base.json --properties .nfv-work\properties.json --out .nfv-work\properties-base.json
nfv-workflow run --bundle .nfv-work\bundle-base.json --properties .nfv-work\properties-base.json --model $env:NFV_MODEL --out .nfv-work\evidence-base
```

`audit` is the AW entrypoint; `evaluate` is the standalone equivalent that also
collects fresh source and reads properties from an `nfv_submit` item.
Their defaults are `--mode general --syntax-rounds 6 --proof-rounds 10`.
`--verifier-timeout` defaults to 60 seconds. Each model request defaults to
240 seconds, with a 30-minute per-property wall limit checked between operations.
The ordinary maximum is **18 logical model requests per property**
(one translation + six syntax repairs + one witness proposal + ten annotation
repairs), plus extraction and the separate source-context session.
The retained SDK may retry transport failures up to four times per request.
Model choices and budgets must be approved before enabling the workflow.

Publication is preview-only unless `--apply` is supplied:

```powershell
nfv-workflow publish --bundle .nfv-work\bundle.json --report .nfv-work\evidence\report.json --evidence-url https://github.com/OWNER/REPO/actions/runs/RUN_ID/artifacts/ARTIFACT_ID --category General
```

Use an actual uploaded artifact URL, never a fabricated example URL. An immutable
commit path (`https://github.com/OWNER/REPO/tree/<40-hex SHA>/path`) is also
accepted, e.g. for evidence committed to a separate repository. `--repository
OWNER/REPO` posts the Discussion to another repository (default: the PR's);
source links still point at the PR repository. Only consume
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
nfv-workflow check --bundle bundle.json --proposals proposals.json --out .nfv-work\scalar
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
  Dafny checks**, actual type and annotation repair, the 6/10 repair bounds,
  altered goals/computation, fabricated provenance, inconsistent witnesses,
  unsafe/bodyless models, and returning-case failures. No inference is required.
  Run `python -m unittest discover -s workflow\tests -v`.
* Source collection: up to 300 changed files, 200 KB per blob, 6 MB source,
  80 context files, and 500 commentary signals/1 MB. Per-signal truncation and
  source omissions are recorded. Deleted-only files and missing diff anchors
  cannot yield a finding. Context gathering is lexical/bounded, not a complete
  language server or build-system resolver.
* General modeling: 600,000-character context, 160,000-character verifier model,
  1,000-line source span, 20 input conditions, 30 environment helpers, and a
  bounded concrete-value witness bridge. Unrepresentable semantics fail the
  model audit rather than yielding a correctness claim.
* Artifact access follows repository permissions and expires after 30 days by
  default. Download evidence before expiry.
* Paper core, historical model prompts, scoring, and experiment budgets are
  unchanged by this package. The workflow owns its language prompts and repairs.
* No end-to-end GitHub Actions run has been recorded yet.
