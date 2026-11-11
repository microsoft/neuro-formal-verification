#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Dafny backend: a PURE, deterministic verifier wrapper. Contains no LLM calls.

The verifier is the only oracle in this pipeline, so this module is deliberately
dumb: it runs `dafny`, classifies the outcome from exit code and output, and
returns it. It never interprets, never guesses, and never decides a label.

Two modes, with different meanings — this distinction is the heart of the design:

  BOUNDED (`/loopUnroll:N`)
      Loop unrolling *under-approximates* executions: paths needing more than N
      iterations are cut off. Measured behaviour on a bug reachable only at >=5
      iterations:
          no unroll   -> FAIL      (sound-for-proof mode)
          unroll 3    -> VERIFIED  (bug beyond bound, missed)
          unroll 10   -> FAIL      (bug within bound, real)
      Therefore a failure here is a genuine counterexample => Incorrect, while a
      "verified" here proves NOTHING about correctness. Treating bounded-verified
      as Correct would be a soundness bug; it only means "no bug within N".

  UNBOUNDED (no unrolling)
      Full deductive verification. `verified` is a real proof => Correct.
      A failure is NOT evidence of a bug: it may just be a missing invariant, so
      it maps to abstain, never to Incorrect.

Self-test:
    python nfv/backends/dafny.py --selftest
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from nfv.backends._scratch import scratch_dir

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from nfv.shared.config import (  # noqa: E402
    DAFNY_EXIT_VERIFICATION_ERROR,
    DAFNY_EXIT_VERIFIED,
    DAFNY_PATH,
    Z3_PATH,
)


class Outcome(str, Enum):
    """What the verifier said. NOT a label — mapping to a label is done elsewhere."""

    VERIFIED = "verified"            # proof succeeded
    VERIFICATION_FAILED = "failed"   # assertion/postcondition unproven
    TYPE_ERROR = "type_error"        # did not parse or resolve
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"


@dataclass
class DafnyResult:
    outcome: Outcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    mode: str
    unroll: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def is_cex(self) -> bool:
        """True only for a bounded-mode refutation."""
        return self.mode == "bounded" and self.outcome is Outcome.VERIFICATION_FAILED

    @property
    def has_witness(self) -> bool:
        """Whether Dafny extracted a concrete counterexample state.

        A bounded failure WITHOUT a witness is weaker evidence: it may reflect
        incomplete proof automation (e.g. an existential the solver could not
        witness) rather than a genuine bug. Recorded so such cases are visible
        during triage instead of being silently counted as refutations.
        """
        return "Related counterexample:" in (self.stdout or "") + (self.stderr or "")

    @property
    def wellformedness_issues(self) -> list[str]:
        """Errors that indicate a MALFORMED MODEL rather than a program bug.

        Dafny reports partiality violations (index out of range, division by
        zero, subset-type violations, precondition of a function call) even when
        they occur while checking that a *specification* is well-defined. Such a
        failure says the translation is broken -- e.g. an `ensures` that indexes
        past the end of a sequence -- not that the program violates its spec.

        Treating these as counterexamples would manufacture "Incorrect" verdicts
        out of translation defects, so the pipeline abstains on them instead.
        """
        text = (self.stdout or "") + (self.stderr or "")
        marks = [
            "index out of range",
            "possible division by zero",
            "value does not satisfy the subset constraints",
            "cannot prove termination",
            "is not allowed to be non-deterministic",
            "function precondition could not be proved",
            "destructor",
        ]
        found = [m for m in marks if m in text]
        if "CheckWellFormed" in text and not found:
            found.append("CheckWellFormed")
        return found

    @property
    def is_malformed(self) -> bool:
        return bool(self.wellformedness_issues)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "mode": self.mode,
            "unroll": self.unroll,
            "errors": self.errors,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _parse_errors(output: str) -> list[str]:
    return [ln.strip() for ln in output.splitlines() if ": Error:" in ln]


def _classify(exit_code: int, output: str) -> Outcome:
    # Resolution/type failures are reported distinctly and must not be confused
    # with a verification failure: only the latter can ever be a counterexample.
    if ("resolution/type errors" in output or "Parse Error" in output
            or (exit_code == 2 and re.search(
                r"(?m)^[1-9]\d* parse errors? detected(?: in [^\r\n]+)?\s*$", output))):
        return Outcome.TYPE_ERROR
    if exit_code == DAFNY_EXIT_VERIFIED:
        if "verifier finished with" in output and " 0 errors" in output:
            return Outcome.VERIFIED
        if _parse_errors(output):
            return Outcome.VERIFICATION_FAILED
        return Outcome.VERIFIED
    if exit_code == DAFNY_EXIT_VERIFICATION_ERROR:
        return Outcome.VERIFICATION_FAILED
    return Outcome.TOOL_ERROR


