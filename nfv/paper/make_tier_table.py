"""Per-tier breakdown of the three final runs -- verification, refutation, and
refutation-given-C.

The library tier is computed from the Python source alone (nfv/data/build_subset.py)
and counts what the model is forced to axiomatise, so it is a proxy for how much
of the environment the model must specify rather than for algorithmic difficulty.
Breaking each run down by tier is the only way to see whether the pipeline's
yield falls where the model must write more of that environment itself, which is
the claim the tiers exist to test.

Each run is scored on what it CLAIMS versus the ground-truth label, never on the
proof rate alone: a claim that contradicts the label is counted and reported in
the same table, because the naive baseline showed a proof rate on its own is
uninformative.
"""
from __future__ import annotations

import json
from pathlib import Path

from nfv.data.build_subset import _calls_used, higher_order_tasks, tier_label

TIERS = ("0", "1", "2a", "2b", "3")

ROOT = Path(__file__).resolve().parents[2]
TABLES = Path(__file__).resolve().parent / "tables"

VERIFY = "lemma4_verify.jsonl"
REFUTE = "lemma4_refute.jsonl"
GIVENC = "lemma4_oracle.jsonl"

# Tasks whose specification omits a precondition, established case by case in
# the Analysis section by reasoning about the program and the specification on
# the domain the proof covers -- never by re-scoring against the pipeline's own
# output.  Under the policy fixed there, a reference keeps its `correct` label
# and the omission is charged to the specification, so only the mutant entry
# below changes class.
#
# This table reports no corrected precision -- every figure in it is scored
# against the labels as published.  The sets are kept only as staleness
# assertions: main() fails loudly if the attribution in the Analysis section
# ever stops matching the runs.  nfv/paper/check_causes.py executes the
# evidence for each.
FLIPPED_VERIFY = {"HumanEval/116"}  # docstring excludes the only distinguishing input
# References whose refutation is sound but exposes a precondition the
# specification never stated: 11 equal-length operands; 20 the absence of NaN;
# 64 an ASCII argument.
FLIPPED_REFUTE = {"HumanEval/11", "HumanEval/20", "HumanEval/64"}


def load(name: str) -> list[dict]:
    p = ROOT / "nfv/results" / name
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.0f}\\%" if b else "--"


def prec(good: int, bad: int) -> str:
    """Precision over claims: of everything the arm asserted, how much agrees
    with the ground-truth label.  Abstentions are excluded -- they assert
    nothing -- so this is not the same question as the yield beside it."""
    n = good + bad
    return f"{100.0 * good / n:.0f}\\%" if n else "--"


def verify_cells(rows: list[dict]) -> tuple[int, int, int, int]:
    """(correct proved, correct programs, bad claims, bad claims on flipped labels)."""
    refs = [r for r in rows if r.get("label") == "correct"]
    muts = [r for r in rows if r.get("label") == "incorrect"]
    proved = sum(1 for r in refs if r.get("prediction") == "correct")
    bad = [r for r in muts if r.get("prediction") == "correct"]
    flipped = sum(1 for r in bad if r.get("task_id") in FLIPPED_VERIFY)
    return proved, len(refs), len(bad), flipped


def refute_cells(rows: list[dict]) -> tuple[int, int, int, int]:
    """(buggy refuted, buggy programs, bad claims, bad claims on flipped labels)."""
    refs = [r for r in rows if r.get("label") == "correct"]
    muts = [r for r in rows if r.get("label") == "incorrect"]
    refuted = sum(1 for r in muts if r.get("prediction") == "incorrect")
    bad = [r for r in refs if r.get("prediction") == "incorrect"]
    flipped = sum(1 for r in bad if r.get("task_id") in FLIPPED_REFUTE)
    return refuted, len(muts), len(bad), flipped


def main() -> int:
    hof = higher_order_tasks()
    tier_of = {t: tier_label(t, c, hof) for t, c in _calls_used().items()}
    ver, ref = load(VERIFY), load(REFUTE)

    # A label correction only means something if the entry actually made a
    # contradicting claim in THIS run. When the reference run changes, a
    # correction carried over from an older one silently does nothing, so fail
    # loudly instead of quietly reporting an uncorrected precision.
    v_bad = {x["task_id"] for x in ver
             if x.get("label") != "correct" and x["prediction"] == "correct"}
    r_bad = {x["task_id"] for x in ref
             if x.get("label") == "correct" and x["prediction"] == "incorrect"}
    assert FLIPPED_VERIFY <= v_bad, f"stale verify corrections: {FLIPPED_VERIFY - v_bad}"
    assert FLIPPED_REFUTE <= r_bad, f"stale refute corrections: {FLIPPED_REFUTE - r_bad}"

    body = []
    tot = [0] * 8
    for k in TIERS:
        sel = lambda rows: [x for x in rows if tier_of.get(x.get("task_id")) == k]
        vp, vn, vb, vf = verify_cells(sel(ver))
        rp, rn, rb, rf_ = refute_cells(sel(ref))
        for i, v in enumerate((vp, vn, rp, rn, vb, rb, vf, rf_)):
            tot[i] += v
        body.append(
            rf"L{k} & {vp}/{vn} ({pct(vp, vn)}) & {vb} & {prec(vp, vb)} & "
            rf"{rp}/{rn} ({pct(rp, rn)}) & {rb} & {prec(rp, rb)} \\"
        )
    body.append(r"\midrule")
    body.append(
        rf"\textbf{{All}} & \textbf{{{tot[0]}/{tot[1]}}} ({pct(tot[0], tot[1])}) & "
        rf"{tot[4]} & \textbf{{{prec(tot[0], tot[4])}}} & "
        rf"\textbf{{{tot[2]}/{tot[3]}}} ({pct(tot[2], tot[3])}) & "
        rf"{tot[5]} & \textbf{{{prec(tot[2], tot[5])}}} \\"
    )

    out = rf"""% generated by nfv/paper/make_tier_table.py -- do not edit by hand
\begin{{table}}[t]\centering\footnotesize
\caption{{The two tasks by library tier, one run each.}}
\label{{tab:tiers-final}}
\begin{{tabular}}{{lrrrrrr}}
\toprule
& \multicolumn{{3}}{{c}}{{verification}} & \multicolumn{{3}}{{c}}{{refutation}} \\
\cmidrule(lr){{2-4}}\cmidrule(lr){{5-7}}
Tier & proved & \emph{{bad}} & \emph{{prec}} &
refuted & \emph{{bad}} & \emph{{prec}} \\
\midrule
{chr(10).join(body)}
\bottomrule
\end{{tabular}}
\end{{table}}
"""
    TABLES.mkdir(parents=True, exist_ok=True)
    (TABLES / "tiersfinal.tex").write_text(out, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
