<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Neuro-Formal Verification (NFV) and nl2post-safe: Code and Dataset Transparency Note

**Draft for review - 4 November 2026.**

## Overview

Neuro-Formal Verification (NFV) is an open-source research prototype that combines
language-model-generated formal models with automated verification and refutation. The proposed
GitHub release has two components: the paper's research code, derived dataset, retained results and
experiment tools; and a GitHub Agentic Workflow (AW) reviewer template/package for experimentation
on authorized open-source repositories. Repository:
https://github.com/microsoft/neuro-formal-verification. This is a research release, not a customer
production tool.

nl2post-safe is the derived evaluation dataset released with NFV. It contains 206 small Python
programs arranged as 103 reference/mutant pairs, with shared postconditions and English task
descriptions. It supports research on model-level verification and refutation, not claims of
independently established source-code correctness.

The release does not distribute foundation-model weights. NFV uses existing language-model services
through the GitHub Copilot SDK; generated formal models are program-reasoning artifacts, not
foundation models. Consult https://docs.github.com/en/copilot/reference/ai-models/supported-models
and the selected provider's model documentation for model-specific capabilities and limitations.
Dataset attribution, derivation and license notices are documented at
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/DATA_PROVENANCE.md. The paper,
Neuro-Formal Verification: Agentic Language-Agnostic Formal Program Reasoning, describes the
research and evaluation: https://arxiv.org/abs/2608.21516. Component documentation:
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/README.md and
https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/README.md.

A detailed discussion of the research can be found in the paper: https://arxiv.org/abs/2608.21516.

### What can NFV and nl2post-safe do?

NFV was developed to investigate how language models and formal tools can reason about program
behavior. The paper artifact supports inspecting retained evidence, replaying saved formal checks,
running new experiments, and comparing methods using Dafny and CBMC. The AW reviewer selects up to
ten nontrivial properties from PR intent and changed source, translates scoped code to Dafny, tries
verification first and then conditional model refutation, and posts one generated evidence comment.
The AW path uses Dafny only. A finding is a conditional MODEL refutation, not a confirmed source
bug; a proof likewise concerns the translated model under its assumptions, not automatically the
source.

nl2post-safe lets researchers compare how methods reason about the same behavioral property for a
reference program and a selected mutant. The paired structure supports checking whether a method
distinguishes the mutation, while retaining uncertain benchmark labels and abstentions for analysis.
It can be inspected independently of NFV or used with other appropriately configured research tools.

### Intended uses

NFV is best suited for researchers reproducing, comparing or extending the paper's work, and
open-source developers and maintainers trying the AW reviewer in repositories or forks they are
authorized to use. It can help participants examine selected behavioral expectations, explicit
assumptions and machine-checked model evidence.

nl2post-safe is intended for reproducible evaluation and methodological research with NFV or
comparable program-reasoning methods. It is an evaluation benchmark, not a curated training corpus
or representative sample of production software.

The release shares the paper's artifacts and dataset, lowers the effort of trying the technique on
real-world open-source code, and aims to generate research-community interest and voluntary
feedback. Learning about useful results, misleading outputs, setup barriers, costs and review burden
should help refine the prototype and guide subsequent research. These are intended benefits, not
measured adoption or productivity claims.

NFV is intended for domain experts who can independently assess the selected property, input
restrictions, environment/helper assumptions, model semantics and source relevance before acting on
an output. Source investigation, ordinary code review and independent tests remain necessary before
declaring a confirmed bug or applying a suggested fix.

### Out-of-scope uses

NFV is not a comprehensive source-code verifier, security audit, production assurance service or
correctness certificate. It should not be used to approve or merge changes autonomously, score
individual contributors, or claim that an absence of findings means a repository is safe. Private or
regulated source is outside the intended open-source experimentation scope.

nl2post-safe should not be used as a correctness oracle, as a comprehensive security benchmark, to
rank individual developers, or to certify real-world deployment. The inherited labels and generated
specifications can be disputed or incomplete.

The dataset covers selected small Python functions and primarily English task descriptions. It does
not represent large multi-file applications, the other source languages accepted by the AW
prototype, or a systematic range of concurrency, operating-system, distributed-system or
security-critical behavior. Broad cross-language or production-readiness conclusions are not
supported.

Trying the AW reviewer on real-world open-source repositories is an intended research experiment,
not a production-readiness claim. Commercial or operational reliance would require further
development, testing and validation beyond this release.

