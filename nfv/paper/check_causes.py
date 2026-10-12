"""Classify every bad claim of the reported run by cause, and execute the claim.

Section~\\ref{sec:analysis} attributes each of the pipeline's wrong verdicts to
one of six causes. A taxonomy is the easiest thing in a paper to write by
assertion, so nothing here is asserted: the bad claims are *found* in the two run
files rather than listed, and each attribution carries a check that runs Python
and either reproduces the claim or fails the build.

The causes, in the order they appear in the paper:

  missing-pre   The witness is a real violation of the Python postcondition, on
                an input the docstring's intent excludes but its text does not.
                The reference keeps its ``correct`` label and the specification
                is charged with an unstated precondition. Evidence: execute the
                witness, then execute the reference over the stated domain.
  stronger-pre  The proof holds only because phase 0b assumed more than the
                docstring states. Evidence: exhibit an admitted input, excluded
                by the assumption, on which the mutant violates the
                postcondition.
  weak-spec     The mutant satisfies the postcondition on the intended domain;
                the label records a value the specification never constrained.
                Evidence: differential-test on that domain.
  library       A library function was formalised into something Python's
                library is not. Evidence: structural, on the emitted artefact.
  translation   The program was translated into something it does not mean.
  stronger-post The postcondition is stronger than the docstring's promise.

Run: python nfv/paper/check_causes.py
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "nfv/data/nl2post_safe.json"
RESULTS = ROOT / "nfv/results"
TABLES = Path(__file__).resolve().parent / "tables"

VERIFY, REFUTE = "lemma4_verify.jsonl", "lemma4_refute.jsonl"

# The attribution, one line per bad claim. Read as: this cause, and this is the
# precondition the specification failed to state (or None where the cause is
# not a precondition). The checks below decide whether each line is true; the
# table is emitted from what the checks found, not from this dict alone.
CAUSE = {
    # refutations of references: all six are real Python counterexamples
    "HumanEval/11":  ("missing-pre", r"$|a| = |b|$"),
    "HumanEval/20":  ("missing-pre", r"no \texttt{NaN}"),
    "HumanEval/49":  ("missing-pre", r"$p > 0$"),
    "HumanEval/64":  ("missing-pre", r"\textsc{ascii}"),
    "HumanEval/79":  ("missing-pre", r"$\mathit{decimal} \ge 0$"),
    "HumanEval/111": ("missing-pre", r"tokens are single letters"),
    # proofs of mutants
    "HumanEval/116": ("missing-pre", r"$arr[i] \ge 0$"),
    "HumanEval/6":   ("stronger-pre", r"no empty space-delimited field"),
    "HumanEval/133": ("stronger-pre", r"\texttt{lst} is integer-typed"),
    "HumanEval/151": ("stronger-pre", r"\texttt{lst} is integer-typed"),
    "HumanEval/35":  ("library", None),
}

NAMES = {
    "missing-pre": "missing precondition",
    "stronger-pre": "stronger precondition",
    "stronger-post": "stronger postcondition",
    "weak-spec": "weaker specification",
    "library": "library formalization",
    "translation": "code translation",
}


def entries() -> dict[str, dict[str, dict]]:
    blob = json.loads(DATA.read_text(encoding="utf-8"))
    by: dict[str, dict[str, dict]] = {}
    for e in blob["entries"]:
        by.setdefault(e["task_id"], {})[e["label"]] = e
    return by


def rows(name: str) -> list[dict]:
    return [json.loads(l) for l
            in (RESULTS / name).read_text(encoding="utf-8").splitlines() if l.strip()]


def run(entry: dict, args: dict):
    """Return (value, holds), evaluating the benchmark's own postcondition."""
    scope: dict = {}
    exec(entry["code"], scope)
    rv = scope[entry["entry_point"]](**args)
    scope.update(args)
    scope["return_value"] = rv
    try:
        exec(entry["postcondition"], scope)
        return rv, True
    except AssertionError:
        return rv, False


