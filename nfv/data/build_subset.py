"""Derive a named entry subset from a completed run, and record its provenance.

A subset is stored as `nfv/data/subsets/<name>.json` and selected at run time
with `--subset <name>`. It is a list of `task_id`s plus enough provenance to
say exactly how it was produced, so a subset can never become an unexplained
list of identifiers.

Subsets always drop or keep WHOLE PAIRS. The benchmark is 103 reference/mutant
pairs and every pair-level metric (discriminating, non-discriminating, both
refuted) is meaningless if one half of a pair is missing.

Build the `core63` subset:

    python -m nfv.data.build_subset core63 \
        --from nfv/results/full_gpt56sol_boogie_both.jsonl
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUBSET_DIR = ROOT / "subsets"
DATASET = ROOT / "nl2post_safe.json"


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _proved(records: list[dict]) -> set[str]:
    """task_ids whose CORRECT entry this run proved, ignoring rescue proofs."""
    return {r["task_id"] for r in records
            if r["label"] == "correct" and r["prediction"] == "correct"
            and not r.get("rescue_used")}


def spurious_cex_tasks(sources: dict[str, list[dict]]) -> set[str]:
    """task_ids where a run reported a counterexample on a CORRECT entry.

    Such a verdict is unsound by construction: the entry is the unmutated
    reference, so any counterexample against it is an artefact of the encoding
    rather than a bug (see findings F14 and F15).
    """
    out: set[str] = set()
    for records in sources.values():
        out |= {r["task_id"] for r in records
                if r["label"] == "correct" and r["prediction"] == "incorrect"}
    return out


def unprovable_tasks(sources: dict[str, list[dict]]) -> set[str]:
    """task_ids whose CORRECT entry NO supplied run could prove.

    Backend-neutral difficulty signal: a pair is dropped only when every arm
    failed on it, rather than when one arm's bounded phase misfired.
    """
    proved: set[str] = set()
    seen: set[str] = set()
    for records in sources.values():
        proved |= _proved(records)
        seen |= {r["task_id"] for r in records}
    return seen - proved


_TRIVIAL = {"len", "range", ".append", ".pop", "print"}
_TOTAL_BUILTINS = {"abs", "min", "max", "sum", "enumerate", "zip", "ord", "chr",
                   "round", "int", "float", "str", "bool"}
_COLLECTIONS_HOF = {
    "sorted", "set", "dict", "list", "tuple", "filter", "map", "any", "all",
    ".sort", ".join", ".split", ".items", ".count", ".index", ".lower", ".upper",
    ".isalpha", ".isupper", ".islower", ".isdigit", ".replace", ".reverse",
    ".values", ".add", ".strip", ".title", ".swapcase", ".insert", ".remove",
    ".copy", ".intersection", ".lstrip", ".isnumeric", ".get",
}
_MODULES = {"math", "re", "functools", "itertools", "collections", "hashlib", "string"}


def _calls_used() -> dict[str, set[str]]:
    """Per task_id, the set of non-local calls made by EITHER entry of the pair.

    Source-only: parses `code` from the dataset and consults no run.
    """
    import ast

    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    used: dict[str, set[str]] = {}
    for e in payload["entries"]:
        acc = used.setdefault(e["task_id"], set())
        tree = ast.parse(e["code"])
        local = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    if f.id not in local:
                        acc.add(f.id)
                elif isinstance(f, ast.Attribute):
                    if isinstance(f.value, ast.Name) and f.value.id in _MODULES:
                        acc.add(f.value.id + "." + f.attr)
                    else:
                        acc.add("." + f.attr)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = getattr(n, "module", None) or ""
                for a in n.names:
                    if (mod or a.name) != "typing":
                        acc.add("import " + (mod or a.name))
    return used


def library_tier(task_calls: set[str]) -> int:
    """0 = nothing to axiomatise, 1 = total builtins, 2 = collections/HOF, 3 = imports."""
    rest = task_calls - _TRIVIAL
    if not rest:
        return 0
    if any(c.startswith("import ") for c in rest) or (rest - _TOTAL_BUILTINS - _COLLECTIONS_HOF):
        return 3
    if not rest - _TOTAL_BUILTINS:
        return 1
    return 2


def higher_order_tasks() -> set[str]:
    """task_ids that pass a FUNCTION VALUE as an argument to a call.

    Source-only, like `library_tier`, and a different question from which names
    are called: `sorted(xs)` orders values, `sorted(xs, key=lambda x: -x)`
    orders them by a function the specification must also describe. The second
    is what makes a specification quantify over an argument it cannot see the
    body of, which is a distinct axiomatisation cost from collections alone.

    A function value is a `lambda`, a reference to a function defined in the
    same source, or a bare builtin function name used as an argument rather
    than called.
    """
    import ast

    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    out: set[str] = set()
    for e in payload["entries"]:
        tree = ast.parse(e["code"])
        local = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            for a in list(n.args) + [k.value for k in n.keywords]:
                if isinstance(a, ast.Lambda):
                    out.add(e["task_id"])
                elif isinstance(a, ast.Name) and (a.id in local or a.id in _FUNCTION_VALUES):
                    out.add(e["task_id"])
    return out


# Builtins whose NAME, passed as an argument, denotes a function rather than a
# value -- `map(int, xs)` is higher-order, `int(x)` is not.
_FUNCTION_VALUES = {"abs", "len", "str", "int", "float", "ord", "chr", "sum",
                    "max", "min", "sorted", "bool", "round"}


def tier_label(task_id: str, task_calls: set[str], hof: set[str]) -> str:
    """The library tier as reported, with L2 split by higher-order use."""
    k = library_tier(task_calls)
    if k == 2:
        return "2b" if task_id in hof else "2a"
    return str(k)


def _tier_recipe(level: int):
    def drop(sources: dict[str, list[dict]]) -> set[str]:
        used = _calls_used()
        return {t for t, c in used.items() if library_tier(c) != level}
    return drop


def library_dependent_tasks(sources: dict[str, list[dict]]) -> set[str]:
    """task_ids whose Python source calls anything beyond the trivially-modelled core.

    Source-only signal: computed by parsing `code` in the dataset, with no
    reference to any run. A call is "trivially modelled" if a formal encoding
    needs no axiom for it -- `len`, `range`, `list.append`, `list.pop`, `print`
    -- plus arithmetic, indexing and control flow. Everything else (builtins
    such as `sorted`/`sum`/`abs`, string and list methods, and any module
    import other than `typing`, which is annotation-only) forces the model to
    invent or assume a specification, which is the trusted-base channel behind
    findings F5, F7, F9 and F12.

    `sources` is ignored; the signature matches the other recipes.
    """
    import ast

    trivial = {"len", "range", ".append", ".pop", "print"}
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    modules = {"math", "re", "functools", "itertools", "collections", "hashlib", "string"}
    dependent: set[str] = set()
    for e in payload["entries"]:
        tree = ast.parse(e["code"])
        local = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        used: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    if f.id not in local:
                        used.add(f.id)
                elif isinstance(f, ast.Attribute):
                    if isinstance(f.value, ast.Name) and f.value.id in modules:
                        used.add(f.value.id + "." + f.attr)
                    else:
                        used.add("." + f.attr)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = getattr(n, "module", None) or ""
                for a in n.names:
                    if (mod or a.name) != "typing":
                        used.add("import " + (mod or a.name))
        if used - trivial:
            dependent.add(e["task_id"])
    return dependent


RECIPES = {
    "core63": {
        "drop": spurious_cex_tasks,
        "rationale":
            "Drops every pair whose reference program was falsely refuted by "
            "Boogie's bounded phase. Findings F14/F15 established by executing "
            "the witnesses that none of those refutations is a real bug: the "
            "counterexamples are artefacts of an unconstrained list length, an "
            "uninterpreted sort with no Python inhabitants, or underconstrained "
            "library axioms. Experiment K then showed the dropped entries are "
            "18-26 points harder for EVERY backend, including z3 and dafny, "
            "which never run Boogie's bounded phase -- so this is a general "
            "'hard to formalise' signal rather than a Boogie artefact. "
            "NOTE: the subset is selected on a difficulty-correlated signal, so "
            "its absolute numbers are optimistic and must not be quoted as "
            "headline results; use it for comparisons between arms.",
    },
    "provable89": {
        "drop": unprovable_tasks,
        "rationale":
            "Drops only the pairs that NO unbounded arm (dafny, dafny-total, "
            "boogie, z3) could prove. Motivation: core63's selection signal is "
            "bounded-Boogie refutation, but unbounded Boogie itself proves 24 "
            "of those same 40 references, so that signal removes many pairs "
            "that are demonstrably provable. This recipe replaces it with a "
            "backend-neutral one -- drop a pair only when every arm failed. "
            "NOTE: this selects directly on the outcome being measured, so its "
            "absolute proof rates are inflated BY CONSTRUCTION and are not "
            "meaningful. It exists solely to test whether the BETWEEN-ARM "
            "ranking of Finding F16 survives a differently-biased subset; the "
            "ranking is the output, never the levels.",
    },
    "libfree21": {
        "drop": library_dependent_tasks,
        "rationale":
            "Keeps only the pairs whose Python source calls nothing beyond the "
            "trivially-modelled core (len, range, list.append/pop, print) plus "
            "arithmetic, indexing and control flow. Every other call -- sorted, "
            "sum, abs, string/list methods, or a module import -- forces the "
            "model to invent or assume a specification, which is the "
            "trusted-base channel behind F5, F7, F9 and F12. This subset "
            "therefore isolates the pipeline's reasoning from its "
            "axiomatisation: a failure here cannot be blamed on a missing or "
            "wrong library spec. Selected from the Python source ONLY, with no "
            "reference to any run, so unlike core63 it is not contaminated by "
            "the outcome being measured (see F18). NOTE: it is small (21 pairs "
            "/ 42 entries), so it has little power for violation counts; use it "
            "for mechanism claims, not for precise rates.",
    },
    "libtier1": {
        "drop": _tier_recipe(1),
        "rationale":
            "Only the L1 pairs: the program calls total builtins (abs, min, "
            "max, sum, ord, chr, round, casts) and nothing heavier. 22 pairs.",
    },
    "libtier2": {
        "drop": _tier_recipe(2),
        "rationale":
            "Only the L2 pairs: collections and higher-order functions "
            "(sorted, set, dict, filter, map, any, all) and string/list "
            "methods. 51 pairs.",
    },
    "libtier3": {
        "drop": _tier_recipe(3),
        "rationale":
            "Only the L3 pairs: a module import (math, functools) or an exotic "
            "builtin such as bin. 9 pairs -- far too small for rates; use for "
            "case analysis only.",
    },
}


def build(name: str, sources: list[Path]) -> Path:
    if name not in RECIPES:
        raise SystemExit(f"unknown subset {name!r}; known: {', '.join(RECIPES)}")
    recipe = RECIPES[name]
    loaded = {str(p).replace("\\", "/"): _load(p) for p in sources}
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    entries = payload["entries"]

    all_tasks = {e["task_id"] for e in entries}
    dropped = recipe["drop"](loaded) & all_tasks
    kept = sorted(all_tasks - dropped)

    # A pair is two entries sharing a task_id. Refuse to emit a subset that
    # would split one, which would silently invalidate every pair metric.
    by_task: dict[str, int] = {}
    for e in entries:
        by_task[e["task_id"]] = by_task.get(e["task_id"], 0) + 1
    partial = [t for t in kept if by_task.get(t, 0) != 2]
    if partial:
        raise SystemExit(f"refusing: {len(partial)} kept task_ids are not complete pairs")

    out = {
        "name": name,
        "rationale": recipe["rationale"],
        "derived_from": sorted(loaded),
        "derived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": DATASET.name,
        "n_pairs_total": len(all_tasks),
        "n_pairs_dropped": len(dropped),
        "n_pairs_kept": len(kept),
        "n_entries_kept": 2 * len(kept),
        "dropped_task_ids": sorted(dropped),
        "task_ids": kept,
    }
    SUBSET_DIR.mkdir(exist_ok=True)
    path = SUBSET_DIR / f"{name}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: kept {len(kept)} pairs ({2 * len(kept)} entries), "
          f"dropped {len(dropped)} -> {path}")
    return path


def load_subset(name: str) -> dict:
    path = SUBSET_DIR / f"{name}.json"
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in SUBSET_DIR.glob("*.json"))) or "none"
        raise SystemExit(f"unknown subset {name!r}; available: {known}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", choices=sorted(RECIPES))
    ap.add_argument("--from", dest="source", required=True, type=Path, action="append",
                    help="a completed run's .jsonl; repeat for recipes that "
                         "combine several arms (e.g. provable89)")
    args = ap.parse_args()
    build(args.name, args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
