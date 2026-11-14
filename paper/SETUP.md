<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# Experiment setup

Return to [the dataset and main-run commands](README.md).

## Python, sign-in and model selection

From the repository root:

```powershell
python -m pip install -r requirements-paper.txt
python -m nfv.shared.config
python -m paper.prepare
```

The retained client requires `github-copilot-sdk==1.0.4`. Copilot CLI/SDK
authentication, model entitlement and historical model availability are
external prerequisites. Substituting another model creates a new experiment.
No credentials belong in commands, datasets or committed results.

For interactive sign-in, start `copilot`, enter `/login`, and follow the prompts.
For noninteractive use, supply a supported fine-grained token with the
**Copilot Requests** permission through `COPILOT_GITHUB_TOKEN`, preferably
injected from your CI secret store. Do not put token values in scripts or logs.
See [GitHub's authentication instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli).

Select an approved model available through your Copilot access with
**`--model MODEL`**. Historical model IDs are recorded in
[manifest.json](manifest.json). Keep new results separate and record the model ID:
substitution is a new experiment, not a reproduction of the original model run.

## Verifiers and Ubuntu 22.04

Use **Dafny 4.11.0 with Z3 4.12.1**. For Ubuntu 22.04, use the official
4.11.0 Ubuntu 22.04 archive, pinned in CI to SHA-256:

```text
a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179
```

Do not substitute older Dafny releases or packages built for a different host.
The historical experiments used Windows/MSVC; Ubuntu runs are
**new observations**, not a relabeling of the historical host. Timing and
proof-search behavior can differ by host.

CBMC **6.9.0** is locally validated and used by CI; the original paper log
does not pin its exact CBMC binary version. The official Ubuntu 22.04 package
used by CI has SHA-256:

```text
5194e76aeff7d4bccc2351abe4b9229b3fca97297ffe685c973211987386db19
```

CBMC needs a C preprocessor: GCC on Ubuntu or the retained MSVC setup on Windows.
Ubuntu inherits the ordinary compiler environment and does not probe Windows
`.exe` fallback paths.

| Setting | Purpose |
|---|---|
| `PATH` | Include `dafny` and `cbmc`. `~/.dotnet/tools/dafny` is also a fallback. |
| `NFV_Z3_PATH` | Absolute path to matching Z3 4.12.1; set before importing NFV. Otherwise discovery checks `~/z3/.../bin/z3`, then `PATH`. |
| `NFV_CBMC_EXE` | Optional absolute CBMC executable path. |
| `NFV_KEEP_SCRATCH` | `1` retains repository-local verifier scratch for diagnosis. Otherwise owned scratch is cleaned. |

There is **no `NFV_DAFNY_PATH` override**. Validate the installed binaries:

```bash
python -m nfv.shared.config
python -m nfv.backends.dafny --selftest
python -m nfv.backends.cbmc --selftest
python -m paper.smoke --output .nfv-work/ubuntu-paper-smoke.json
```

The backend APIs are SDK-independent. The workflow directly reuses
`nfv.backends.dafny.run_dafny(..., mode="unbounded")`; its optional proposer
uses `CopilotChat`. Workflow settings do not change historical paper budgets.

## Bounded new-model smoke

Unlike retained-artifact replay, this makes **new model requests**. After
setting up tools and your authenticated account, this Ubuntu command runs one
mutant with deliberately reduced smoke budgets, not the full paper budgets:

```bash
set -o pipefail
: "${NFV_Z3_PATH:?Set the matching Z3 4.12.1 executable}"
python -m paper.prepare
mkdir -p paper/new-results
python -m nfv.run --data paper/data/nl2post_safe.json \
  --backend dafny-theory-lemma4 --model MODEL \
  --phase refute --only HumanEval/157 --label incorrect --limit 1 \
  --attempts 1 --max-refutation-attempts 1 --max-syntax-rounds 1 \
  --max-proof-rounds 1 --concurrency 1 --llm-timeout 120 \
  --verify-timeout 30 --verify-budget 30 \
  --out paper/new-results/ubuntu_smoke_refute.jsonl --no-log \
  2>&1 | tee paper/new-results/ubuntu_smoke_refute.log
```

Choose fresh output/log paths. This permits at most five logical model requests;
the retained transport policy allows up to four API attempts per request.
Usage/account information is logged; abstention is a valid smoke outcome,
never a refutation. These instructions do not launch a campaign automatically.
