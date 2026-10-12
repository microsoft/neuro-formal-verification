"""Emit tables/tasks.tex: precision, recall and F1 for each task.

The paper defines two tasks -- does P satisfy S, does P fail S -- plus the
pipeline that composes them. They are not
interchangeable and they are not scored on the same denominator, so this table
states each one's population explicitly rather than leaving the reader to infer
it:

  verification            scored over the 103 correct references
  refutation              scored over the 103 incorrect mutants
  pipeline                all 206 entries, one verdict per entry

Refutation given a killing input C is an ablation rather than one of the tasks
the paper reports, and it is confined to its appendix: it runs on a different
population (mutants only), at a different proof budget, so a row here would not
be comparable with the rows around it.

Precision is correct verdicts over verdicts issued, recall is verdicts issued
over the population, and abstentions are never counted as verdicts. Every number
is read from a run file; none is typed by hand.

    python nfv/paper/make_tasks_table.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "nfv" / "results"
OUT = ROOT / "nfv" / "paper" / "tables" / "tasks.tex"
MACROS = ROOT / "nfv" / "paper" / "tables" / "pipeline_numbers.tex"

VERIFY = "lemma4_verify.jsonl"
REFUTE = "lemma4_refute.jsonl"
ORACLE = "lemma4_oracle.jsonl"


def load(name: str) -> list[dict]:
    p = RESULTS / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def prf(hits: int, issued: int, pop: int) -> tuple[str, str, str]:
    p = hits / issued if issued else 0.0
    r = hits / pop if pop else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return f"{100*p:.1f}", f"{100*r:.1f}", f"{f:.2f}"


def main() -> int:
    v = load(VERIFY)
    refs = [x for x in v if x.get("label") == "correct"]
    muts = [x for x in v if x.get("label") != "correct"]
    v_hits = sum(1 for x in refs if x["prediction"] == "correct")
    v_bad = sum(1 for x in muts if x["prediction"] == "correct")

    r = load(REFUTE)
    r_refs = [x for x in r if x.get("label") == "correct"]
    r_muts = [x for x in r if x.get("label") != "correct"]
    r_hits = sum(1 for x in r_muts if x["prediction"] == "incorrect")
    r_bad = sum(1 for x in r_refs if x["prediction"] == "incorrect")

    o = load(ORACLE)
    o_hits = sum(1 for x in o if x["prediction"] == "incorrect")
    o_bad = sum(1 for x in o if x["prediction"] == "correct")

    # The pipeline is refutation first, then verification on whatever refutation
    # left open. It is COMPOSED from the two runs above rather than read from a
    # separate combined run, so that every row of this table is the same backend
    # at the same budget. The earlier combined run on file is a different arm
    # (`dafny-theory-order`), which would put a second variable in the table.
    vd = {x["entry_id"]: x for x in v}
    rd = {x["entry_id"]: x for x in r}
    assert set(vd) == set(rd), "the two arms must cover the same entries"
    c_hits = c_issued = 0
    for i, x in vd.items():
        if rd[i]["prediction"] == "incorrect":
            pred = "incorrect"
        elif x["prediction"] == "correct":
            pred = "correct"
        else:
            pred = "abstain"
        if pred != "abstain":
            c_issued += 1
            if (pred == "correct") == (x.get("label") == "correct"):
                c_hits += 1
    c = vd

    # Union of the two refutation arms: the same mutant refuted by either. Kept
    # for the appendix's report of the ablation; the main table no longer states
    # it, because a union over two arms at different proof budgets is not a
    # result the pipeline can deliver -- nothing chooses between the arms at run
    # time, so the union is an upper bound nobody can cash.
    o_ids = {x["entry_id"] for x in o if x["prediction"] == "incorrect"}
    r_ids = {x["entry_id"] for x in r_muts if x["prediction"] == "incorrect"}
    union = len(o_ids | r_ids)

    lines = [
        r"\begin{table}[t]\centering\small",
        r"\caption{\daf{}, the reference backend, on each task and its own"
        r" population. Verification and refutation run at 5 attempts $\times$ 10"
        r" proof rounds. The pipeline row is composed from the two arms above ---"
        r" refutation first, verification on what it leaves open --- so every row"
        r" is the same backend. ``wrong'' counts"
        r" verdicts contradicting the label.}",
        r"\label{tab:tasks}",
        r"\begin{tabular}{lrrrrrr}",
        r"\toprule",
        r"task & population & issued & wrong & prec.\ (\%) & rec.\ (\%) & $F_1$\\",
        r"\midrule",
    ]

    def row(name, hits, bad, pop):
        p, rr, f = prf(hits, hits + bad, pop)
        lines.append(f"{name} & {pop} & {hits + bad} & {bad} & {p} & {rr} & {f}\\\\")

    row(r"verification ($P \models S$)", v_hits, v_bad, len(refs))
    row(r"refutation ($P \not\models S$)", r_hits, r_bad, len(r_muts))
    lines.append(r"\midrule")
    row(r"pipeline (refute, then verify)", c_hits, c_issued - c_hits, len(c))
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")

    # The conclusion states the pipeline row in prose, so emit it as macros from
    # the same counts the row is built from rather than letting the abstract and
    # the table drift apart.
    p, rr, _ = prf(c_hits, c_issued, len(c))
    MACROS.write_text(
        "\n".join([
            rf"\newcommand{{\anpipepop}}{{{len(c)}}}",
            rf"\newcommand{{\anpipeissued}}{{{c_issued}}}",
            rf"\newcommand{{\anpipegood}}{{{c_hits}}}",
            rf"\newcommand{{\anpipewrong}}{{{c_issued - c_hits}}}",
            rf"\newcommand{{\anpipeprec}}{{{float(p):.0f}}}",
            rf"\newcommand{{\anpiperec}}{{{float(rr):.0f}}}",
            rf"\newcommand{{\anpipeopen}}{{{100 * (len(c) - c_hits) / len(c):.0f}}}",
        ]) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {MACROS}")
    print(f"  verification {v_hits}/{v_hits + v_bad} over {len(refs)}")
    print(f"  refutation   {r_hits}/{r_hits + r_bad} over {len(r_muts)}")
    print(f"  given-C      {o_hits}/{o_hits + o_bad} over {len(o)}")
    print(f"  pipeline     {c_hits}/{c_issued} over {len(c)}")
    print(f"  union of refutation arms: {union}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
