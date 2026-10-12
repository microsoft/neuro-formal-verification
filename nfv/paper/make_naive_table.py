"""Generate tables/naive.tex -- the motivating naive-baseline table.

The naive arm is the paper's argument for why the pipeline has stages, so the
table is generated from the run rather than typed: a hand-copied number here
would be a hand-copied number in the one place a reader checks first.

    python nfv/paper/make_naive_table.py
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results" / "naive_nogates.jsonl"
OUT = Path(__file__).resolve().parent / "tables" / "naive.tex"


def classify(rec: dict) -> str:
    """Which mechanism produced this unsound proof.

    Order matters. A self-referential goal is checked first because it makes the
    obligation vacuous no matter what the body does -- if the ensures is stated
    in terms of the model's own helpers, whether the body also fixed the bug is
    beside the point. `assume` is checked next as the only other mechanism that
    is visible in the text. What remains is the residue: the goal is honest, the
    proof rests on nothing assumed, and the obligation still holds -- which can
    only mean the body is not the program we were given.
    """
    d = rec.get("final_dafny") or rec.get("model_dafny") or ""
    helpers = set(re.findall(r"^\s*(?:function|predicate)\s+(?:method\s+)?(\w+)", d, re.M))
    ensures = " ".join(re.findall(r"ensures(.*)", d))
    if any(re.search(rf"\b{h}\b", ensures) for h in helpers):
        return "helper"
    if re.search(r"\bassume\b|\{:axiom\}", d):
        return "axiom"
    return "rewrite"


def main() -> int:
    recs = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    n_ref = sum(1 for r in recs if r["label"] == "correct")
    n_mut = len(recs) - n_ref
    c = Counter((r["label"], r["prediction"]) for r in recs)
    proved_ref, proved_mut = c[("correct", "correct")], c[("incorrect", "correct")]
    prec = 100.0 * proved_ref / (proved_ref + proved_mut)

    unsound = [r for r in recs if r["prediction"] == "correct" and r["label"] == "incorrect"]
    m = Counter(classify(r) for r in unsound)
    req = sum(1 for r in unsound
              if re.search(r"^\s*requires", r.get("final_dafny") or "", re.M))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    vac_path = ROOT / "results" / "naive_vacuity.json"
    vac_note = ""
    if vac_path.exists():
        v = json.loads(vac_path.read_text(encoding="utf-8"))
        reach = Counter((o["label"], o["reachable"]) for o in v)
        vac_note = (
            rf" Reachability probes (\texttt{{ensures false}} on a body-intact copy) "
            rf"find {reach[('correct', True)]} of the {proved_ref} reference proofs and "
            rf"{reach[('incorrect', True)]} of the {proved_mut} mutant proofs "
            rf"\emph{{reachable}}: only {reach[('correct', False)] + reach[('incorrect', False)]} "
            rf"are vacuous. The proofs have content --- what is wrong is what they are "
            rf"\emph{{about}}, not that they are empty.")

    OUT.write_text(rf"""\begin{{table}}[t]\centering\small
\caption{{The naive baseline: one two-line translation prompt, three repair
rounds, no library specifications, no blind preconditions, no provenance, no
freeze and no checks, on all {len(recs)} entries with the same model and verifier
as the staged pipeline. It proves {proved_ref} of {n_ref} correct programs --- far
above any staged configuration --- and {proved_mut} of {n_mut} known-wrong ones,
for a precision of {prec:.1f}\%, which is chance on a balanced benchmark. The
proof rate and the failure are the same number.{vac_note} Below, each unsound proof is
attributed to the mechanism that produced it and the stage that removes it;
a goal stated in terms of the model's own helpers is counted first, since it
makes the obligation vacuous regardless of what the body does.}}
\label{{tab:naive}}
\begin{{tabular}}{{lrr}}
\toprule
& Proved & Rate \\
\midrule
Correct programs ({n_ref}) & {proved_ref} & {100.0 * proved_ref / n_ref:.1f}\% \\
Known-wrong programs ({n_mut}) & {proved_mut} & {100.0 * proved_mut / n_mut:.1f}\% \\
\midrule
\multicolumn{{2}}{{l}}{{\emph{{Precision of a proof}}}} & \textbf{{{prec:.1f}\%}} \\
\bottomrule
\end{{tabular}}

\vspace{{1em}}
\begin{{tabular}}{{llr}}
\toprule
Mechanism & Stage that removes it & Count \\
\midrule
Program rewritten to match the goal & Phase~1 provenance, freeze (V3) & {m['rewrite']} \\
Goal restated via invented helpers & Phase~0 library specifications & {m['helper']} \\
Proof rests on an assumption & V1 constructs, V5 audit & {m['axiom']} \\
\midrule
\multicolumn{{2}}{{l}}{{\emph{{(also) unjustified \texttt{{requires}} present}}}} & {req} \\
\bottomrule
\end{{tabular}}
\end{{table}}
""", encoding="utf-8")

    print(f"wrote {OUT}")
    print(f"  refs {proved_ref}/{n_ref}  mutants {proved_mut}/{n_mut}  precision {prec:.1f}%")
    print(f"  mechanisms: {dict(m)}  requires: {req}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
