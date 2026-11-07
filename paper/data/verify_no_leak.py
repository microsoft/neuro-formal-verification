#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Audit the sanitized benchmark for label leakage.

Hard-fails on anything that must be absent (natural language residue, collapsed
pairs). *Reports* — but does not fail on — residual statistical signal, because
some signal is legitimate: mutants really are different programs, and mutation
tends to add code. Asserting that to zero would mean deleting the benchmark.

Usage:
    python -m paper.data.verify_no_leak
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import tokenize
from collections import Counter, defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(SCRIPT_DIR, "nl2post_safe.json")

BUG_WORDS = re.compile(r"bug|mutant|incorrect|intentional|wrong|broken|introduc", re.I)


def best_threshold_accuracy(entries: list[dict], feat) -> tuple[float, str]:
    """Accuracy of the best single-threshold classifier on `feat`.

    Tuned on the evaluation set itself, so it is an optimistic upper bound on what
    a trivial lexical classifier could achieve.
    """
    vals = sorted({feat(e) for e in entries})
    best, desc = 0.0, ""
    for t in vals:
        for sign in (1, -1):
            hit = sum(
                1
                for e in entries
                if ((feat(e) > t) if sign == 1 else (feat(e) <= t)) == (e["code_type"] == "mutant")
            )
            acc = hit / len(entries)
            if acc > best:
                best, desc = acc, f"{'>' if sign == 1 else '<='} {t}"
    return best, desc


def token_signal(entries: list[dict]) -> tuple[float, list[tuple[str, float]]]:
    """Most class-discriminative identifiers, and a 1-token classifier's accuracy."""
    def toks(src: str) -> set[str]:
        out = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.NAME:
                    out.add(tok.string)
        except Exception:
            pass
        return out

    n_mut = sum(1 for e in entries if e["code_type"] == "mutant")
    n_ref = len(entries) - n_mut
    df: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in entries:
        idx = 0 if e["code_type"] == "mutant" else 1
        for t in toks(e["code"]):
            df[t][idx] += 1

    scored = []
    for t, (m, r) in df.items():
        if m + r < 5:
            continue
        acc = (m + (n_ref - r)) / len(entries)
        scored.append((t, max(acc, 1 - acc)))
    scored.sort(key=lambda kv: -kv[1])
    best = scored[0][1] if scored else 0.5
    return best, scored[:5]


def main() -> None:
    if not os.path.exists(DATASET):
        print(f"missing {DATASET}; run build_safe.py first", file=sys.stderr)
        raise SystemExit(1)
    with open(DATASET, encoding="utf-8") as fh:
        payload = json.load(fh)
    entries = payload["entries"]

    hard: list[str] = []

    # ---- hard requirements: no natural language anywhere ----
    n_comments = n_docstrings = n_doctests = n_bugwords = n_post_nl = 0
    for e in entries:
        code = e["code"]
        try:
            for tok in tokenize.generate_tokens(io.StringIO(code).readline):
                if tok.type == tokenize.COMMENT:
                    n_comments += 1
        except Exception:
            hard.append(f"{e['entry_id']}: code does not tokenize")
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if ast.get_docstring(node):
                    n_docstrings += 1
        if ">>>" in code:
            n_doctests += 1
        if BUG_WORDS.search(code):
            n_bugwords += 1
        if "#" in e["postcondition"] or ">>>" in e["postcondition"]:
            n_post_nl += 1

    for name, count in [
        ("comments in code", n_comments),
        ("docstrings in code", n_docstrings),
        ("doctests in code", n_doctests),
        ("bug-words in code", n_bugwords),
        ("natural language in postcondition", n_post_nl),
    ]:
        if count:
            hard.append(f"{count} entries with {name}")

    # ---- hard requirement: no pair may collapse ----
    pairs: dict[tuple, dict] = defaultdict(dict)
    for e in entries:
        pairs[(e["task_id"], e["response_num"])][e["code_type"]] = e
    collapsed = [
        k for k, v in pairs.items()
        if "reference" in v and "mutant" in v and v["reference"]["code"] == v["mutant"]["code"]
    ]
    if collapsed:
        hard.append(f"{len(collapsed)} pairs collapse to identical sanitized code")

    # ---- reported (not enforced) residual signal ----
    len_acc, len_rule = best_threshold_accuracy(entries, lambda e: len(e["code"]))
    line_acc, line_rule = best_threshold_accuracy(entries, lambda e: e["code"].count("\n"))
    tok_acc, top_tokens = token_signal(entries)

    print("=" * 68)
    print("LEAKAGE AUDIT — nl2post-safe")
    print("=" * 68)
    print(f"entries: {len(entries)}   pairs: {len(pairs)}   "
          f"correct: {payload['num_correct']}   incorrect: {payload['num_incorrect']}")
    print()
    print("hard requirements (must be zero):")
    print(f"  comments in code .................. {n_comments}")
    print(f"  docstrings in code ................ {n_docstrings}")
    print(f"  doctests in code .................. {n_doctests}")
    print(f"  bug-announcing words in code ...... {n_bugwords}")
    print(f"  natural language in postcondition . {n_post_nl}")
    print(f"  collapsed reference/mutant pairs .. {len(collapsed)}")
    print()
    print("residual signal (reported, optimistic upper bounds — tuned on this set):")
    print(f"  majority baseline ................. 50.0%")
    print(f"  best code-length threshold ........ {100*len_acc:.1f}%  (len {len_rule})")
    print(f"  best line-count threshold ......... {100*line_acc:.1f}%  (lines {line_rule})")
    print(f"  best single-identifier rule ....... {100*tok_acc:.1f}%")
    print(f"    top identifiers: {', '.join(f'{t} ({100*a:.0f}%)' for t, a in top_tokens)}")
    print()
    print("  NOTE: residual length signal reflects mutation adding code, not a")
    print("        natural-language tell. It is a property of the benchmark and is")
    print("        reported as a threat to validity, not asserted to zero.")
    print()

    if hard:
        print("RESULT: FAIL", file=sys.stderr)
        for m in hard:
            print("  -", m, file=sys.stderr)
        raise SystemExit(1)
    print("RESULT: PASS — no natural-language leakage, no collapsed pairs")


if __name__ == "__main__":
    main()