NFV was not designed or evaluated for all downstream purposes. Anyone adapting it must assess
accuracy, safety, privacy and fairness for the proposed use and preserve meaningful human oversight.
A mathematically valid proof of an inaccurate model can still mislead.

Without independent, domain-appropriate assurance, do not rely on NFV for decisions or changes whose
failure could cause injury or affect a person's legal, financial or life opportunities. The
prototype alone is not sufficient assurance for sensitive systems.

High-risk decision making, including law enforcement, legal, financial and healthcare decisions
about people, is outside the intended use. The tool does not evaluate people or establish fitness
for these domains.

## Dataset details

### Dataset contents

nl2post-safe is distributed as paper/data/nl2post_safe.json: a UTF-8 JSON object with an entries
array containing 206 programs from 103 distinct HumanEval tasks. Each task contributes one reference
entry and one mutant entry, giving 103 entries labeled correct and 103 labeled incorrect.

Each entry includes entry_id, task_id, entry_point, response_num, code_type, label, code,
postcondition, docstring, code_sha256 and killing_test_present. The docstring field contains
separately retained task prose, shared across the pair; it is not embedded in sanitized code. The
code_sha256 value is a 16-hex-character SHA-256 prefix, not a full digest. killing_test_present
indicates upstream witness availability; the main dataset does not include the killing-test input
itself.

The labels correct and incorrect are inherited reference/mutant benchmark labels relative to a
selected postcondition. They are not independent proofs of complete program correctness or confirmed
defects. The original labels, including disputed labels discussed in the paper, are preserved rather
than silently relabeled.

The sources include the nl2postcond artifact associated with FSE 2024 and its HumanEval/EvalPlus
material. Exact dates of upstream acquisition and NFV dataset derivation are not recorded in the
retained acquisition metadata; the conference year is not an acquisition timestamp.

The main dataset JSON contains no HTTP/HTTPS links and its program/specification records can be
inspected without fetching external data. Accompanying documentation links to upstream sources,
licenses and reconstruction information:
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/DATA_PROVENANCE.md. Repeating
the original acquisition requires external upstream inputs and historical dependencies that are not
all bundled.

### Data creation and processing

nl2post-safe is adapted from microsoft/intent-formalization's nl2postcondition-fse2024 research
artifact: https://github.com/microsoft/intent-formalization/tree/main/nl2postcondition-fse2024. The
upstream research is nl2postcond (Endres et al., FSE 2024). The acquisition recipe selects generated
postconditions from gpt4_simple_no_ref.zip (the GPT-4 SimplePromptNoRef output) and code mutants and
distinguishing inputs from distinct_code_mutants.jsonl.zip.

The source material consists of Python programming problems, task descriptions and reference
solutions from HumanEval via EvalPlus, plus upstream generated postconditions, mutants and
distinguishing inputs. HumanEval is attributed at https://github.com/openai/human-eval and EvalPlus
at https://github.com/evalplus/evalplus. These are technical benchmark artifacts, not a corpus of
user conversations or an NFV collection of contributor behavior.

The HumanEval problems originate with OpenAI; EvalPlus redistributes and extends the benchmark. The
nl2postcond authors produced the postcondition/mutant research artifact redistributed by
microsoft/intent-formalization. NFV's research team selected and transformed retained records rather
than authoring all benchmark programs or collecting a new human-participant dataset.

Stage 1 uses build_nl2post_dataset.py to join upstream records and select one
postcondition/reference/mutant pair per selected problem, choosing by upstream mutation kill rate.
The acquisition code checks that the mutant fails the postcondition with AssertionError and the
reference passes on the distinguishing input. The retained stage-1 output has 155 pairs (310
entries). These finite execution checks do not prove a postcondition for every input.

The NFV research team used automated scripts and a hosted language model for additional
specification screening. Stage 2 requests 100 generated tests per reference and retains pairs with a
passing reference-check status. Its retained summary records 116 passes, 26 exceptions, 6
specification failures and 7 runtime errors, leaving 116 pairs (232 entries). A nonpassing status is
not uniformly evidence of an unsound specification. The original generated tests are not retained,
and the archival adapter needed to regenerate them is not shipped.

The documented NFV acquisition recipe uses specific public research artifacts and an EvalPlus
loader; it does not describe a broad web crawl. The historical upstream commit, EvalPlus package
version and downloaded cache digest were not recorded. Retained selected intermediates support
deterministic downstream checking, but do not establish bit-identical replay of the original
acquisition.

