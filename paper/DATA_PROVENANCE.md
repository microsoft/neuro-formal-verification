<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Dataset attribution and release provenance

The **nfv-micro-bench** benchmark is **derived data, not newly authored benchmark code**. Retain
this attribution and the original notices when redistributing it. This note
records observed upstream licensing; it does not invent a separate license for
the derived dataset or replace repository-owner/legal review.

## Source and license

All distributed benchmark content derives from
[microsoft/intent-formalization, nl2postcondition-fse2024](https://github.com/microsoft/intent-formalization/tree/main/nl2postcondition-fse2024):
its generated postconditions, code mutants and distinguishing inputs, together
with the HumanEval problems (via EvalPlus) that it already redistributes. We
ship that repository's notices unchanged in [`licenses`](licenses):

| File | Upstream file | License |
|---|---|---|
| `microsoft-intent-formalization-LICENSE` | [`LICENSE`](https://github.com/microsoft/intent-formalization/blob/main/LICENSE), Git blob `9e841e7a26e4eb057b24511e7b92d42b257a80e5` | MIT |
| `microsoft-intent-formalization-evalplus-LICENSE` | [`nl2postcondition-fse2024/nl2postcondition_source_evalplus/LICENSE`](https://github.com/microsoft/intent-formalization/blob/main/nl2postcondition-fse2024/nl2postcondition_source_evalplus/LICENSE), Git blob `eb587c5b6e4756f0b77f1b7bbd9d3b48dcbb7133` | Apache-2.0 |

The original problems are from [OpenAI HumanEval](https://github.com/openai/human-eval)
and [EvalPlus](https://github.com/evalplus/evalplus); we credit them here for
attribution. The HumanEval MIT notice is also retained in
[`licenses/openai-human-eval-LICENSE`](licenses/openai-human-eval-LICENSE).
The acquisition metadata does not record the upstream commit,
EvalPlus package version or downloaded cache digest.

Microsoft-authored reconstruction tooling carries copyright and MIT headers.
`manifest.json` records its release-file hashes separately from original source
hashes and Git blobs. These tooling notices do not relicense upstream benchmark
content; the dataset and retained results keep their frozen bytes.

The upstream research is **nl2postcond** (Endres et al., FSE 2024), as cited in
the paper's unmodified references. The acquisition recipe selects
`eval_results/gpt4_simple_no_ref.zip`, specifically the GPT-4
`SimplePromptNoRef` output, and `distinct_code_mutants.jsonl.zip`.
These archives are not bundled because the retained selected S1/S2 artifacts
are enough for downstream dataset reconstruction. Their blobs in the source
Git tree are, respectively:

* `002c8ef12ba190ae2544b1b7f04c57ac859b03cb` (12,196,391 bytes)
* `2a70e3e950ee22fe9d7a393f0dbde9918d706f24` (23,637,386 bytes)

The retained selected predecessors support deterministic downstream checks.
Repeating the original acquisition also requires the upstream inputs and
historical package versions.

## Frozen derivation

1. `build_nl2post_dataset.py` joins upstream postconditions/mutants and
   HumanEval problems, selecting 155 pairs/310 entries.
2. `validate_specs.py` used `claude-sonnet-4.5` and 100 tests per reference;
   the tracked validation summary retains 116 pairs/232 entries. Its
   generated test programs and legacy model adapter are not shipped.
   `verify_nl2post_dataset.py` records the distinguishing-input check.
3. `paper.data.build_safe` uniformly strips comments/docstrings/doctests from
   code and assertion messages/comments from specifications, and drops 13
   type-assertion-only pairs, yielding **103 pairs/206 entries**. Prose
   documentation remains a separate shared input for goal-blind precondition
   synthesis. It is not hidden in code.
4. The original pair identifiers and mutant response numbers are in
   `paper/data/MANIFEST.md`; file-level provenance is in `paper/manifest.json`.
   The optional pilot subset is retained only to check the documented
   deterministic derivation, not to replace the full evaluation denominator.

Reference/mutant labels are retained exactly, including disputed labels
discussed in the paper. We do not turn the paper's post-hoc attribution into
silent dataset relabeling. Killing tests are withheld from normal runs and
joined only for the explicitly labeled given-counterexample ablation.
