#!/usr/bin/env python3
"""
Reproduce the benchmark from public sources, and verify the frozen copy.

The benchmark shipped in this repository (`paper/data/nl2post_safe.json`, 206
entries = 103 reference/mutant pairs) is not authored here. It is a filtered,
sanitized projection of two public artifacts. This script states that derivation
as a sequence of executable stages so a reader can either re-run it or check that
the frozen file is what the recipe says it is.

Stages
------
  S1  HumanEval + nl2post postconditions + code mutants -> nl2post_dataset.json
      (310 entries / 155 pairs)   [nl2post/build_nl2post_dataset.py]
  S2  execution-verify every pair                       -> nl2post_verified_subset.json
      (232 entries / 116 pairs)   [nl2post/verify_nl2post_dataset.py, validate_specs.py]
  S3  strip all natural language, drop degenerate pairs -> paper/data/nl2post_safe.json
      (206 entries / 103 pairs)   [paper/data/build_safe.py]
  S4  tier-stratified pilot draw                        -> paper/data/subsets/pilot10.json
      (20 entries / 10 pairs)     [this script, --stage 4]

S1 and S2 need the upstream zips (see --help output of `check`). S3 and S4 are
deterministic given the previous stage, so they can always be re-derived and
compared byte-for-byte against what is committed.

Usage
-----
    python -m paper.data.rebuild_dataset check        # verify frozen files (default)
    python -m paper.data.rebuild_dataset stages       # print the derivation
    python -m paper.data.rebuild_dataset pilot        # re-derive pilot10 from its seed
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "paper" / "data"
DATASET = DATA / "nl2post_safe.json"
SUBSETS = DATA / "subsets"

# Stage -> (artifact, expected entry count, expected pair count, producer)
STAGES = [
    ("S1", "nl2post/nl2post_dataset.json", 310, 155, "nl2post/build_nl2post_dataset.py"),
    ("S2", "results/nl2post_verified_subset.json", 232, 116, "nl2post/verify_nl2post_dataset.py"),
    ("S3", "paper/data/nl2post_safe.json", 206, 103, "paper/data/build_safe.py"),
    ("S4", "paper/data/subsets/pilot10.json", 20, 10, "paper/data/rebuild_dataset.py pilot"),
]

UPSTREAM = [
    ("nl2post GPT-4 postconditions (simple/no-ref ablation)",
     "nl2post/eval_results/gpt4_simple_no_ref.zip"),
    ("distinct code mutants + distinguishing inputs",
     "nl2post/eval_results/distinct_code_mutants.jsonl.zip"),
]

# Members actually read from each archive. Everything else in them is unused.
ARCHIVE_MEMBERS = [
    ("gpt4_simple_no_ref.zip",
     "gpt4_simple_no_ref/summary_eval_results.jsonl",
     "1640 postcondition records; 1264 have test_set_correct == True"),
    ("gpt4_simple_no_ref.zip",
     "gpt4_simple_no_ref/individual_postcondition_results/"
     "HumanEval_<id>/postcondition_<n>.json",
     "1264 files, one per surviving postcondition"),
    ("distinct_code_mutants.jsonl.zip",
     "distinct_code_mutants.jsonl",
     "5939 mutant records, keyed by (task_id, response_num)"),
]

# HumanEval/EvalPlus problems come from the installed `evalplus` package
# (evalplus.data.get_human_eval_plus, 164 problems), NOT from any archive here.



def _entries(payload):
    if not isinstance(payload, dict):
        return payload
    for key in ("entries", "specs"):
        if key in payload:
            return payload[key]
    raise KeyError("no entry list in payload")


def _pairs(entries) -> dict:
    out: dict = {}
    for e in entries:
        out.setdefault((e["task_id"], e.get("response_num")), {})[e["code_type"]] = e
    return out


def cmd_stages(_args) -> int:
    print("Upstream inputs (public, not produced here):")
    for name, rel in UPSTREAM:
        mark = "present" if (ROOT / rel).exists() else "ABSENT"
        print(f"  [{mark:>7}] {name}\n              {rel}")
    print("\nDerivation:")
    for tag, rel, n, npairs, producer in STAGES:
        mark = "present" if (ROOT / rel).exists() else "ABSENT"
        print(f"  {tag}  {rel}")
        print(f"        {n:>4} entries / {npairs:>3} pairs   [{mark}]")
        print(f"        produced by: {producer}")
    print("\nWhat each stage removes:")
    print("  S2  pairs whose reference itself violates its own postcondition, or whose")
    print("      mutant fails for a reason other than the postcondition (155 -> 116 pairs).")
    print("  S3  natural language (comments, docstrings, doctests) from BOTH halves, and")
    print("      whole pairs whose postcondition only asserts the result TYPE, which any")
    print("      typed target language discharges vacuously (116 -> 103 pairs).")
    print("  S4  a tier-stratified 10-pair draw, selected on a source-only property.")
    return 0


def cmd_check(_args) -> int:
    ok = True
    for tag, rel, n, npairs, _ in STAGES:
        path = ROOT / rel if tag in {"S3", "S4"} else ROOT / "paper/data/upstream" / rel
        if not path.exists():
            print(f"  {tag}  {rel}: ABSENT")
            ok = False
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if tag == "S4":
            got_n, got_p = payload["n_entries_kept"], payload["n_pairs_kept"]
        else:
            es = _entries(payload)
            got_n, got_p = len(es), len(_pairs(es))
        good = (got_n, got_p) == (n, npairs)
        ok &= good
        print(f"  {tag}  {rel}: {got_n} entries / {got_p} pairs "
              f"{'OK' if good else f'MISMATCH (expected {n}/{npairs})'}")

    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    entries = payload["entries"]
    bad = [k for k, v in _pairs(entries).items() if set(v) != {"reference", "mutant"}]
    if bad:
        ok = False
        print(f"  BROKEN PAIRS: {len(bad)}")
    labels = {"correct": 0, "incorrect": 0}
    for e in entries:
        labels[e["label"]] = labels.get(e["label"], 0) + 1
    print(f"  labels: {labels} {'OK' if labels['correct'] == labels['incorrect'] else 'UNBALANCED'}")
    ok &= labels["correct"] == labels["incorrect"]

    leaks = [e["entry_id"] for e in entries if "#" in e["code"] or '"""' in e["code"]]
    print(f"  natural-language residue in code: {len(leaks)} {'OK' if not leaks else 'LEAK'}")
    ok &= not leaks

    exe = [e["entry_id"] for e in entries if "killing_test" in e]
    print(f"  killing tests present: {len(exe)} {'OK (static-only)' if not exe else 'LEAK'}")
    ok &= not exe

    print("\n" + ("ALL CHECKS PASSED" if ok else "CHECKS FAILED"))
    return 0 if ok else 1