def run_dafny(
    source: str,
    *,
    mode: str = "unbounded",
    unroll: int | None = None,
    timeout: float = 120.0,
    workdir: str | Path | None = None,
    filter_symbol: str | None = None,
) -> DafnyResult:
    """Verify Dafny `source`.

    mode="bounded"  -> loop unrolling; a failure is a counterexample.
    mode="unbounded"-> full proof; `verified` is a proof, a failure is inconclusive.

    `filter_symbol` restricts verification to a single named declaration. It is
    used to ask a focused question about one probe method without paying for --
    or being confused by -- the rest of the file's obligations.
    """
    if mode not in ("bounded", "unbounded"):
        raise ValueError(f"unknown mode: {mode}")
    if DAFNY_PATH is None:
        raise RuntimeError("dafny not found")
    if mode == "bounded" and unroll is None:
        unroll = 5

    with scratch_dir(workdir, "nfv_dafny_") as tmpdir:
        path = tmpdir / "model.dfy"
        path.write_text(source, encoding="utf-8", newline="\n")

        cmd = [DAFNY_PATH, "verify"]
        if Z3_PATH:
            cmd += ["--solver-path", Z3_PATH]
        if mode == "bounded":
            cmd += ["--boogie", f"/loopUnroll:{unroll}"]
            # Extract a concrete witness so every reported Cex is triageable. A
            # bounded failure with NO extractable witness is more likely a proof
            # -automation artifact (e.g. an unwitnessed `exists`) than a real bug.
            cmd += ["--extract-counterexample"]
        if filter_symbol:
            cmd += [f"--filter-symbol={filter_symbol}"]
        cmd.append(str(path))

        import time

        start = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return DafnyResult(
                outcome=Outcome.TIMEOUT,
                exit_code=None,
                stdout="",
                stderr="",
                duration_s=time.time() - start,
                mode=mode,
                unroll=unroll,
            )
        duration = time.time() - start
        combined = (proc.stdout or "") + (proc.stderr or "")
        return DafnyResult(
            outcome=_classify(proc.returncode, combined),
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=duration,
            mode=mode,
            unroll=unroll,
            errors=_parse_errors(combined),
        )


def type_check(
    source: str,
    *,
    timeout: float = 60.0,
    workdir: str | Path | None = None,
) -> DafnyResult:
    """Stage-1 gate: resolve/type-check only, no proof attempt.

    `dafny resolve` exits 0 when well-typed and 2 on resolution/type errors, so it
    cleanly separates "does not compile" from "does not verify". This is the only
    feedback the translator is ever allowed to see.
    """
    if DAFNY_PATH is None:
        raise RuntimeError("dafny not found")

    import time

    with scratch_dir(workdir, "nfv_dafny_") as tmpdir:
        path = tmpdir / "model.dfy"
        path.write_text(source, encoding="utf-8", newline="\n")

        start = time.time()
        try:
            proc = subprocess.run(
                [DAFNY_PATH, "resolve", str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return DafnyResult(Outcome.TIMEOUT, None, "", "", time.time() - start, "typecheck")

        combined = (proc.stdout or "") + (proc.stderr or "")
        outcome = Outcome.VERIFIED if proc.returncode == 0 else Outcome.TYPE_ERROR
        return DafnyResult(
            outcome=outcome,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=time.time() - start,
            mode="typecheck",
            errors=_parse_errors(combined),
        )


_SELFTEST_GOOD = """method Good(n: int) returns (r: int)
  ensures r >= 0
{
  r := if n >= 0 then n else -n;
}
"""

_SELFTEST_BAD = """method Bad(n: int) returns (r: int)
  ensures r >= 0
{
  r := n;
}
"""

_SELFTEST_DEEP = """method Deep(n: int) returns (r: int)
  ensures r < 5
{
  var i := 0;
  r := 0;
  while i < n
  {
    r := r + 1;
    i := i + 1;
  }
}
"""

_SELFTEST_ILLFORMED = """method Oops(n: int) returns (r: int)
{
  r := undefined_name;
}
"""


def selftest() -> int:
    """Assert the verifier behaves as the design assumes. Fail loudly if not."""
    cases = [
        ("correct program, unbounded", _SELFTEST_GOOD, "unbounded", None, Outcome.VERIFIED),
        ("incorrect program, unbounded", _SELFTEST_BAD, "unbounded", None, Outcome.VERIFICATION_FAILED),
        ("incorrect program, bounded", _SELFTEST_BAD, "bounded", 5, Outcome.VERIFICATION_FAILED),
        # Under-approximation: the bug needs >=5 iterations.
        ("deep bug, unroll 3 (must MISS)", _SELFTEST_DEEP, "bounded", 3, Outcome.VERIFIED),
        ("deep bug, unroll 10 (must FIND)", _SELFTEST_DEEP, "bounded", 10, Outcome.VERIFICATION_FAILED),
        ("ill-formed program", _SELFTEST_ILLFORMED, "unbounded", None, Outcome.TYPE_ERROR),
    ]
    failures = 0
    print(f"dafny: {DAFNY_PATH}")
    print(f"z3   : {Z3_PATH}")
    print()
    for name, src, mode, unroll, expected in cases:
        res = run_dafny(src, mode=mode, unroll=unroll)
        ok = res.outcome is expected
        failures += 0 if ok else 1
        flag = "OK  " if ok else "FAIL"
        print(f"  [{flag}] {name:<34} -> {res.outcome.value:<12} "
              f"(exit={res.exit_code}, {res.duration_s:.1f}s)")
        if not ok:
            print(f"         expected {expected.value}")

    # Stage-1 gate: type errors must be distinguishable from proof failures.
    for name, src, expected in [
        ("typecheck: well-typed but unproven", _SELFTEST_BAD, Outcome.VERIFIED),
        ("typecheck: ill-formed", _SELFTEST_ILLFORMED, Outcome.TYPE_ERROR),
    ]:
        res = type_check(src)
        ok = res.outcome is expected
        failures += 0 if ok else 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<34} -> {res.outcome.value:<12} "
              f"(exit={res.exit_code}, {res.duration_s:.1f}s)")
    print()
    if failures:
        print(f"SELFTEST FAILED: {failures} case(s)")
        return 1
    print("SELFTEST PASSED")
    print("  bounded failure  => counterexample => Incorrect")
    print("  bounded verified => proves nothing (no bug within bound only)")
    print("  unbounded verified => proof => Correct")
    print("  unbounded failure  => inconclusive => abstain, never Incorrect")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    ap.print_help()