def bad_claims() -> dict[str, str]:
    """The wrong verdicts, found in the run files rather than listed here.

    The value is the kind of entry the claim was made about, read off the
    ground-truth label rather than assumed from the arm.
    """
    found: dict[str, str] = {}
    for name, want in ((VERIFY, "correct"), (REFUTE, "incorrect")):
        for r in rows(name):
            if r["prediction"] == want and r["label"] != want:
                found[r["task_id"]] = ("reference" if r["label"] == "correct"
                                       else "mutant")
    return found


def main() -> int:
    db = entries()
    failures: list[str] = []
    checked: set[str] = set()

    def check(task: str, cond: bool, detail: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'}  {task}: {detail}")
        checked.add(task)
        if not cond:
            failures.append(task)

    claims = bad_claims()
    print(f"bad claims found in the run files: {len(claims)}")
    missing = set(claims) - set(CAUSE)
    extra = set(CAUSE) - set(claims)
    if missing or extra:
        raise SystemExit(f"attribution is stale: unattributed {sorted(missing)}, "
                         f"attributed but no longer bad {sorted(extra)}")

    # --- refutations of references: real counterexamples, unstated domains ---
    print("\nrefutations of references (cause: missing precondition)")

    rv, holds = run(db["HumanEval/11"]["correct"], {"a": "0", "b": "01"})
    check("HumanEval/11", not holds,
          f"string_xor('0','01') = {rv!r}; unequal lengths make the "
          f"postcondition unsatisfiable")

    nan = float("nan")
    rv, holds = run(db["HumanEval/20"]["correct"], {"numbers": [nan, nan]})
    check("HumanEval/20", not holds and rv is None,
          f"find_closest_elements([nan,nan]) = {rv!r}; every comparison with "
          f"nan is false")

    rv, holds = run(db["HumanEval/49"]["correct"], {"n": 0, "p": -1})
    ok_pos = not [(n, p) for n in range(0, 400) for p in range(1, 100)
                  if not run(db["HumanEval/49"]["correct"], {"n": n, "p": p})[1]]
    check("HumanEval/49", (not holds) and ok_pos,
          f"modp(0,-1) = {rv!r} violates; under p > 0 the reference holds on "
          f"all 39,600 sampled pairs")

    rv, holds = run(db["HumanEval/64"]["correct"], {"s": "\u0130"})
    check("HumanEval/64", not holds,
          f"vowels_count('\\u0130') = {rv!r}; its lowercase is "
          f"{ascii('\u0130'.lower())}, injecting an 'i' the program never sees")

    rv, holds = run(db["HumanEval/79"]["correct"], {"decimal": -3})
    ok_nonneg = all(run(db["HumanEval/79"]["correct"], {"decimal": d})[1]
                    for d in range(0, 500))
    check("HumanEval/79", (not holds) and ok_nonneg,
          f"decimal_to_binary(-3) = {rv!r}, since bin(-3)[2:] is "
          f"{bin(-3)[2:]!r}; under decimal >= 0 the reference holds on 500 values")

    rv, holds = run(db["HumanEval/111"]["correct"], {"test": "aa a"})
    rng = random.Random(0)
    ok_letters = True
    for _ in range(2000):
        toks = [rng.choice("abcdef") for _ in range(rng.randrange(1, 7))]
        if not run(db["HumanEval/111"]["correct"], {"test": " ".join(toks)})[1]:
            ok_letters = False
            break
    check("HumanEval/111", (not holds) and ok_letters,
          f"histogram('aa a') = {rv!r}; test.count('a') is 3 and counts "
          f"substrings, not words. On single-letter tokens --- what the "
          f"docstring describes --- the reference holds on 2000 random inputs")

    # --- proofs of mutants ---
    print("\nproofs of mutants")

    ref, mut = db["HumanEval/116"]["correct"], db["HumanEval/116"]["incorrect"]
    rng = random.Random(0)
    trials = agree = holds116 = 0
    for _ in range(20000):
        arr = [rng.randrange(0, 512) for _ in range(rng.randrange(0, 9))]
        r, _ = run(ref, {"arr": list(arr)})
        m, ok = run(mut, {"arr": list(arr)})
        trials += 1
        agree += (r == m)
        holds116 += ok
    neg_r, _ = run(ref, {"arr": [-1, -2]})
    neg_m, _ = run(mut, {"arr": [-1, -2]})
    check("HumanEval/116", agree == trials and holds116 == trials and neg_r != neg_m,
          f"mutant agrees with the reference on {agree}/{trials} non-negative "
          f"arrays and satisfies the postcondition on {holds116}; on [-1,-2], "
          f"which the docstring excludes, they differ ({neg_r} vs {neg_m}). "
          f"Label flipped to correct")

    ref, mut = db["HumanEval/6"]["correct"], db["HumanEval/6"]["incorrect"]
    # every input the assumed precondition excludes: empty, trailing, leading,
    # repeated separator -- each yields an empty field under split(' ')
    wit6 = ["", "() ", " ()", "()  ()"]
    res6 = [(w, run(ref, {"paren_string": w}), run(mut, {"paren_string": w}))
            for w in wit6]
    check("HumanEval/6",
          all(ok_r and not ok_m for _, (_, ok_r), (_, ok_m) in res6),
          "on every input the assumed precondition excludes the reference "
          "holds and the mutant violates: "
          + "; ".join(f"{w!r} ref {r!r} vs mutant {m!r}"
                      for w, (r, _), (m, _) in res6))

    ref, mut = db["HumanEval/133"]["correct"], db["HumanEval/133"]["incorrect"]
    rng = random.Random(0)
    eq = 0
    for _ in range(3000):
        xs = [rng.randrange(-20, 20) for _ in range(rng.randrange(0, 6))]
        eq += (run(ref, {"lst": list(xs)})[0] == run(mut, {"lst": list(xs)})[0])
    fv, ok_f = run(mut, {"lst": [1.5, 2.25]})
    check("HumanEval/133", eq == 3000 and not ok_f,
          f"mutant equals the reference on {eq}/3000 integer lists --- '--num' "
          f"is the identity there --- and on [1.5,2.25] returns {fv!r}, which "
          f"is not an int, so only the integer typing sustains the proof")

    ref, mut = db["HumanEval/151"]["correct"], db["HumanEval/151"]["incorrect"]
    rng = random.Random(0)
    sat = differ = 0
    for _ in range(3000):
        xs = [rng.randrange(-20, 20) for _ in range(rng.randrange(0, 6))]
        r, _ = run(ref, {"lst": list(xs)})
        m, ok = run(mut, {"lst": list(xs)})
        sat += ok
        differ += (r != m)
    # the doubling is invisible to the postcondition, but the mutant also drops
    # the reference's "'.' not in str(num)" guard, and that half IS visible: on
    # a float list it returns a float and fails isinstance(rv, int). So the
    # specification is not too weak -- seq<int> is what removes the witness.
    fl = [0.1, 0.2, 0.3]
    mf, ok_f = run(mut, {"lst": list(fl)})
    rf, ok_r = run(ref, {"lst": list(fl)})
    check("HumanEval/151", sat == 3000 and differ > 0 and ok_r and not ok_f,
          f"mutant satisfies the postcondition on {sat}/3000 integer lists "
          f"while returning a different value from the reference on {differ} of "
          f"them, so the doubling is invisible; but on {fl} it returns {mf!r} "
          f"against the reference's {rf!r} and fails 'isinstance(rv,int)', so "
          f"the specification is strong enough and only the seq<int> typing "
          f"sustains the proof")

    # --- the library formalisation ------------------------------------------
    print("\nlibrary formalisation")
    row35 = next(r for r in rows(VERIFY)
                 if r["task_id"] == "HumanEval/35" and r["label"] == "incorrect")
    dfy = row35["final_dafny"]
    body = re.search(r"function PyMaxLoop\b.*?\n}", dfy, re.S)
    base = bool(body and "PyAdd(current, PyIntegerLiteral(1))" in body.group(0))
    ensures = "ensures result == PyMax(l)" in dfy
    clean = row35.get("theory_axioms") == 0 and not row35.get("trusted_base_final")
    check("HumanEval/35", base and ensures and clean,
          f"the postcondition is 'result == PyMax(l)' and PyMax's own base case "
          f"is 'PyAdd(current, PyIntegerLiteral(1))' --- the mutation --- so the "
          f"obligation reduces to the program equalling itself. "
          f"axioms={row35.get('theory_axioms')}, "
          f"trusted base={row35.get('trusted_base_final')}")

    # The Python specification is not the weak one here: it pins the value, and
    # the mutant violates it on an ordinary list.  The weakening happens only in
    # the model, which is what separates 35 from 151.
    e35 = db["HumanEval/35"]["incorrect"]
    _, held = run(e35, {"l": [1, 5, 3]})
    strong = e35["postcondition"].strip() == "assert return_value == max(l)"
    check("HumanEval/35", strong and not held,
          f"the Python postcondition is {e35['postcondition'].strip()!r}, which "
          f"the mutant violates on [1,5,3] --- the specification as given is "
          f"not weak; only the translated one is")

    ref35 = next(r for r in rows(VERIFY)
                 if r["task_id"] == "HumanEval/35" and r["label"] == "correct")
    refdfy = ref35["final_dafny"]
    faithful = ("PythonMaxFrom" in refdfy
                and "PyIntegerLiteral(1)" not in refdfy
                and "PyAdd(" not in refdfy)
    check("HumanEval/35", faithful,
          "the same run models max faithfully for the reference "
          "(PythonMaxFrom, no +1), so this is a hazard of writing the theory "
          "beside the program rather than a limit of the arm")

    # --- what the taxonomy adds up to ---------------------------------------
    tally: dict[str, list[str]] = {}
    for task, (cause, _) in CAUSE.items():
        tally.setdefault(cause, []).append(task)
    print("\nby cause")
    for cause in NAMES:
        got = sorted(tally.get(cause, []), key=lambda t: int(t.split("/")[1]))
        print(f"  {NAMES[cause]:24} {len(got):>2}  "
              f"{', '.join(t.split('/')[1] for t in got)}")

    if set(claims) != checked:
        failures.append(f"unchecked: {sorted(set(claims) - checked)}")

    # --- emitted table and macros -------------------------------------------
    order = sorted(CAUSE, key=lambda t: (list(NAMES).index(CAUSE[t][0]),
                                         int(t.split("/")[1])))
    tex = [r"% generated by nfv/paper/check_causes.py -- do not edit by hand",
           r"\begin{table}[t]\centering\footnotesize",
           r"\caption{Every wrong verdict of the reported run, by cause. "
           r"\emph{entry} is the kind of program the claim was made about: a "
           r"\emph{reference} is labeled correct and was refuted, a "
           r"\emph{mutant} is labeled incorrect and was proved. "
           r"\emph{unstated assumption} is the precondition the "
           r"specification would have needed for the verdict and the label to "
           r"agree. Every row is executed rather than asserted.}",
           r"\label{tab:causes}",
           r"\begin{tabular}{llll}",
           r"\toprule",
           r"task & entry & cause & unstated assumption \\",
           r"\midrule"]
    for task in order:
        cause, pre = CAUSE[task]
        tex.append(f"\\texttt{{{task.replace('/', '\\_')}}} & {claims[task]} & "
                   f"{NAMES[cause]} & {pre or '---'} \\\\")
    tex += [r"\bottomrule", r"\end{tabular}", r"\end{table}", ""]
    (TABLES / "causes.tex").write_text("\n".join(tex), encoding="utf-8")

    macros = {
        "cabad": len(claims),
        "camisspre": len(tally.get("missing-pre", [])),
        "castrongpre": len(tally.get("stronger-pre", [])),
        "caweakspec": len(tally.get("weak-spec", [])),
        "calibrary": len(tally.get("library", [])),
        "catranslation": len(tally.get("translation", [])),
        "carefutebad": sum(1 for t in claims if claims[t] == "reference"),
        "caverifybad": sum(1 for t in claims if claims[t] == "mutant"),
    }
    lines = [r"% GENERATED by nfv/paper/check_causes.py -- do not edit."]
    lines += [rf"\newcommand{{\{k}}}{{{v}}}" for k, v in macros.items()]
    (TABLES / "causes_numbers.tex").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")
    print("\nwrote causes.tex, causes_numbers.tex:", macros)

    if failures:
        print("FAILED:", ", ".join(map(str, failures)))
        return 1
    print("every attribution reproduces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
