# Paper v3 artifact (Dafny and CBMC)

Artifact for [Neuro-Formal Verification: Agentic Language-Agnostic Formal Program
Reasoning, arXiv:2608.21516v3](https://arxiv.org/abs/2608.21516v3).
The code and data were extracted from `shuvendu-lahiri/quasi-formal-verification`,
tag `neuro-formal-v1`, commit
`650a2ce0cffff9847ea4f4674e8a6e1168d172a1`.
Results remain attributed to that paper and its authors.

**The executable core, frozen dataset, and offline final-result reproduction
are runnable.** All 12 required final raw result files were recovered from the
author's local `informal-verification-paper` checkout after Git/remote searches
found none. They are copied unchanged, with hashes and provenance. No model
inference was used to create or validate this extraction. Historical stochastic
reruns and dirty code states remain distinct from recomputing recorded results.

## Layout and provenance

* `..\nfv\`: Dafny/CBMC engines, in-code prompts, staged translation, audits,
  proof/refutation, original judge baseline and metrics.
* `..\nfv\data\nl2post_safe.json`: unchanged 206 entries, 103 reference/mutant
  pairs; labels, specifications and documentation preserved.
* `..\nfv\results\`: 12 recovered final JSONL files, including generated formal
  artifacts embedded in the original records; no unrelated account logs.
* `data\upstream\`: tracked S1/S2 dataset predecessors, S2 validation summary,
  original construction scripts, and the **historical** plain-arm result file.
* `reported\`: unedited manuscript, architecture figure and submitted tables
  from the source's `nfv\paper\nfv_arxiv`. Checked against official v3 HTML;
  no assertion that this is byte-identical to the arXiv submission archive.
* `recomputed\`: 20 regenerated tables and machine-readable per-run metrics.
  Generated tables never overwrite `reported`.
* `manifest.json`: file-level source Git blobs/release hashes, exact logged
  commands, mixed/dirty commit history, generator inputs, tools and missing data.
* `licenses\`, `DATA_PROVENANCE.md`: original upstream notices and attribution.
* `extract_source.py`, `build_manifest.py`: maintainer provenance utilities.
  Extraction produces the pre-port source subset; it is **not** an in-place
  update command and would overwrite release portability changes.

Only `dafny`, `dafny-theory`, `dafny-theory-lemma4`, `dafny-naive`, `cbmc` and
`cbmc-theory` are registered. Base variants are shared dependencies; lemma-aware
Dafny, library-theory CBMC, and the unstaged Dafny baseline are required by v3.
The paper's Nagini comparison is explicitly outside this release scope.
No Boogie, Lean, Nagini or standalone SMT backend is included.

## Offline reproduction (no model or verifier calls)

From the repository root, using Python 3.10 or newer:

```powershell
python -m nfv.data.rebuild_dataset check
python -m nfv.data.build_safe --check
python -m nfv.data.verify_no_leak
python -m paper.reproduce
python -m paper.reproduce --require-complete
```

Both reproduction commands now exit **0** with the recovered results.
Without one of the required files, the default command reports the missing
inputs and regenerates what is available; `--require-complete` instead exits 1.

Available checks establish:

* S1: 310 entries/155 pairs; S2: 232/116; S3: 206/103.
* Sanitized entries rebuild exactly from retained S2; no comments/docstrings,
  doctests or collapsed pairs remain. Labels balance 103/103.
* All 103 oracle killing tests join to mutants only and remain absent from the
  ordinary dataset.
* Twenty tables regenerate: all 15 that have submitted counterparts match
  numeric tokens, seven match text exactly, and five are auxiliary analysis
  tables. Other text differences are generator comments or manuscript
  presentation changes; originals are not rewritten to conceal that drift.
* Recorded final outcomes reproduce: Dafny verification **72/77** correct
  claims, refutation **47/53**, the combined pipeline **118/128** over 206
  entries, CBMC **65/72**, and judge **148/206**. The oracle ablation yields
  **35/35** claims on 103 mutants. The unstaged baseline proves **101/103**
  references and **101/103** mutants, hence 50% precision.
* All eleven cause attributions in the retained analysis script execute
  successfully against the original records and benchmark.
* The only tracked run, root `a5r10_refute.jsonl`, recomputes to 206 records,
  49 correctly refuted mutants and 8 incorrectly refuted references. This is
  **not** the final lemma4 result (47 and 6).

The original `nfv.paper.*` generators remain callable. `paper.reproduce`
preflights their required files instead of generating misleading empty tables.
Results should be placed under `nfv\results` only if their provenance matches
the named run. Complete raw records are necessary for final table generation;
retained aggregate TeX is evidence of reported numbers, not a substitute for
the recovered raw observations.

## Running new experiments

Install `requirements-paper.txt` for the paper model client:

```powershell
python -m pip install -r requirements-paper.txt
python -m nfv.shared.config
python -m paper.prepare
```

The source client records `github-copilot-sdk==1.0.4`. A working GitHub Copilot
CLI/SDK login, suitable account entitlement and the requested model are external
prerequisites. Model availability can change; substituting another model is a
new experiment. No API credentials belong in the dataset or repository.

**Account selection is the developer's, not the author's.** Neither
`CopilotChat` nor the runners set an author login, author token or default
`NFV_EXPECTED_LOGIN`. They use the SDK's configured authentication and inspect
its account status. Set `NFV_EXPECTED_LOGIN` to **your own** expected login to
reject a different account; an explicit pin also aborts if identity cannot be
determined. This fail-closed handling of an unknown pinned identity is a release
guardrail, not a change to prompts, models or experiment budgets.

The historical EMU policy is retained: Enterprise Managed Users are rejected
unless explicitly permitted, because their available models and tenant
policies may differ. For `python -m nfv.run` and `python -m nfv.run_judge`, pass
`--allow-emu`. Those CLIs explicitly pass a boolean, so their default rejection
is not overridden by `NFV_ALLOW_EMU`. Direct `CopilotChat(...)` callers,
including the optional workflow proposer, instead honor `NFV_ALLOW_EMU=1`
when `allow_emu` is unspecified. Both paths honor `NFV_EXPECTED_LOGIN`;
there is no retained `--expected-login` CLI flag.

Install Dafny **4.11.0** and its matching Z3 **4.12.1**; use `NFV_Z3_PATH` if
discovery cannot locate it. CBMC **6.9.0** was discovered and self-tested
locally; the retained source does not explicitly pin the historical paper's
CBMC binary version. Set `NFV_CBMC_EXE` if needed.
CBMC also needs a C preprocessor (MSVC on Windows, a supported compiler on
other platforms). `python -m nfv.backends.dafny --selftest` and
`python -m nfv.backends.cbmc --selftest` exercise the real installed tools.
Scratch files are confined to the repository.

### Ubuntu experiment hosts

**Ubuntu 22.04 is a supported host for new experiments and offline analysis.**
Use the **Dafny 4.11.0 Ubuntu 22.04 distribution**, not the older Dafny 4.9.1
Ubuntu 20.04 package used by the reference z3-bench integration. The parent
Actions setup pins the 4.11.0 archive to SHA-256
`a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179`.
Changing host is not permission to change the paper's verifier or solver
versions.

The historical experiment log records Windows paths and MSVC behavior. The
recovered measurements remain those historical runs: an Ubuntu run is a **new,
separately recorded experiment**, not a retroactive claim that the original
results were obtained on Ubuntu. Timing, process behavior and proof-search
outcomes can vary with the host even at the same nominal tool versions.

Tool discovery is platform-aware:

| Setting | Ubuntu use |
|---|---|
| `PATH` | Include the directory containing executable `dafny`. There is no `NFV_DAFNY_PATH` override. `~/.dotnet/tools/dafny` is also a fallback. |
| `NFV_Z3_PATH` | Absolute path to the matching **Z3 4.12.1** executable supplied by tool setup. Set before importing NFV. Otherwise discovery checks `~/z3/.../bin/z3`, then `PATH`. |
| `NFV_CBMC_EXE` | Optional absolute CBMC executable path. Otherwise Ubuntu uses `cbmc` on `PATH`, without probing Windows `.exe` locations. |
| `NFV_EXPECTED_LOGIN` | Expected authenticated Copilot account; a mismatch aborts before inference. Credentials remain in the parent-managed authentication environment, not this variable. |
| `NFV_KEEP_SCRATCH` | Optional `1` to retain repository-local verifier scratch directories for diagnosis. |

Ubuntu CBMC inherits the ordinary compiler environment instead of requiring
MSVC; provision a supported C preprocessor. Linux discovery has dedicated
regression tests. Run the real verifier selftest in the Ubuntu job as well;
mocked discovery checks are not a substitute for executing the Linux binary.

After the parent-owned Actions tool/authentication setup, this **refute-only**
smoke is bounded independently of the full-paper budgets:

```bash
set -o pipefail
: "${NFV_Z3_PATH:?Set the matching Z3 4.12.1 executable}"
: "${NFV_EXPECTED_LOGIN:?Set the expected authenticated Copilot login}"
python -m nfv.shared.config
python -m nfv.backends.dafny --selftest
python -m paper.prepare
mkdir -p nfv/new-results
python -m nfv.run --backend dafny-theory-lemma4 --model gpt-5.6-sol \
  --phase refute --only HumanEval/157 --label incorrect --limit 1 \
  --attempts 1 --max-refutation-attempts 1 --max-syntax-rounds 1 \
  --max-proof-rounds 1 --concurrency 1 --llm-timeout 120 \
  --verify-timeout 30 --verify-budget 30 \
  --out nfv/new-results/ubuntu_smoke_refute.jsonl --no-log \
  2>&1 | tee nfv/new-results/ubuntu_smoke_refute.log
```

Use fresh smoke output paths. This allows at most five logical model requests;
the original transport policy allows up to four API attempts per request.
The log records the account and usage; the JSONL records per-entry usage.
An abstention is a valid smoke outcome and must not be relabeled as a refutation.
No Ubuntu workflow or model campaign is created by these checkout tools.

Examples of **new** runs matching the logged budget/configuration:
Use a fresh output file under `nfv\new-results`, never the recovered
`nfv\results` files. The original JSONL store appends rather than truncating;
`--resume` is appropriate only for the same new configuration.

```powershell
python -m nfv.run --backend dafny-theory-lemma4 --model gpt-5.6-sol --phase unbounded --attempts 5 --max-proof-rounds 10 --concurrency 24 --out nfv\new-results\lemma4_verify.jsonl --no-log
python -m nfv.run --backend dafny-theory-lemma4 --model gpt-5.6-sol --phase refute --attempts 5 --max-proof-rounds 10 --concurrency 24 --out nfv\new-results\lemma4_refute.jsonl --no-log
python -m nfv.run --backend dafny-theory-lemma4 --model gpt-5.6-sol --phase refute --oracle-witness --attempts 1 --max-proof-rounds 3 --concurrency 24 --out nfv\new-results\lemma4_oracle.jsonl --no-log
python -m nfv.run --backend dafny-naive --model gpt-5.6-sol --phase unbounded --attempts 1 --max-proof-rounds 3 --concurrency 24 --out nfv\new-results\naive_nogates.jsonl --no-log
python -m nfv.run_judge --model gpt-5.6-sol --out nfv\new-results\judge.jsonl
```

For CBMC and weak models use the exact command recorded per run in
`manifest.json`; CBMC's final logged resume explicitly used a 300-second LLM
timeout and an 1,800-second entry deadline (not a 300-second verifier timeout).
The final Sonnet refutation file was abandoned at **203**, not 206,
entries. Never complete its denominator by inventing abstentions.
The judge's invocation above is reconstructed from its CLI; its historical
command is missing from the experiment log.

Budgets are **not** today's CLI defaults: main/weak-model runs use A5/R10,
the given-counterexample ablation A1/R3, and the unstaged baseline A1/R3.
Refutation also has a separate five-witness-attempt default. Do not silently add
early-stop or newer combined-mode options; preserve the explicitly logged CBMC
deadline where applicable. The paper's combined table
joins two independent run files via `make_tasks_table.py`; it is not the newer
online combined-mode experiment (not extracted).

## Recovery provenance and remaining limitations

Git HEAD contains zero files under `nfv/results`; its fetched path history
contains none, and a direct GitHub contents query for the final verify file
returned 404. These checks accurately establish that the result files were
not in Git; they do not establish that no local copies existed.

An authenticated metadata-only search of the source repository's GitHub
Releases and Actions also returned **zero releases, zero artifacts, and zero
workflow runs**. There was therefore no archive candidate to download.
The endpoints, account, timestamp and responses are recorded in
`provenance\remote-artifacts.json` and the manifest.

The subsequent author-provided local checkout `informal-verification-paper`
contained all 12 final raw files. Its HEAD was the same pinned commit,
`neuro-formal-v1`, with no tracked changes. Only those exact JSONLs were copied,
byte-for-byte, without modifying the original checkout. File sizes, record
counts, model/backend metadata and SHA-256 hashes are recorded in
`provenance\local-results.json` and the manifest. The Sonnet refutation run
remains its original 203 records. Final models and histories are preserved to
the extent they are embedded in the result records; no missing intermediate
transcript has been invented.

Some runs were resumed across commits and some were logged with uncommitted
changes. The release SHA is not asserted to reproduce all those dirty states.
Individual run metadata in the manifest retains this distinction. Historical
model service snapshots are not pinned by a model name alone.

The dataset itself is fully usable. Re-creating *its original acquisition* is
a different task: S1 additionally needs the public archives and historical
EvalPlus cache/version; S2 used model-generated tests, whose summary but not
test programs is tracked. The archived `validate_specs.py` also depends on an
unbundled legacy `py2c.call_llm_async` adapter. Its original output is retained;
regenerating with a replacement client/model must not silently relabel it.

## Workflow integration and tests

The workflow directly reuses
`nfv.backends.dafny.run_dafny(source, mode="unbounded", timeout=60, workdir=...)`
and its result's `to_dict()` method. This backend is SDK-independent. The
optional proposer uses `nfv.shared.copilot_client.CopilotChat`.

The workflow owns its restricted typed JSON model and concrete-witness
negation harness. It does not invoke the paper's Python translator on C++,
claim source-to-model equivalence is proved, or rely on a newly introduced
general-purpose adapter. See `..\workflow\README.md` for the separate workflow
checks; they do not change historical paper scoring.

```powershell
python -m unittest discover -s tests -p test_paper_core.py -v
```
