# Before/after: an NFV-found Z3 bug, refuted before its fix and not after

This report re-analyzes a Z3 bug that NFV found earlier, using the prototype workflow and a
live model. It analyzes the pre-fix code and the fixed code. **Nothing is handwritten.** The
properties, Dafny translations, witnesses and abstentions all come from one live `gpt-5.6-sol`
run. Dafny 4.11 decides every result.

## The bug

`sls::solver` is the SAT/SMT adapter for local search in Z3. Its best-phase accessors were stubs:

```cpp
void solver::set_has_new_best_phase(bool b) {
                                          // update discarded
}
bool solver::get_best_phase(sat::bool_var v) {
    return false;                         // ignores the SAT solver's stored phase
}
```

| Step | Link |
|---|---|
| NFV found it in a file-level formal audit (property `best_phase_lookup_returns_stored_phase`, witness `v = 0`, `best_phase = [true]`) | [Z3Prover/bench#4761](https://github.com/Z3Prover/bench/discussions/4761) |
| Triaged as an issue | Z3Prover/z3#10959 |
| Fixed by merged PR: both accessors forward to `ctx.s()` | Z3Prover/z3#10961 |

## What was run

The fix PR, z3#10961, is the analyzed merged pull request.

| Snapshot | Commit | Anchors |
|---|---|---|
| Pre-fix base | `d799f787d6fc9c16eb4a3ebbe63e6593e9b23a98` | removed lines 52 and 56 of `src/sat/smt/sls_solver.cpp` |
| Fixed head | `b31bd75d31d5f326d342fb29588637b81db6333a` (merge `7694e5c5564372242b15061ec72537fa93cc9e2a`) | added lines 52 and 56 |

1. `collect` captured the PR thread and the head source, with context ordered nearest-first.
2. `collect --side base` captured the pre-merge source. It uses the same context files, so the
   two snapshots differ only in the changed code.
3. `extract` (live model) read the PR thread and produced six intended properties
   ([properties-head.json](properties-head.json)). Their evidence quotes come from the earlier
   NFV audit comment on the PR, which restates the PR's intent ("Delegate `get_best_phase(v)` to
   the underlying SAT solver").
4. `reanchor` moved each claim to the removed lines of the same diff block. The property text
   and quotes are unchanged. The test-only property has no base counterpart and was dropped
   ([properties-base.json](properties-base.json)).
5. `run` (live model, 6 syntax / 10 proof repairs) ran on both snapshots with the same
   properties.

```text
nfv-workflow collect              --event event.json --out bundle-head.json
nfv-workflow collect --side base  --event event.json --out bundle-base.json
nfv-workflow extract  --bundle bundle-head.json --model gpt-5.6-sol --out properties-head.json
nfv-workflow reanchor --bundle bundle-base.json --properties properties-head.json --out properties-base.json
nfv-workflow run --bundle bundle-base.json --properties properties-base.json --model gpt-5.6-sol --out run-base
nfv-workflow run --bundle bundle-head.json --properties properties-head.json --model gpt-5.6-sol --out run-head
```

## Result

| Intended property | Pre-fix base | Fixed head |
|---|---|---|
| For a valid Boolean variable, the adapter returns the SAT solver's best phase | **refuted**: `S_pre = [true]`, `v = 0`; model returns `false` | not refuted; model: "no concrete input … falsifies the intended equality" |
| Setting the new-best-phase flag through the adapter updates the SAT solver | **refuted**: `S_pre = false`, `b = true`; flag stays `false` | not refuted; negative check failed after 10 proof repairs |
| False stored phase is returned as false | not refuted (10 proof repairs) | not refuted; model: "no concrete witness can violate" |
| Unset phase returns the SAT solver's default | abstained: missing context | abstained: missing context |
| Passing `false` clears a set flag | abstained: goal altered | abstained: missing wiring context |
| Test runs only without `SINGLE_THREAD` | not applicable: test added by the PR | abstained: fabricated provenance |

**Two refutations before the fix, none after.** They match the two accessors the PR changed.
The first refutation has the same witness as the original NFV audit. Each refutation took one
translation call and no repairs.

### Refutation 1: `get_best_phase` (pre-fix)

```dafny
// src: src/sat/sat_solver.h:382 | bool get_best_phase(bool_var b) { return m_best_phase.get(b, false); }
function best_phase_value(S_pre: seq<bool>, v: int): bool
  requires 0 <= v < |S_pre|
{
  S_pre[v]
}

// src: src/sat/smt/sls_solver.cpp:55 | bool solver::get_best_phase(sat::bool_var v) {
method WorkflowEntry(S_pre: seq<bool>, v: int) returns (result: bool)
  requires 0 <= v < |S_pre|
  ensures !((result == best_phase_value(S_pre, v)))      // negated intended property
  requires S_pre == [true]
  requires v == 0
{
// src: src/sat/smt/sls_solver.cpp:56 | return false;
  result := false;
}
```

Dafny proves the negated property on the witness (`4 verified, 0 errors`). The reachability
probe also passes: the witness satisfies the preconditions, so the body is not vacuously
skipped. Full program: [evidence/base/8135df5d3cb1c9000099/final/model.dfy](evidence/base/8135df5d3cb1c9000099/final/model.dfy).

### Refutation 2: `set_has_new_best_phase` (pre-fix)

With the flag initially `false` and `b = true`, the empty adapter body leaves the SAT flag
`false`. The SAT setter would make it `true`. See
[evidence/base/e888770583af5b79e2cb/final/model.dfy](evidence/base/e888770583af5b79e2cb/final/model.dfy).
One caveat: the line that reads the unchanged flag carries a loose provenance tag, the setter's
line rather than the empty body. The automated gates accepted it. The conclusion follows from
the empty body on line 52.

## Limits

- These are **conditional model refutations**. Source correspondence is "not established".
  The SAT solver state is abstracted as a sequence or flag, and no C++ was executed. The
  maintainers' regression test in z3#10961 is the executable confirmation.
- "Not refuted" on the fixed head is not a proof. The model either argued that no witness
  exists or used up its repair budget.
- The Discussion publisher rejects base-side snapshots. Before/after analysis is for
  reporting and validation only.

## Replay

The recorded live model responses are in [transcripts/](transcripts). The replay feeds them
back unchanged and reruns every gate and Dafny check, with no model calls:

```text
python examples/z3-10961/replay.py
```

Expected output: `base: 2 refutation(s)` and `head: 0 refutation(s)`. The bundles here are
excerpts. They keep the changed files and every file the recorded responses name; the full
collected bundles held 81–82 files.
