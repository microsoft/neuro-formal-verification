"""Join the killing test back onto the benchmark entries.

`nl2post_safe.json` deliberately drops the killing test -- the benchmark is
static-only and nothing is ever executed. That is the right default, but it
makes one question unaskable: how much of refutation's failure is failure to
*find* a witness, as opposed to failure to *prove* one?

This module re-attaches the killing test from the upstream verified subset for
that ablation only. It is never used by the ordinary pipeline, and it is only
defined for mutants -- a correct entry has no killing test by construction.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "paper" / "data" / "upstream" / "results" / "nl2post_verified_subset.json"


def _key(e: dict) -> tuple:
    return (e["task_id"], e["entry_point"], e.get("response_num"))


def load_killing_tests() -> dict[tuple, list]:
    """Return {(task_id, entry_point, response_num): killing_test}."""
    if not UPSTREAM.exists():
        return {}
    d = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    entries = d["entries"] if isinstance(d, dict) else d
    out = {}
    for e in entries:
        if e.get("label") != "incorrect":
            continue
        kt = e.get("killing_test")
        if kt is not None:
            out[_key(e)] = kt
    return out


def attach(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Attach `killing_test` to every mutant. Returns (entries, missing_ids).

    Entries are mutated in place. A mutant with no killing test is reported
    rather than silently skipped: a partial oracle set would understate the
    oracle's advantage and quietly bias the ablation.
    """
    kt = load_killing_tests()
    missing = []
    for e in entries:
        if e.get("label") != "incorrect":
            continue
        v = kt.get(_key(e))
        if v is None:
            missing.append(e["entry_id"])
        else:
            e["killing_test"] = v
    return entries, missing


def _selftest() -> int:
    import sys
    data = json.loads((ROOT / "paper" / "data" / "nl2post_safe.json")
                      .read_text(encoding="utf-8"))
    entries = data["entries"]
    mutants = [e for e in entries if e["label"] == "incorrect"]
    _, missing = attach(entries)
    have = [e for e in mutants if "killing_test" in e]
    print(f"mutants={len(mutants)} joined={len(have)} missing={len(missing)}")
    if missing:
        print("  missing: " + ", ".join(missing[:10]))
    ok = len(have) == len(mutants)
    print("PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
