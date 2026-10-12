#!/usr/bin/env python3
"""
Build the frozen, sanitized nl2post-safe benchmark.

Removes ALL natural language from both the code and the specification, uniformly
for reference and mutant entries, then materializes each program to a real file so
that provenance annotations (`// src: source.py:<line> | <stmt>`) are stable and
citable.

Why this exists
---------------
The raw subset leaks its labels badly:
  * 41 mutants literally announce the bug ("# This creates a bug by ...")
  * comment *presence alone* separates the classes (100/116 mutants vs 5/116
    references), giving a one-line classifier ~91% accuracy with no analysis.
  * 108 entries embed doctest examples (`>>> f(x)` -> `y`), i.e. concrete
    input/output pairs, which violates the static-only rule and lets a model
    infer bugginess by comparing code against the examples.

Sanitization therefore strips comments, docstrings and doctests from the code,
and reduces each postcondition to its bare `assert` expression.

Usage:
    python nfv/data/build_safe.py
    python nfv/data/build_safe.py --check   # rebuild in-memory and diff, no writes
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tokenize
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NFV_DIR = os.path.dirname(SCRIPT_DIR)
REPO_DIR = os.path.dirname(NFV_DIR)

SOURCE_PATH = os.path.join(REPO_DIR, "paper", "data", "upstream", "results", "nl2post_verified_subset.json")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "nl2post_safe.json")
WORK_DIR = os.path.join(NFV_DIR, "work")

SOURCE_FILENAME = "source.py"


class _StripDocstrings(ast.NodeTransformer):
    """Remove docstring nodes; substitute `pass` if a body would become empty."""

    def _strip(self, node):
        self.generic_visit(node)
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip
    visit_ClassDef = _strip


def sanitize_code(code: str) -> str:
    """Strip all comments, docstrings and doctests; canonicalize formatting.

    `ast.unparse` discards comments and normalizes layout in one step, so the two
    classes become typographically indistinguishable apart from real semantic
    differences.
    """
    tree = _StripDocstrings().visit(ast.parse(code))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def sanitize_postcondition(post: str) -> str:
    """Reduce the spec to its formal content: imports plus the bare assertion.

    Drops the English `#` gloss (present on all entries) and the `assert`
    message operand (`assert e, "msg"` -> `assert e`), which is prose and carries
    no semantics.
    """
    tree = ast.parse(post)
    kept: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            kept.append(stmt)
        elif isinstance(stmt, ast.Assert):
            stmt.msg = None
            kept.append(stmt)
        else:
            raise ValueError(f"unexpected statement in postcondition: {type(stmt).__name__}")
    if not any(isinstance(s, ast.Assert) for s in kept):
        raise ValueError("postcondition contains no assert")
    module = ast.Module(body=kept, type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module)


def count_comments(src: str) -> int:
    n = 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            n += 1
    return n


def count_docstrings(src: str) -> int:
    n = 0
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if ast.get_docstring(node):
                n += 1
    return n


def entry_id(rec: dict) -> str:
    task = rec["task_id"].replace("/", "_")
    return f"{task}__r{rec['response_num']}__{rec['code_type']}"


_TYPE_ONLY = re.compile(r"assert\s+isinstance\s*\([^)]*\)\s*$")


def is_trivial_postcondition(post: str) -> bool:
    """True if the postcondition only asserts the RESULT TYPE.

    `assert isinstance(return_value, bool)` has real content in Python -- a mutant
    returning `None` violates it -- but Dafny's return type already guarantees it,
    so the translated `ensures` is a tautology satisfied identically by a program
    and its mutant. Such a pair is indistinguishable to ANY verifier, so it
    measures nothing and is excluded from the benchmark.

    Excluded as whole pairs (13 correct + 13 incorrect), keeping the split balanced.
    """
    body = "\n".join(l.strip() for l in post.splitlines() if l.strip().startswith("assert"))
    return bool(_TYPE_ONLY.fullmatch(body.strip()))


def _strip_doctests_from_doc(doc: str) -> str:
    """Remove worked examples from a docstring, keeping the prose.

    Examples are concrete input/output pairs -- tests by another name. Keeping them
    would break the static-only rule and, worse, hand the translator the expected
    behaviour, tempting it to "fix" a mutant into agreement with the examples.
    The prose (which carries the domain restrictions) is retained.
    """
    out, skipping = [], False
    for line in doc.splitlines():
        s = line.strip()
        if s.startswith(">>>") or re.match(r"^(for\s+\w+\(|\w+\([^)]*\)\s*(==>|==|=>|->))", s):
            skipping = True
            continue
        if re.match(r"^(Example|Examples|For example|Sample|Input|Output|Constraints)\b[:s]*$",
                    s, re.I):
            skipping = True
            continue
        if skipping:
            # A blank line ends an example block only if real prose follows.
            if not s:
                continue
            if re.match(r"^[A-Z][a-z]", s) and not re.match(r"^\w+\(", s):
                skipping = False
            else:
                continue
        out.append(line)
    return "\n".join(out).strip()


def _docstring_map() -> dict[tuple, str]:
    """Canonical docstring per (task_id, response_num) pair.

    Nine pairs have docstrings that differ between reference and mutant -- typo
    fixes and rewordings, no bug announcements -- but any asymmetry is exploitable,
    so ONE canonical text is used for both halves. The longest is chosen: the rule
    consults no label and retains the most domain information.
    """
    with open(SOURCE_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    best: dict[tuple, str] = {}
    for rec in raw:
        try:
            tree = ast.parse(rec["code"])
        except SyntaxError:
            continue
        doc = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == rec["entry_point"]:
                doc = ast.get_docstring(node) or ""
                break
        doc = _strip_doctests_from_doc(doc)
        key = (rec["task_id"], rec["response_num"])
        if len(doc) > len(best.get(key, "")):
            best[key] = doc
    return best


def build() -> list[dict]:
    with open(SOURCE_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)

    out: list[dict] = []
    dropped_trivial = 0
    docmap = _docstring_map()
    for rec in raw:
        code = sanitize_code(rec["code"])
        post = sanitize_postcondition(rec["postcondition"])
        if is_trivial_postcondition(post):
            dropped_trivial += 1
            continue
        eid = entry_id(rec)
        out.append(
            {
                "entry_id": eid,
                "task_id": rec["task_id"],
                "entry_point": rec["entry_point"],
                "response_num": rec["response_num"],
                "code_type": rec["code_type"],
                "label": rec["label"],
                "code": code,
                "postcondition": post,
                # Prose only (examples stripped) and canonical across the pair.
                # Supplies the domain restrictions that justify a `requires`.
                "docstring": docmap.get((rec["task_id"], rec["response_num"]), ""),
                "code_sha256": hashlib.sha256(code.encode()).hexdigest()[:16],
                # Retained as provenance/triage metadata ONLY. Never executed:
                # the pipeline is statically verified end to end.
                "killing_test_present": rec.get("killing_test") is not None,
            }
        )
    if dropped_trivial:
        print(f"dropped {dropped_trivial} entries with type-assertion-only postconditions")
    return out


def materialize(entries: list[dict]) -> None:
    """Write each sanitized program to nfv/work/<entry_id>/source.py."""
    if os.path.isdir(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    for e in entries:
        d = os.path.join(WORK_DIR, e["entry_id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, SOURCE_FILENAME), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(e["code"] + "\n")


def self_check(entries: list[dict], raw_path: str) -> None:
    """Hard-fail on anything that would invalidate the benchmark."""
    errs: list[str] = []

    for e in entries:
        try:
            compile(e["code"], "<sanitized>", "exec")
        except SyntaxError as exc:
            errs.append(f"{e['entry_id']}: sanitized code does not compile: {exc}")
            continue
        if count_comments(e["code"]):
            errs.append(f"{e['entry_id']}: residual comments in code")
        if count_docstrings(e["code"]):
            errs.append(f"{e['entry_id']}: residual docstrings in code")
        if ">>>" in e["code"]:
            errs.append(f"{e['entry_id']}: residual doctest in code")
        if "#" in e["postcondition"] or ">>>" in e["postcondition"]:
            errs.append(f"{e['entry_id']}: residual NL in postcondition")
        if "assert" not in e["postcondition"]:
            errs.append(f"{e['entry_id']}: postcondition lost its assert")

    # A pair collapsing to identical code would mean the "bug" was only a comment,
    # i.e. sanitization destroyed the benchmark entry.
    pairs: dict[tuple, dict] = defaultdict(dict)
    for e in entries:
        pairs[(e["task_id"], e["response_num"])][e["code_type"]] = e
    collapsed = [
        k
        for k, v in pairs.items()
        if "reference" in v and "mutant" in v and v["reference"]["code"] == v["mutant"]["code"]
    ]
    for k in collapsed:
        errs.append(f"{k}: reference and mutant collapse to identical sanitized code")

    # Dropping trivial entries must remove BOTH halves of a pair, or the remaining
    # half would silently skew the class balance.
    for k, v in pairs.items():
        if len(v) != 2:
            errs.append(f"{k}: incomplete pair after filtering ({sorted(v)})")
    n_correct = sum(1 for e in entries if e["label"] == "correct")
    n_incorrect = sum(1 for e in entries if e["label"] == "incorrect")
    if n_correct != n_incorrect:
        errs.append(f"class imbalance: {n_correct} correct vs {n_incorrect} incorrect")

    # No surviving postcondition may be a pure type assertion.
    for e in entries:
        if is_trivial_postcondition(e["postcondition"]):
            errs.append(f"{e['entry_id']}: trivial type-assertion postcondition survived")

    # Docstrings are prose only, and identical across a pair, so they can supply
    # domain restrictions without becoming a label signal.
    for e in entries:
        if ">>>" in e["docstring"]:
            errs.append(f"{e['entry_id']}: doctest survived in docstring")
    for k, v in pairs.items():
        if len(v) == 2 and v["reference"]["docstring"] != v["mutant"]["docstring"]:
            errs.append(f"{k}: docstring differs between reference and mutant")

    if errs:
        print("SELF-CHECK FAILED:", file=sys.stderr)
        for m in errs[:40]:
            print("  -", m, file=sys.stderr)
        raise SystemExit(1)

    print(f"self-check OK: {len(entries)} entries, {len(pairs)} pairs, 0 collapsed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="build and validate without writing")
    args = ap.parse_args()

    entries = build()
    self_check(entries, SOURCE_PATH)

    n_correct = sum(1 for e in entries if e["label"] == "correct")
    n_incorrect = sum(1 for e in entries if e["label"] == "incorrect")
    print(f"labels: correct={n_correct} incorrect={n_incorrect}")

    if args.check:
        print("--check: no files written")
        return

    payload = {
        "description": "nl2post-safe: sanitized, execution-verified subset. "
        "All natural language removed from code and spec.",
        "source": os.path.relpath(SOURCE_PATH, REPO_DIR).replace("\\", "/"),
        "sanitization": {
            "code": "comments, docstrings and doctests removed; ast.unparse canonicalization",
            "postcondition": "bare assert only; '#' gloss and assert message operand removed",
            "applied_to": "reference and mutant uniformly",
        },
        "static_only": "killing_test is NOT included; nothing is ever executed",
        "total": len(entries),
        "num_correct": n_correct,
        "num_incorrect": n_incorrect,
        "entries": entries,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    materialize(entries)
    print(f"wrote {OUTPUT_PATH}")
    print(f"materialized {len(entries)} programs under {WORK_DIR}")


if __name__ == "__main__":
    main()