#### People and identifiers

A record represents a programming task and program variant, not an individual person's behavior,
demographics or characteristics. HumanEval task IDs, entry IDs and response numbers identify
technical records, not study participants. Names or domain words in synthetic programming examples
should not be interpreted as observations about real people.

The dataset is not intended to contain data about children and was not constructed through
recruitment of children or other human subjects. The released materials do not provide a systematic
age-related content annotation or a categorical guarantee that no example could refer to a child.

The documented upstream selection concerns code, specifications and technical identifiers. The
acquisition records do not establish a comprehensive historical inventory of personal identifiers in
every upstream input. Upstream attribution and license notices are retained and are separate from
benchmark task labels.

Recorded privacy-oriented processing anonymized host-specific paths in selected diagnostic metadata,
as documented in paper/manifest.json and paper/provenance/redactions.json. This did not change
program/model code, inputs, labels or verifier verdicts. The separate code/docstring sanitization
described below was designed to reduce benchmark leakage, not to certify de-identification.

The main dataset is not designed to include personal records or identify people. That design intent
and the diagnostic-path redactions are not a comprehensive privacy guarantee for all source
material, retained artifacts or new tool outputs. Users should review the particular material they
redistribute or submit to a model.

#### Sensitive or harmful content

The source artifacts are programming benchmarks rather than collections of health, financial,
biometric, political, religious or other sensitive personal records. The retained provenance does
not document a comprehensive sensitive-category annotation of the original upstream corpus.

The benchmark was not assembled to study offensive or harmful content. The retained records do not
establish a systematic audit of upstream examples for sexual content, violence, hate, self-harm or
other potentially distressing material.

Apart from the documented diagnostic-path anonymization, no category-specific sensitive-content
removal process is established by the retained materials. Do not interpret the name nl2post-safe as
a privacy or content-safety certification.

The main dataset is not intended to contain sensitive personal information; a comprehensive absence
claim has not been established. The same caution applies to newly generated model responses,
diagnostics and workflow artifacts.

Comment, docstring, doctest and assertion-message removal addressed label leakage and evaluation
design. It was not a harmful-content filtering procedure, and no dedicated harmful-content removal
is documented.

The dataset has not been systematically certified free of potentially offensive or harmful content.
Inspect examples for the intended audience and application, and use the project's reporting channels
for concerns without reposting sensitive material publicly.

#### Other processing

Selection is limited to one pair per selected task; the code and postcondition formatting is
canonicalized. References and mutants deliberately share the same property and task prose. This is
not a claim that every program fragment or postcondition is globally deduplicated, or that overlap
with model training corpora has been removed.

Stage 3, paper.data.build_safe, uniformly removes code comments, embedded docstrings and doctests,
canonicalizes code with ast.unparse, and reduces postconditions to imports and bare assertions
without prose messages/comments. Separately retained task prose has worked examples removed and is
canonicalized across each pair. The script drops 13 whole pairs with type-assertion-only
postconditions, yielding 103 pairs (206 entries). Labels remain inherited; there is no new human
correctness-labeling campaign.

These changes reduce direct label cues such as bug-announcing comments and worked examples; they do
not prove the absence of all leakage. Killing tests are withheld from normal static-analysis runs
and joined only in the explicitly labeled given-counterexample ablation. Upstream intermediates
retained for provenance can include those inputs and should not be supplied to a normal evaluation
as additional inference context. Pair identifiers, selected upstream response numbers and
sanitized-text hashes are recorded in paper/data/MANIFEST.md; file-level provenance and checksums
are recorded in paper/manifest.json.

## How to get started

Start at https://github.com/microsoft/neuro-formal-verification. For the paper artifact, follow
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/README.md and
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/SETUP.md. The documented smoke
command, python -m paper.smoke, replays four retained formal artifacts without inference; it does
not test fresh translation equivalence. For AW experiments, follow
https://github.com/microsoft/neuro-formal-verification/blob/dev/workflow/ONBOARDING.md. Choose an
authorized open-source repository or fork, review the installer and permissions, pin a trusted
immutable NFV revision, configure approved model access and credentials, and deliberately budget any
paid inference. Keep NFV disabled through the initial installation PR; activate after merge and
approve a separate pilot. Installation does not itself launch a paid audit. Never put tokens in PR
text or public feedback. To inspect the dataset, load paper/data/nl2post_safe.json and its entries
array. Read paper/data/MANIFEST.md and paper/DATA_PROVENANCE.md alongside it. The command python -m
paper.data.build_safe --check reconstructs the frozen dataset in memory from the retained
predecessor and compares it without writing dataset files or making model calls. Do not silently
regenerate historical screening or overwrite pinned data/results with a new model.

