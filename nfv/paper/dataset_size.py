"""Size distribution of the 206 dataset programs.

The dataset's difficulty is usually discussed in terms of the library surface a
program touches (the tiers), which says what must be *specified*.  This says
what must be *translated*: how much code the transliteration phase has to carry
across, and how much control flow the proof search then has to reason about.

Three measures, all computed from the Python source alone and none of them from
any run:

  * LOC   -- non-blank, non-comment lines of the function under test, counting
             the signature and excluding the docstring, since the docstring is
             an input to phase 0b rather than something translated.
  * AST   -- number of nodes in the parsed function, a size measure that does
             not depend on formatting.
  * loops -- `for` and `while` statements, because each one is an invariant the
             proof search must supply, and branches, each a case it must split.

Correct references and their buggy mutants are reported separately, and the
paired difference is computed task by task, because the two populations turn out
NOT to be the same size.  The mutants are independently written buggy solutions
to the same task, not minimal edits of the reference, so they are systematically
wordier.  That is a confound worth stating: verification is scored on the
references and refutation on the mutants, so the two tasks are not measured over
programs of equal translation burden.

Writes nfv/paper/tables/size.tex and prints the same numbers.
"""
from __future__ import annotations

import ast
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "nfv" / "data" / "nl2post_safe.json"
TABLES = Path(__file__).resolve().parent / "tables"

BANDS = ((1, 5), (6, 10), (11, 15), (16, 25), (26, 10**6))
BAND_LABELS = ("1--5", "6--10", "11--15", "16--25", "$>$25")


def _entry_fn(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def measure(code: str, entry_point: str) -> dict | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    fn = _entry_fn(tree, entry_point)
    if fn is None:
        return None

    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]          # drop the docstring: an input, not a translation

    lines = {fn.lineno}
    for stmt in body:
        for node in ast.walk(stmt):
            if hasattr(node, "lineno"):
                lines.add(node.lineno)

    loops = sum(1 for s in body for n in ast.walk(s)
                if isinstance(n, (ast.For, ast.While, ast.AsyncFor)))
    branches = sum(1 for s in body for n in ast.walk(s)
                   if isinstance(n, (ast.If, ast.IfExp)))
    comps = sum(1 for s in body for n in ast.walk(s)
                if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp,
                                  ast.GeneratorExp)))
    nodes = sum(1 for s in body for _ in ast.walk(s)) + 1

    return {"loc": len(lines), "ast": nodes, "loops": loops,
            "branches": branches, "comps": comps}


