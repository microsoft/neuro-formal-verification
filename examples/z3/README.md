# Z3 example: merged PR #11123

This example runs the prototype implementation's default general pipeline on a
real **merged** Z3 pull request. It is an offline replay: the translation is
handwritten instead of model-generated, but every gate and Dafny check is real.
No Discussion was created on Z3Prover/z3.

## The pull request

[Z3 PR #11123](https://github.com/Z3Prover/z3/pull/11123), "fix(seq): respect
known length lower bounds when minting fresh model values", was merged on
October 9, 2026, as `5353a8ead555533bb513467cb917bcaeae8901cd`. Its head is
`396ee5b75b74d96e0344aeaad665648507d597cc` and its base is
`5b01068df8fbce9772031a288a235e3f7090f01e`.

The commit message states the intended behavior:

> reuse the existing theory_seq::lower_bound2 helper to query any known length
> lower bound on the original term, and pad the fresh string value to meet it
> before it is used as a model value.

## What was run

1. **Collection (real, read-only, no model calls).** `nfv-workflow collect`
   ran on the merged-PR event. It captured the immutable head, the commit
   signal, and the changed lines of `src/smt/theory_seq.cpp`.
   `bundle.json` keeps that identity, signal, and those changed-line anchors.
   It reduces the source to an exact excerpt of `theory_seq::mk_value`
   (lines 2278–2326).
2. **Intended property.** `properties.json` formalizes the quote: when a
   positive length lower bound is known, the fresh string used as the model
   value is padded to at least that length.
3. **Translation (handwritten).** `translation.json` translates lines
   2308–2319, including the padding loop, into Dafny. Every computation line
   carries an exact `// src:` tag. Strings are modeled by their lengths. The
   results of `is_string` and `lower_bound2` are free inputs. Fresh values from
   `seq_factory::get_fresh_value` are plain ASCII (`!0!`, `!1!`, ...), so
   `encode()` does not change their length.
4. **Refutation checks (real Dafny).** The pipeline inverts the goal, pins the
   proposed inputs, and checks the negative model and the concrete call.
   It also checks that the model can return, plus integrity and provenance.

Replay it from the repository root after installing the root package, the
`workflow` package, and Dafny 4.11.0 (see [the workflow guide](../../workflow/README.md)):

```powershell
python examples\z3\replay.py
```

## Result

**One checked conditional model refutation, with zero repairs and zero
inference calls.**

The padding at lines 2311–2312 runs only when `lo.get_unsigned() <= max_pad`,
where `max_pad = 1 << 16` (65,536). For a known lower bound of **65,537** and
the 3-character placeholder `!0!`, the branch is skipped. The model value then
stays 3 characters long, shorter than the bound it was meant to satisfy.

* Negative model and concrete call: verified (`2 verified, 0 errors`).
* Returning-case probe: a normal return is possible, as required.
* Gates: integrity, goal, source freeze, conditions, witness consistency, and
  reachability all pass. Source correspondence is **not established**.

[`discussion-preview.md`](discussion-preview.md) shows what a developer would
see in the Discussion, which a live run labels `NFV-formal-review`. The [`evidence/`](evidence/) folder contains the Dafny
models, verifier logs, and `report.json`.

## How to read this

This is a **conditional model refutation, not a confirmed Z3 bug**. The cap
looks like a deliberate resource limit. However, neither the commit message nor
the code comment at lines 2302–2307 mentions it. For bounds above 65,536, the
model value can still contradict the derived length bound, which is the
inconsistency the PR set out to fix. A maintainer might document the cap, fall
back differently, or treat it as acceptable. The model does not cover exception
handling, memory limits, or the later `model_validate` check.

## Unmerged PRs are not analyzed

Iter 5 restricts the workflow to merged PRs. Running `nfv-workflow collect` on
the event for [Z3 PR #11007](https://github.com/Z3Prover/z3/pull/11007), which
was closed without merging, stops immediately:

```text
{"status": "error", "message": "Only merged pull requests are supported; closed without merging"}
```

In GitHub Actions, the activation job is also skipped for such PRs, so no agent
or model call runs.

The short Z3 source excerpt remains subject to
[Z3's source license](https://github.com/Z3Prover/z3/blob/396ee5b75b74d96e0344aeaad665648507d597cc/LICENSE.txt).