## Dataset validation

Dataset validation checks the retained derivation, paired structure, counts, code/specification
sanitization and hash correspondence. The historical distinguishing-input check and generated-test
screening provide finite empirical evidence, not universally valid specifications or independent
source correctness.

The release's offline artifact tests check the 103 reference/mutant pairs, deterministic
reconstruction from retained inputs and pinned file hashes. Saved formal-artifact smoke replay
separately checks recorded verifier verdicts; it does not re-run the original dataset acquisition,
reconstruct missing generated tests or establish semantic equivalence between source and translated
models.

Methods and limitations are discussed in https://arxiv.org/abs/2608.21516,
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/README.md and
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/DATA_PROVENANCE.md. Checksums
and retained intermediates improve traceability; they do not authenticate the original collection or
replace licensing, privacy or release review.

## Evaluation

The paper evaluates model-level verification and refutation on 206 small Python programs: 103
reference/mutant pairs derived from an existing benchmark. The labels are inherited benchmark
labels, not independently proved ground truth. This is not a representative evaluation of the AW
reviewer across real-world projects or all supported languages.

See https://arxiv.org/abs/2608.21516 for the evaluation design and findings. The release retains
result records, file hashes and configurations under paper/results and
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/manifest.json;
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/README.md explains replay and
new experiments.

### Evaluation methods

The released metrics distinguish non-abstaining coverage, accuracy among covered entries and overall
accuracy with abstentions counted as errors, precision/recall for refutation, and disagreements with
benchmark labels. Reference/mutant pair checks ask whether a refutation distinguishes the mutation
rather than firing on both programs. Label disagreements require human triage; they are not
automatically verifier failures or confirmed source defects. Metrics can be recounted with python -m
nfv.metrics on a retained result file.

The retained experiments compare Dafny verification/refutation, CBMC bounded refutation, a direct
language-model judge and less-constrained formalization variants. These paper experiments are
distinct from the AW reviewer. A CBMC bounded check without a counterexample is not an unbounded
correctness proof. The combined paper result is an offline, refutation-first join of independent
Dafny arms, not a new end-to-end AW evaluation.

The paper uses hosted language-model configurations recorded per run in paper/README.md,
paper/manifest.json and the retained results. NFV does not require one fixed foundation model; AW
users configure an available model through their approved Copilot access. Consult
https://docs.github.com/en/copilot/reference/ai-models/supported-models and the selected provider's
documentation.

Changing the model, service version, prompts, context, assumptions or budgets can change the
generated formalization and verdict. Historical hosted model-service snapshots are unavailable, so
fresh inference is not guaranteed to reproduce retained outcomes. Replay of saved models and fresh
retranslation must be reported separately.

Offline tests and verifier gates check aspects of artifact integrity, permitted model constructs and
within-run evidence consistency. Benchmark evaluation does not establish comprehensive safety,
fairness, accessibility, privacy or adversarial robustness, and is not a claim of Responsible AI
review approval or high-consequence validation.

### Evaluation results

As recorded in the release manifest, the paper's offline combination of its independent Dafny arms
produced 128 non-abstaining outcomes over 206 entries. Of these, 118 agreed with the inherited
benchmark labels and 10 disagreed; 78 entries had no non-abstaining outcome under that combination.
These are benchmark/model-level counts, not source correctness guarantees. Label uncertainty,
formalization choices and limited coverage must be considered when interpreting them. The retained
experiment does not establish real-world AW effectiveness, uniform language performance, or
production readiness.

## Limitations

### Code

NFV is a research prototype. Initial translation can omit effects, misrepresent numeric or string
semantics, impose unjustified input restrictions, or model missing helpers incorrectly. English
intent is also formalized by inference. Source tags and hashes establish provenance, not
source/model semantic equivalence. Annotation-only proof repairs preserve the frozen computation
within a run, but cannot repair every error in the initial interpretation or establish comparability
across runs.

Prompts and documented intent/benchmark material are primarily English; quality for other natural
languages has not been established. The AW path accepts C, C++, Java, C#, Go, JavaScript,
TypeScript, Python and Rust, but this is not a complete compiler or evidence of equal semantic
coverage. Source context and property selection are bounded; the current witness bridge supports
integers, Booleans and sequences of those values rather than arbitrary reconstructed runtime
objects.