def summarize(vals: list[int]) -> dict:
    s = sorted(vals)
    return {
        "n": len(s),
        "min": s[0],
        "p25": s[len(s) // 4],
        "median": statistics.median(s),
        "mean": statistics.mean(s),
        "p75": s[(3 * len(s)) // 4],
        "p90": s[int(0.9 * (len(s) - 1))],
        "max": s[-1],
    }


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows: list[tuple[str, dict]] = []
    skipped = []
    for e in data["entries"]:
        m = measure(e["code"], e["entry_point"])
        if m is None:
            skipped.append(e["entry_id"])
            continue
        rows.append((e["label"], m))

    assert not skipped, f"unparsed entries: {skipped}"
    assert len(rows) == data["total"], f"{len(rows)} != {data['total']}"

    pops = {
        "all": [m for _, m in rows],
        "correct": [m for lab, m in rows if lab == "correct"],
        "incorrect": [m for lab, m in rows if lab == "incorrect"],
    }

    # Paired by task, so the reference/mutant gap is not a population artefact.
    by_task: dict[str, dict[str, dict]] = {}
    for e in data["entries"]:
        m = measure(e["code"], e["entry_point"])
        by_task.setdefault(e["task_id"], {})[e["label"]] = m
    paired = [(v["correct"]["loc"], v["incorrect"]["loc"])
              for v in by_task.values() if "correct" in v and "incorrect" in v]
    diffs = [b - a for a, b in paired]
    d_bigger = sum(1 for x in diffs if x > 0)
    d_same = sum(1 for x in diffs if x == 0)
    d_smaller = sum(1 for x in diffs if x < 0)

    stats = {k: {f: summarize([m[f] for m in v]) for f in ("loc", "ast")}
             for k, v in pops.items()}

    loc_all = [m["loc"] for m in pops["all"]]
    bands = []
    for lo, hi in BANDS:
        n = sum(1 for v in loc_all if lo <= v <= hi)
        bands.append((n, 100.0 * n / len(loc_all)))

    a = pops["all"]
    with_loop = sum(1 for m in a if m["loops"])
    loop_free = len(a) - with_loop
    with_branch = sum(1 for m in a if m["branches"])
    with_comp = sum(1 for m in a if m["comps"])
    multi_loop = sum(1 for m in a if m["loops"] >= 2)

    print(f"{len(a)} programs\n")
    print(f"{'':<10} {'n':>4} {'min':>4} {'p25':>5} {'med':>5} {'mean':>6} "
          f"{'p75':>5} {'p90':>5} {'max':>5}")
    for pop in ("all", "correct", "incorrect"):
        for field in ("loc", "ast"):
            s = stats[pop][field]
            print(f"{pop + '/' + field:<10} {s['n']:>4} {s['min']:>4} "
                  f"{s['p25']:>5} {s['median']:>5.0f} {s['mean']:>6.1f} "
                  f"{s['p75']:>5} {s['p90']:>5} {s['max']:>5}")
    print()
    for lbl, (n, pct) in zip(BAND_LABELS, bands):
        print(f"  LOC {lbl:<7} {n:>4}  {pct:5.1f}%")
    print()
    print(f"  loop-free            {loop_free:>4}  {100.0*loop_free/len(a):5.1f}%")
    print(f"  >=1 loop             {with_loop:>4}  {100.0*with_loop/len(a):5.1f}%")
    print(f"  >=2 loops            {multi_loop:>4}  {100.0*multi_loop/len(a):5.1f}%")
    print(f"  >=1 branch           {with_branch:>4}  {100.0*with_branch/len(a):5.1f}%")
    print(f"  >=1 comprehension    {with_comp:>4}  {100.0*with_comp/len(a):5.1f}%")
    print()
    print(f"  paired tasks         {len(paired):>4}")
    print(f"  mutant larger        {d_bigger:>4}   same {d_same}   smaller {d_smaller}")
    print(f"  median LOC gap       {statistics.median(diffs):>4.0f}"
          f"   mean {statistics.mean(diffs):.2f}")

    sa = stats["all"]
    tex = [
        r"% generated by nfv/paper/dataset_size.py -- do not edit",
        r"\begin{table}[t]\centering\small",
        r"\caption{Size and control flow of the "
        f"{len(a)} programs. LOC counts distinct physical lines of the",
        r"function under test carrying a syntax node; comments, blank lines,",
        r"imports and the docstring are excluded. Paired by task, mutants are",
        f"longer than their reference on {d_bigger} of {len(paired)} tasks and",
        f"shorter on {d_smaller}, a median gap of "
        f"{statistics.median(diffs):.0f} lines.""}",
        r"\label{tab:size}",
        r"\begin{tabular}{lrrrrrrr}",
        r"\toprule",
        r" & min & p25 & median & mean & p75 & p90 & max \\",
        r"\midrule",
        (f"LOC & {sa['loc']['min']} & {sa['loc']['p25']} & "
         f"{sa['loc']['median']:.0f} & {sa['loc']['mean']:.1f} & "
         f"{sa['loc']['p75']} & {sa['loc']['p90']} & {sa['loc']['max']} \\\\"),
        r"\bottomrule",
        r"\end{tabular}",
        r"",
        r"\smallskip",
        r"\begin{tabular}{lrr@{\qquad}lrr}",
        r"\toprule",
        r"control flow & \# & \% & control flow & \# & \% \\",
        r"\midrule",
    ]
    flow = [("loop-free", loop_free), ("$\\ge 1$ branch", with_branch),
            ("$\\ge 1$ loop", with_loop), ("$\\ge 1$ comprehension", with_comp),
            ("$\\ge 2$ loops", multi_loop)]
    pct = lambda n: 100.0 * n / len(a)
    for i in range(0, len(flow), 2):
        left = flow[i]
        row = f"{left[0]} & {left[1]} & {pct(left[1]):.0f}\\%"
        if i + 1 < len(flow):
            right = flow[i + 1]
            row += f" & {right[0]} & {right[1]} & {pct(right[1]):.0f}\\%"
        else:
            row += r" & & & "
        tex.append(row + r" \\")
    tex += [r"\bottomrule", r"\end{tabular}", r"\end{table}", r""]

    out = TABLES / "size.tex"
    out.write_text("\n".join(tex), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