def cmd_pilot(_args) -> int:
    """Re-derive pilot10 from its recorded seed and compare to the frozen file."""
    from paper.data.build_subset import _calls_used, library_tier

    frozen = json.loads((SUBSETS / "pilot10.json").read_text(encoding="utf-8"))
    tiers = {t: library_tier(c) for t, c in _calls_used().items()}
    by_tier: dict[str, list[str]] = {}
    for task, lvl in tiers.items():
        by_tier.setdefault(f"L{lvl}", []).append(task)
    for k in by_tier:
        by_tier[k].sort()

    rng = random.Random(frozen["seed"])
    drawn: list[str] = []
    for tier in sorted(frozen["tier_quota"]):
        drawn += rng.sample(by_tier[tier], frozen["tier_quota"][tier])

    match = sorted(drawn) == sorted(frozen["task_ids"])
    print(f"  tier populations : {{{', '.join(f'{k}: {len(v)}' for k, v in sorted(by_tier.items()))}}}")
    print(f"  quota            : {frozen['tier_quota']}  seed {frozen['seed']}")
    print(f"  re-derived       : {sorted(drawn)}")
    print(f"  frozen           : {sorted(frozen['task_ids'])}")
    print("\n" + ("REPRODUCED EXACTLY" if match else "MISMATCH"))
    return 0 if match else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", help="verify the frozen artifacts against the recipe")
    sub.add_parser("stages", help="print the derivation chain")
    sub.add_parser("pilot", help="re-derive pilot10 from its seed")
    args = ap.parse_args(argv)
    return {"stages": cmd_stages, "pilot": cmd_pilot}.get(args.cmd, cmd_check)(args)


if __name__ == "__main__":
    sys.exit(main())