Each selected AW property has one outcome: verified, refuted, resources exhausted, or
type-check/audit failure. Failure to refute is not evidence of correctness. A witness is a
counterexample in the model, not an observed source execution or necessarily an executable source
input. Optional follow-ups may display a separately checked shorter property or model-checked
candidate patch, without changing the accepted whole property or outcome. Such patches are not
source-verified fixes and are never automatically applied, built or executed. AI-authored
explanations can be misleading despite formal checks.

NFV inherits limitations and biases of the selected language model. Code, comments or documentation
styles may receive uneven analysis, and dense reports may create accessibility barriers.
Representative fairness and accessibility evaluations have not been established by the small paper
benchmark. Do not use finding counts to measure individual developer competence or performance.

Model access, availability, safety features and data-handling terms depend on the operator's service
and configuration. NFV does not establish provider retention, training-use, deletion or residency
guarantees. A provider's model card and policies are necessary context, not certification of the
combined NFV system.

The derived benchmark is small, Python-only and based on selected reference/mutant pairs. Labels
have documented limitations and upstream acquisition metadata has gaps. Possible overlap with
foundation-model training data is not fully characterized. Self-selected community examples and
voluntary feedback would introduce additional selection bias rather than establishing broad
reliability.

PR content is untrusted. The AW path inspects immutable source blobs as data and does not compile,
import, test or execute PR-head code. Tool-free analysis, sandbox boundaries and validated source
capture reduce some risks but do not constitute a complete prompt-injection defense or a
comprehensive adversarial security evaluation. Malicious content can still bias inference. Comments
and artifacts can expose tentative findings or sensitive material; completed reports are normally
posted automatically, without a general human prepublication triage gate. Paid inference and compute
use can be substantial; request/time limits are not guaranteed monetary ceilings.

### Data

nl2post-safe was developed for research and experimental evaluation. Its small, selected paired
population is not representative of arbitrary software or real-world PR changes; the AW reviewer has
not been comprehensively evaluated by this Python benchmark.

The task descriptions and documented intent are primarily English, and all released benchmark
programs are Python. Other natural languages, source languages and cultural contexts are not
systematically covered.

Potential errors include imperfect generated postconditions, misleading inherited labels,
constrained input assumptions and residual selection or leakage effects. A pair shares task prose
and specification by design; finite tests cannot establish complete label validity.

Missing historical information includes the acquisition commit/package/cache pins, exact
collection/derivation dates and the original stage-2 generated tests and legacy adapter. The
retained intermediates support downstream reconstruction, not a claim of fully reproducible upstream
collection.

Large applications, realistic repository environments and many semantic features are absent or
underrepresented. Coverage and accuracy on this benchmark should not be extrapolated to all
supported languages, security vulnerabilities or high-consequence systems.

External upstream links and hosted model services may change or become unavailable. The bundled main
dataset can be inspected locally, but complete reacquisition and fresh inference depend on external
resources.

The dataset has not been systematically evaluated for sociocultural, economic, demographic or
linguistic bias. Benchmark selection and foundation-model training overlap can also bias
comparisons. The term safe describes the documented benchmark sanitization, not comprehensive
Responsible AI validation.

Do not use the dataset or benchmark scores as sole assurance for highly regulated or high-risk
decisions affecting safety or a person's legal, financial or life opportunities.

The dataset is not restricted to one foundation model. Generated specifications, historical
screening and evaluated methods depend on hosted models and their limitations. New models or changed
prompts/budgets can change results; consult the selected provider's documentation and separate
saved-artifact replay from fresh inference.

## Best practices

Start with a small, authorized pilot and a clear behavioral question. Review the quote, complete
formal goal, input restrictions, helper assumptions, omitted context and source correspondence
before relying on evidence. Record the immutable revision and configuration; keep failures and costs
in evaluation denominators. Combine findings with ordinary review and independent source tests. Use
trigger restrictions and spending alerts: the AW path's limits of ten properties, six draft syntax
repairs and ten annotation-only proof repairs per property do not guarantee low cost. Optional
follow-ups and source-context work also consume resources.

The release does not prescribe a separate training/development/test split. For a new split, keep
each reference/mutant pair and all material from the same task together, and separate development
examples from held-out evaluation. Do not tune on test labels, distinguishing inputs or retained
verdicts and then claim an independent held-out score. Keep labels and triage/provenance metadata
out of inference inputs except in a clearly identified ablation. Use the full 206-entry denominator
for the main benchmark, retain abstentions and failures, and report any subset or relabeling
explicitly.

