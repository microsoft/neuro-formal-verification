<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Run the paper experiments

These are the experiments of [*Neuro-Formal Verification: Agentic Language-Agnostic
Formal Program Reasoning*](https://arxiv.org/abs/2608.21516) (arXiv:2608.21516).

**Dataset: [`paper/data/nl2post_safe.json`](data/nl2post_safe.json)** — one
JSON object containing `entries`: **206 programs = 103 `correct` references +
103 `incorrect` mutants** (103 pairs). Each entry has `entry_id`, `task_id`,
`entry_point`, `label`, `code`, `postcondition`, `docstring`, and provenance
fields. Top-level counts are `total`, `num_correct`, and `num_incorrect`.
Labels are the original benchmark labels, not independently proved ground truth.

Example: the mutant of the pair for `HumanEval/97` (provenance fields omitted):

```json
{
  "entry_id": "HumanEval_97__r0__mutant",
  "task_id": "HumanEval/97",
  "entry_point": "multiply",
  "label": "incorrect",
  "code": "def multiply(a, b):\n    unit_a = a % 10\n    unit_b = b % 10\n    product = unit_a * unit_b\n    return product",
  "postcondition": "assert return_value == abs(a) % 10 * (abs(b) % 10)",
  "docstring": "Complete the function that takes two integers and returns \nthe product of their unit digits.\nAssume the input is always valid."
}
```

Its reference, `HumanEval_97__r0__reference` (label `correct`), has the same
postcondition and docstring, with code
`return int(str(a)[-1]) * int(str(b)[-1])`. The mutant breaks the postcondition
for negative inputs: Python's `-3 % 10` is `7`, not `3`.

## 1. Setup

Use Python 3.11+, `github-copilot-sdk==1.0.4`, an authenticated Copilot account
with access to your selected model, **Dafny 4.11.0 / Z3 4.12.1**, and CBMC with a C
preprocessor. CBMC 6.9.0 is validated; its exact historical version is unknown.
**Ubuntu 22.04 is supported for new runs.** See [SETUP.md](SETUP.md) for tool
installation, paths, sign-in and Ubuntu details.

From the repository root:

```powershell
python -m pip install -r requirements-paper.txt
python -m paper.prepare
```

Preparation materializes the frozen programs under `nfv/work`; it does not
make model calls.

## 2. Run the main experiments

The top-level runner is **`python -m nfv.run`**. These commands run all 206
entries with the paper budgets: **A5/R10, concurrency 24**.
Run each separately:

Replace **`MODEL`** with an approved model ID available through your Copilot
access. The historical model IDs are recorded in [manifest.json](manifest.json).
Keep new runs separate; changing models affects comparability, and historical
model-service snapshots are not guaranteed to remain available.

**Dafny verification**
```powershell
python -m nfv.run --data paper/data/nl2post_safe.json --backend dafny-theory-lemma4 --model MODEL --phase unbounded --attempts 5 --max-proof-rounds 10 --concurrency 24 --out paper/new-results/lemma4_verify.jsonl --no-log
```

**Dafny refutation**
```powershell
python -m nfv.run --data paper/data/nl2post_safe.json --backend dafny-theory-lemma4 --model MODEL --phase refute --attempts 5 --max-proof-rounds 10 --concurrency 24 --out paper/new-results/lemma4_refute.jsonl --no-log
```

**CBMC bounded refutation**
```powershell
python -m nfv.run --data paper/data/nl2post_safe.json --backend cbmc-theory --model MODEL --phase bounded --attempts 5 --max-proof-rounds 10 --concurrency 24 --llm-timeout 300 --entry-deadline 1800 --out paper/new-results/cbmc_theory.jsonl --no-log
```

Preserved defaults: three syntax-repair rounds, five refutation witness
attempts, unwind 10, verifier timeout 120 seconds, proof budget 60 seconds,
and Dafny LLM timeout 600 seconds. CBMC uses its final logged resume settings:
**300-second LLM timeout / 1,800-second entry deadline**, not a 300-second
verifier timeout. The workflow prototype's larger repair budgets do not apply here.

Use **fresh output files**: the store appends. Never overwrite archived
`paper/results`; use `--resume` only for an identical new configuration.

## 3. Quick smoke, separate from full experiments

**No inference:** replay two Dafny and two CBMC model artifacts from the original
raw records and compare their recorded verifier verdicts:

```powershell
python -m paper.smoke
```

All four matched in the [retained evidence](replayed/smoke.json). New evidence
goes to `.nfv-work/paper-smoke.json`; mismatches or unavailable tools exit 1.
Use `--backend dafny` or `--backend cbmc` for one backend.
This checks exact formal artifacts, not new translation or source/model
equivalence; historical admission gates are not rerun. CBMC bounded success
remains **abstain**. [Setup](SETUP.md#bounded-new-model-smoke) also provides a
separately budgeted, one-entry **new model** smoke.

## Notes

These experiments accompany [arXiv:2608.21516](https://arxiv.org/abs/2608.21516);
results remain attributed to its authors. Historical model-service snapshots
are unavailable, so identical new inference outcomes are not guaranteed.
The Nagini comparison is outside this release.

[manifest.json](manifest.json) records versions, budgets and file hashes.
Only host paths in diagnostic metadata have been anonymized; program/model
code, labels and verdicts are unchanged. Keep the
[dataset attribution](DATA_PROVENANCE.md) and [upstream license notices](licenses).