Use an approved Copilot/model configuration and review provider-specific Responsible AI guidance and
data policies. The following Azure/OpenAI resources are general background; they do not imply that
NFV has a direct Azure OpenAI integration or that those services' mitigations make its outputs safe:

- What is Azure AI Content Safety?
  https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview

- Responsible AI practices for Azure OpenAI models:
  https://learn.microsoft.com/en-us/legal/cognitive-services/openai/overview

- Azure OpenAI Transparency Note:
  https://learn.microsoft.com/en-us/legal/cognitive-services/openai/transparency-note

- OpenAI usage policies: https://openai.com/policies/usage-policies

- Azure OpenAI Code of Conduct:
  https://learn.microsoft.com/en-us/legal/cognitive-services/openai/code-of-conduct

Use only source and datasets you are authorized to analyze and share. Preserve upstream notices and
attribution; public availability does not remove licensing or privacy obligations. Review
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/DATA_PROVENANCE.md before
redistributing the benchmark. Check model-provider terms and the visibility/retention of PR
comments, logs and Actions artifacts. Even public code and metadata can contain secrets or personal
information. Keep feedback minimal and non-sensitive; do not share credentials, private source or
personal information. Research feedback is voluntary, not automatic usage telemetry. Operators
remain responsible for applicable data-protection requirements, organizational policies,
least-privilege credentials and spending controls. Review pinned releases and dependencies; a
matching hash establishes identity, not that a component is safe. Tell contributors that reports are
AI-assisted, partial and model-conditional, and avoid unfair blame or authoritative source-bug
claims. Before enabling automatic publication, consider whether tentative findings could expose a
security issue. Follow the repository's security-reporting guidance at
https://github.com/microsoft/neuro-formal-verification/blob/dev/SECURITY.md rather than posting
sensitive details in public issues or PRs. Once published, copies may persist even if a report is
hidden.

## License

NFV code is released under the MIT License; see
https://github.com/microsoft/neuro-formal-verification/blob/dev/LICENSE. The derived benchmark
retains its upstream MIT and Apache-2.0 notices under paper/licenses. Dataset provenance does not
create a replacement license, and the root code license does not grant blanket permission over all
inputs, third-party material or provider services. The incorporated source artifacts retain the
microsoft/intent-formalization MIT notice, its redistributed EvalPlus Apache-2.0 notice and the
HumanEval MIT notice. Preserve applicable copyright, license and attribution text when
redistributing derived content; see
https://github.com/microsoft/neuro-formal-verification/blob/dev/NOTICE.txt and
https://github.com/microsoft/neuro-formal-verification/blob/dev/paper/licenses. This note does not
assign a new blanket MIT license to all dataset content.

Nothing disclosed here, including the Out of Scope Uses section, should be interpreted as or deemed
a restriction or modification to the license the code is released under.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of
Microsoft trademarks or logos is subject to and must follow Microsoft's Trademark & Brand
Guidelines. Use of Microsoft trademarks or logos in modified versions of this project must not cause
confusion or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to
those third-party's policies.

## Ethics

The release reuses technical programming benchmarks and applies automated selection, screening and
sanitization. No new human-participant study or participant recruitment is documented in the
retained dataset-creation materials. No Institutional Review Board approval is asserted by this
note.

The retained materials do not document a new participant-consent process for NFV's dataset
derivation. Public availability of upstream artifacts does not establish informed consent for
arbitrary downstream uses or remove the need to comply with applicable rights and obligations.

No paid human annotation or participant-compensation process is documented for NFV's retained
automated derivation. This does not establish how all upstream contributors were recruited,
compensated or governed.

## Contact

This research was conducted by members of Microsoft Research. We welcome non-sensitive questions,
suggestions, collaboration and reports of unexpected behavior through the project's GitHub Issues:
https://github.com/microsoft/neuro-formal-verification/issues. Please include only authorized,
minimal examples and distinguish a model-level finding from a source-reproduced issue. Security
concerns must follow https://github.com/microsoft/neuro-formal-verification/blob/dev/SECURITY.md
rather than public feedback channels.

Feedback is intended to inform research priorities, prototype improvements and appropriate
documentation or mitigation updates. This is not a supported production service, and no
response-time, universal monitoring or continued-maintenance guarantee is made.
