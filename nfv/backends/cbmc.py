#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
CBMC (C Bounded Model Checker) backend: a BOUNDED, program-level checker.

This is the only arm whose tool is designed to find bugs rather than prove their
absence, and it runs in ONE mode: bounded. CBMC unwinds loops a fixed number of
times, so `VERIFICATION SUCCESSFUL` means "no violation within the bound" and is
recorded as an abstention, never as Correct. Reading it as a proof would break
this pipeline's central rule, that no verdict is reported which the tool did not
establish.

The other direction it establishes well, and that is the whole arm. A failure
comes with a concrete trace: an assignment to every input, and the value of every
intermediate, that drives the program to the violated assertion. So a failure IS
a refutation -- not a failure to prove, which is what the other backends report
when they cannot close a goal. Abstaining on it would discard the only evidence
this tool was chosen to produce. The verdict rule is therefore:

    goal assertion violated, no undefined behaviour  =>  Incorrect
    everything else                                  =>  abstain

There is no LLM-driven refutation phase for this backend and no unbounded mode.
Both would be answering a question CBMC already answers directly.

THE CONVENTION (mechanically enforced, see `nfv.agent.c_integrity`)

    // src: source.py:1 | def f(n):
    int f(int n) { ... }                       // the program, transliterated

    // synth: precondition from docstring: "<quoted phrase>"
    int f_pre(int n) { return n >= 0; }        // `1` when nothing is stated

    // synth: postcondition from provided assertion
    int f_post(int n, int return_value) { return return_value >= 0; }

    // synth: goal
    int main(void) {
      int n = nondet_int();
      __CPROVER_assume(f_pre(n));
      __CPROVER_assert(f_post(n, f(n)), "nfv-goal");
      return 0;
    }

The harness is what the other backends get for free from a signature: `main`
chooses an arbitrary input, constrains it to the precondition, and asserts the
postcondition of the result. Refutation negates the asserted expression and adds
`__CPROVER_assume` lines for the witness, which is the same surgery the Dafny arm
performs on `ensures` and `requires` clauses.

THREE HAZARDS, ALL MEASURED ON THIS TOOLCHAIN

1. THE BOUND IS PART OF THE ANSWER. `--unwinding-assertions` makes CBMC check
   that its own unrolling was deep enough, and reports a distinct property when
   it was not. This arm never issues a Correct verdict, so an insufficient bound
   cannot corrupt one; it is recorded all the same, because it is the difference
   between "searched everywhere and found nothing" and "ran out of rope".

2. UNDEFINED BEHAVIOUR MAKES A TRACE MEANINGLESS. A trace that reaches the
   assertion by way of a buffer overrun, a division by zero or an out-of-range
   float-to-int conversion says nothing about the Python program, because the C
   program had no defined behaviour to say it about. Python's arithmetic is
   total where C's is not -- `int(x)` always works, `(long)x` does not -- so
   this is the failure mode the arm meets most often, not a corner case. The
   built-in checks are therefore left ENABLED and named explicitly, and a
   failure of one of them makes the result malformed: the same treatment Dafny's
   `index out of range` gets, and for the same reason. The legacy harness in
   `aux_scripts` disables these; this one must not.

3. PREPROCESSING NEEDS MSVC. Measured: with no `cl.exe` on PATH, CBMC on Windows
   reports `CL Preprocessing failed / PARSING ERROR` and exits 6, which is
   indistinguishable from a syntax error in the artifact unless the environment
   is set up first. `nfv.shared.config` locates the MSVC toolchain, and this
   module refuses to run without it rather than recording an arm's worth of
   phantom type errors.

Self-test:
    python -m nfv.backends.cbmc --selftest
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from nfv.backends._scratch import scratch_dir

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from nfv.backends.dafny import Outcome  # noqa: E402  (shared vocabulary)
from nfv.shared.config import CBMC_PATH, msvc_env  # noqa: E402

# The comment carried by the single goal assertion. Everything that has to find
# the goal -- inversion, the witness install, the reachability gate -- looks for
# this string, so it is defined once here.
GOAL_TAG = "nfv-goal"
REACH_TAG = "nfv-reach"

# Default loop unwinding. Chosen to match the Dafny bounded arm's `--unroll`
# default so the two bounded arms are asked the same question.
DEFAULT_UNWIND = 10

# ---------------------------------------------------------------------------
# THE BOUNDED INPUT DOMAIN.
#
# This prelude is prepended to every artifact by the backend. It is NOT written
# by the model, cannot be edited by the model, and is not part of what the
# integrity audit inspects, because it is a parameter of the arm in exactly the
# way `--unwind` is a parameter of the arm.
#
# It exists because C's integers are bounded and Python's are not. Without it,
# a faithful transliteration of `sum(xs)` overflows a `long` for some input, and
# signed overflow is undefined, so the check comes back inconclusive on almost
# every entry that does arithmetic -- measured, on the first smoke run, as 6 of 8
# entries lost to overflow rather than to anything about the programs.
#
# Restricting the inputs instead is sound for the purpose this arm serves. Every
# value admitted here is a legal Python input, so an execution that violates the
# postcondition inside these bounds violates it for Python too: a refutation
# remains a refutation. What is given up is the other direction, and this arm
# never claimed it -- a clean run was already reported as an abstention, and now
# it means "no violation among inputs of this size" rather than "no violation".
# Making the restriction mechanical and uniform is what keeps it honest: the
# model cannot narrow the domain to escape a counterexample, because the model
# does not get to choose it.
# ---------------------------------------------------------------------------
INT_BOUND = 1000
LEN_BOUND = 6
FLOAT_BOUND = 1000.0
# Printable ASCII. A Python string is a sequence of CHARACTERS, and drawing its
# elements from the integer domain instead admits negative values, which are not
# characters at all -- measured, that produced a refutation on a correct program
# which turned entirely on a control code the benchmark's inputs never contain.
# Modelling the element type correctly is not a concession to the result; it is
# the same obligation the arm already meets for integers and floats.
CHAR_LO, CHAR_HI = 32, 126


def make_prelude(*, len_bound: int = LEN_BOUND, int_bound: int = INT_BOUND,
                 float_bound: float = FLOAT_BOUND) -> str:
    """The bounded input domain, at the given size.

    The defaults are the arm's published parameters and are what every ordinary
    run uses. Widening them is only ever done when the input is PINNED, where it
    is both sound and cheap: enlarging the domain adds executions, so it cannot
    invalidate a counterexample found in the smaller one, and once the input is
    fixed there is no search over the larger domain to pay for. It must not be
    done for a searching run, which would then be exploring a different domain
    from the one the published arm explored.
    """
    return f"""\
// nfv: bounded input domain (arm parameter; not part of the translation)
long nondet_long(void);
int nondet_int(void);
double nondet_double(void);
#define NFV_LEN_MAX {len_bound}
static long nfv_int(void) {{
  long x = nondet_long();
  __CPROVER_assume(x >= -{int_bound}L && x <= {int_bound}L);
  return x;
}}
static double nfv_double(void) {{
  double x = nondet_double();
  __CPROVER_assume(x > -{float_bound} && x < {float_bound});
  return x;
}}
static int nfv_len(void) {{
  int n = nondet_int();
  __CPROVER_assume(n >= 0 && n <= NFV_LEN_MAX);
  return n;
}}
static long nfv_char(void) {{
  long c = nondet_long();
  __CPROVER_assume(c >= {CHAR_LO} && c <= {CHAR_HI});
  return c;
}}
// end nfv prelude
"""


PRELUDE = make_prelude()
_PRELUDE_LINES = PRELUDE.count("\n")

_PROP = re.compile(r"^\[(?P<id>[^\]]+)\]\s+(?:line\s+\d+\s+)?(?P<desc>.*?):\s+"
                   r"(?P<status>SUCCESS|FAILURE)\s*$", re.MULTILINE)

# Built-in checks whose failure indicts the TRANSLATION rather than the program.
_UB_KINDS = ("array_bounds", "pointer_dereference", "pointer_arithmetic",
             "division-by-zero", "division_by_zero", "overflow", "NaN",
             "pointer_primitives", "bit_count", "undefined-shift")


@dataclass
class CbmcResult:
    outcome: Outcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    mode: str = "bounded"
    unroll: int | None = None
    errors: list[str] = field(default_factory=list)
    # Property ids that FAILED, split by what they mean.
    failed_goal: list[str] = field(default_factory=list)
    failed_unwind: list[str] = field(default_factory=list)
    failed_builtin: list[str] = field(default_factory=list)

    @property
    def is_cex(self) -> bool:
        """The goal assertion was violated by a concrete execution.

        Whether that execution says anything about the PYTHON program is a
        separate question, answered by `is_malformed`: a trace that reaches the
        assertion by way of undefined behaviour is an artefact of the C. Both
        are reported, and the caller refuses the verdict when both hold, which
        is the same gate the Dafny arm applies to `index out of range`.
        """
        return (self.outcome is Outcome.VERIFICATION_FAILED
                and bool(self.failed_goal))

    @property
    def has_witness(self) -> bool:
        """Whether CBMC printed the trace that assigns the inputs."""
        return "Counterexample:" in (self.stdout or "") or \
               "State " in (self.stdout or "")

    @property
    def wellformedness_issues(self) -> list[str]:
        """Reasons the artifact, rather than the program, is at fault.

        Only undefined behaviour appears here. An insufficient unwinding is NOT
        a well-formedness problem: it can make a SUCCESS meaningless, but it can
        never invalidate a FAILURE, because a failure is a concrete execution
        that was actually constructed. That case is already handled where the
        outcome is decided -- `VERIFICATION SUCCESSFUL` is emitted by CBMC only
        when the unwinding assertions passed too -- so it must not be allowed to
        suppress a counterexample here.
        """
        if not self.failed_builtin:
            return []
        return ["undefined behaviour on the path to the assertion: "
                + ", ".join(self.failed_builtin[:4])]

    @property
    def bound_insufficient(self) -> bool:
        """The unrolling did not exhaust every loop, so no proof was obtained."""
        return bool(self.failed_unwind)

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
            "failed_goal": self.failed_goal,
            "failed_unwind": self.failed_unwind,
            "failed_builtin": self.failed_builtin,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _invoke(source: str, args: list[str], timeout: float,
            workdir: Path | None, prelude: str | None = None
            ) -> tuple[int | None, str, float]:
    env = msvc_env()
    if env is None:
        raise RuntimeError(
            "MSVC toolchain not found; CBMC cannot preprocess C on Windows "
            "without cl.exe (measured: `CL Preprocessing failed`, exit 6)")
    with scratch_dir(workdir, "nfv_cbmc_") as tmpdir:
        path = tmpdir / "model.c"
        path.write_text((prelude or PRELUDE) + source, encoding="utf-8",
                        newline="\n")
        start = time.time()
        try:
            proc = subprocess.run([CBMC_PATH, str(path), *args],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env, cwd=str(tmpdir))
        except subprocess.TimeoutExpired:
            return None, "", time.time() - start
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), \
            time.time() - start


def _classify_properties(out: str) -> tuple[list[str], list[str], list[str]]:
    """(goal, unwinding, built-in) property ids that FAILED."""
    goal, unwind, builtin = [], [], []
    for m in _PROP.finditer(out):
        if m.group("status") != "FAILURE":
            continue
        pid, desc = m.group("id"), m.group("desc")
        if GOAL_TAG in desc or REACH_TAG in desc:
            goal.append(pid)
        elif "unwind" in pid or "unwinding assertion" in desc:
            unwind.append(pid)
        elif any(k in pid or k in desc for k in _UB_KINDS):
            builtin.append(pid)
        else:
            # An assertion we did not plant and cannot attribute. Treated as a
            # built-in failure, i.e. as a defect in the artifact, because a
            # verdict must never rest on a property whose meaning is unknown.
            builtin.append(pid)
    return goal, unwind, builtin


def run_cbmc(
    source: str,
    *,
    mode: str = "bounded",
    unroll: int | None = None,
    timeout: float = 300.0,
    workdir: str | Path | None = None,
    filter_symbol: str | None = None,
    prelude: str | None = None,
) -> CbmcResult:
    """Bounded-model-check a C artifact written in the convention above.

    Only `mode="bounded"` exists. The other backends offer an unbounded mode in
    which success is a proof; CBMC has no such mode, and manufacturing one by
    raising the unwinding bound until the tool stops complaining would report a
    proof the tool never established. The arm is therefore one-directional in
    the sense that matters: it can say Incorrect on the strength of a concrete
    execution, and it says nothing at all otherwise.
    """
    if mode != "bounded":
        raise ValueError(
            "cbmc is a bounded model checker; there is no unbounded mode. "
            "A run that finds no counterexample has established nothing.")
    if CBMC_PATH is None:
        raise RuntimeError("cbmc not found")

    n = unroll or DEFAULT_UNWIND
    args = ["--function", "main", "--unwind", str(n), "--unwinding-assertions",
            # Hazard 2: these stay ON, and are named explicitly rather than left
            # to the tool's defaults so the arm cannot silently change meaning
            # between CBMC releases. A trace that got to the assertion through
            # undefined behaviour is not evidence about the Python program.
            "--bounds-check", "--pointer-check", "--div-by-zero-check",
            "--signed-overflow-check", "--unsigned-overflow-check",
            # Measured, and the reason this list is not shorter: `int(x)` in
            # Python is total, while `(long)x` in C is undefined once x exceeds
            # the integer range. Without `--conversion-check` a faithful-looking
            # transliteration of `number - int(number)` is "refuted" by an input
            # no Python program would ever see.
            "--conversion-check", "--float-overflow-check", "--nan-check",
            "--trace"]
    rc, out, dur = _invoke(source, args, timeout,
                           Path(workdir) if workdir else None, prelude)
    if rc is None:
        return CbmcResult(Outcome.TIMEOUT, None, out, "", dur, mode=mode, unroll=n)

    if "PARSING ERROR" in out or "CONVERSION ERROR" in out:
        errs = [ln.strip() for ln in out.splitlines()
                if "error:" in ln.lower() or "warning:" in ln.lower()]
        return CbmcResult(Outcome.TYPE_ERROR, rc, out, "", dur, mode=mode,
                          unroll=n, errors=errs[:20])

    goal, unwind_f, builtin = _classify_properties(out)

    if "VERIFICATION SUCCESSFUL" in out:
        # Nothing failed, including the unwinding assertions, so the bound was
        # sufficient and the claim holds on every execution -- not merely on
        # those within the unrolling.
        return CbmcResult(Outcome.VERIFIED, rc, out, "", dur, mode=mode, unroll=n)

    if "VERIFICATION FAILED" in out:
        if goal:
            return CbmcResult(Outcome.VERIFICATION_FAILED, rc, out, "", dur,
                              mode=mode, unroll=n, failed_goal=goal,
                              failed_unwind=unwind_f, failed_builtin=builtin)
        # The goal held; something else did not. Neither direction was
        # established, so this is inconclusive rather than a verdict.
        return CbmcResult(Outcome.TIMEOUT, rc, out, "", dur, mode=mode, unroll=n,
                          failed_unwind=unwind_f, failed_builtin=builtin)

    return CbmcResult(Outcome.TOOL_ERROR, rc, out, "", dur, mode=mode, unroll=n,
                      errors=[out.strip()[-400:]])


def type_check(
    source: str,
    *,
    timeout: float = 120.0,
    workdir: str | Path | None = None,
) -> CbmcResult:
    """Stage-1 gate: does the artifact preprocess, parse and type-check?

    `--show-symbol-table` stops after the front end, so this asks only about
    well-formedness and never pays for a solver call.
    """
    if CBMC_PATH is None:
        raise RuntimeError("cbmc not found")
    rc, out, dur = _invoke(source, ["--show-symbol-table"], timeout,
                           Path(workdir) if workdir else None)
    if rc is None:
        return CbmcResult(Outcome.TIMEOUT, None, out, "", dur, mode="typecheck")
    bad = ("PARSING ERROR" in out or "CONVERSION ERROR" in out
           or "error:" in out.lower())
    errs = [ln.strip() for ln in out.splitlines() if "error:" in ln.lower()]
    return CbmcResult(
        outcome=Outcome.TYPE_ERROR if bad else Outcome.VERIFIED,
        exit_code=rc, stdout=out, stderr="", duration_s=dur,
        mode="typecheck", errors=errs[:20],
    )


_HARNESS = """\
int f(long n) { return %s; }
int f_pre(long n) { return %s; }
int f_post(long n, int return_value) { return return_value >= 0; }

// synth: goal
int main(void) {
  long n = nfv_int();
  __CPROVER_assume(f_pre(n));
  __CPROVER_assert(f_post(n, f(n)), "nfv-goal");
  return 0;
}
"""

_GOOD = _HARNESS % ("n", "n >= 0")
_BUGGY = _HARNESS % ("n - 1", "n >= 0")
# Not a semantic bug but a syntactic one: the artifact must not reach the solver.
_TYPE_ERROR = _HARNESS % ("n + ;", "n >= 0")
# THE case that motivates keeping the built-in checks on, in the form the arm
# meets when it bypasses the bounded input domain: `-n` overflows at LONG_MIN, so
# the faithful transliteration of `abs` is "refuted" by an input Python would
# never produce. Uses `nondet_long` directly, which is exactly what the prelude
# and the integrity audit exist to prevent.
_UB = """\
long f(long n) { return n >= 0 ? n : -n; }
int f_pre(long n) { return 1; }
int f_post(long n, long return_value) { return return_value >= 0; }

// synth: goal
int main(void) {
  long n = nondet_long();
  __CPROVER_assume(f_pre(n));
  __CPROVER_assert(f_post(n, f(n)), "nfv-goal");
  return 0;
}
"""
# The same hazard on floats. Python's `int(x)` is total; C's `(long)x` is
# undefined once x leaves the integer range, so this faithful-looking
# transliteration of `number - int(number)` is "refuted" by an input no Python
# program would ever see. Caught only by `--conversion-check`.
_FLOAT_UB = """\
double truncate_number(double number) { return number - (long)number; }
int truncate_number_pre(double number) { return number > 0.0; }
int truncate_number_post(double number, double return_value) {
  return 0.0 <= return_value && return_value < 1.0;
}

// synth: goal
int main(void) {
  union { long bits; double value; } c;
  c.bits = nondet_long();
  double number = c.value;
  __CPROVER_assume(truncate_number_pre(number));
  __CPROVER_assert(truncate_number_post(number, truncate_number(number)), "nfv-goal");
  return 0;
}
"""
# ...and the same program taking its input from the bounded domain instead. This
# is the check that the prelude actually earns its place: same C, same tool, and
# the undefined behaviour is gone.
_FLOAT_OK = """\
double truncate_number(double number) { return number - (long)number; }
int truncate_number_pre(double number) { return number > 0.0; }
int truncate_number_post(double number, double return_value) {
  return 0.0 <= return_value && return_value < 1.0;
}

// synth: goal
int main(void) {
  double number = nfv_double();
  __CPROVER_assume(truncate_number_pre(number));
  __CPROVER_assert(truncate_number_post(number, truncate_number(number)), "nfv-goal");
  return 0;
}
"""
# A summation over an arbitrary list: the shape that lost 6 of 8 entries to
# overflow before the input domain was bounded.
_SUM = """\
long total(long xs[], int n) {
  long s = 0;
  for (int i = 0; i < n; i++) s += xs[i];
  return s;
}
int total_pre(long xs[], int n) { return n >= 0; }
int total_post(long xs[], int n, long return_value) {
  return n != 0 || return_value == 0;
}

// synth: goal
int main(void) {
  int n = nfv_len();
  long xs[NFV_LEN_MAX];
  for (int i = 0; i < n; i++) xs[i] = nfv_int();
  __CPROVER_assume(total_pre(xs, n));
  __CPROVER_assert(total_post(xs, n, total(xs, n)), "nfv-goal");
  return 0;
}
"""
_LOOPY = """\
int nondet_int(void);

int f(int n) {
  int s = 0;
  for (int i = 0; i < n; i++) s += 1;
  return s;
}
int f_pre(int n) { return n >= 0 && n <= 5; }
int f_post(int n, int return_value) { return return_value == n; }

// synth: goal
int main(void) {
  int n = nondet_int();
  __CPROVER_assume(f_pre(n));
  __CPROVER_assert(f_post(n, f(n)), "nfv-goal");
  return 0;
}
"""


def selftest() -> int:
    """Assert the tool behaves as the design assumes. Fail loudly if not."""
    failures = 0
    print(f"cbmc: {CBMC_PATH}")
    print(f"preprocessor environment: {'found' if msvc_env() else 'NOT FOUND'}\n")

    cases = [
        ("postcondition holds", _GOOD, Outcome.VERIFIED),
        ("postcondition violated -> Cex", _BUGGY, Outcome.VERIFICATION_FAILED),
        ("broken translation -> type error", _TYPE_ERROR, Outcome.TYPE_ERROR),
    ]
    for name, src, expected in cases:
        res = run_cbmc(src, timeout=300.0)
        ok = res.outcome is expected
        failures += 0 if ok else 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<38} -> {res.outcome.value:<12} "
              f"({res.duration_s:.0f}s)")
        if not ok:
            print(f"         expected {expected.value}; errors={res.errors[:2]}")

    # A counterexample must carry the trace, or it cannot be triaged.
    wit = run_cbmc(_BUGGY, timeout=300.0)
    ok = wit.is_cex and wit.has_witness
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'Cex carries a concrete trace':<38} -> "
          f"is_cex={wit.is_cex}, trace={wit.has_witness}")

    # Undefined behaviour must never be reported as a refutation.
    ub = run_cbmc(_UB, timeout=300.0)
    ok = ub.is_cex and ub.is_malformed
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'UB is malformed, NOT a refutation':<38} -> "
          f"is_cex={ub.is_cex}, malformed={ub.is_malformed}, {ub.failed_builtin}")

    # The same hazard on floats, which is the form it actually takes on this
    # benchmark. Regressing `--conversion-check` would silently turn this into a
    # false Incorrect on a correct program.
    fub = run_cbmc(_FLOAT_UB, timeout=300.0)
    ok = fub.is_cex and fub.is_malformed
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'float->int UB is malformed too':<38} -> "
          f"is_cex={fub.is_cex}, malformed={fub.is_malformed}, {fub.failed_builtin}")

    # THE JUSTIFICATION FOR THE PRELUDE: the same two programs, taking their
    # inputs from the bounded domain instead, are conclusive rather than lost.
    for name, src in [("...bounded input fixes the float case", _FLOAT_OK),
                      ("...and a summation over a list", _SUM)]:
        res = run_cbmc(src, timeout=300.0)
        ok = not res.is_malformed
        failures += 0 if ok else 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<38} -> {res.outcome.value}, "
              f"malformed={res.is_malformed} {res.failed_builtin}")

    # THE critical property of a bounded tool: when the bound is too small it
    # must say so, and must not be read as either verdict.
    small = run_cbmc(_LOOPY, unroll=2, timeout=300.0)
    ok = small.outcome is not Outcome.VERIFIED and small.bound_insufficient
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'insufficient bound is NOT a proof':<38} -> "
          f"{small.outcome.value}, unwind_failed={small.failed_unwind}")

    # ...and with a bound that exhausts the loop, the same artifact verifies.
    big = run_cbmc(_LOOPY, unroll=20, timeout=300.0)
    ok = big.outcome is Outcome.VERIFIED
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'...and IS a proof once exhausted':<38} -> "
          f"{big.outcome.value}")

    for name, src, expected in [
        ("typecheck: well-formed", _BUGGY, Outcome.VERIFIED),
        ("typecheck: broken", _TYPE_ERROR, Outcome.TYPE_ERROR),
    ]:
        res = type_check(src)
        ok = res.outcome is expected
        failures += 0 if ok else 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<38} -> {res.outcome.value}")

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures} case(s)")
        return 1
    print("SELFTEST PASSED")
    print("  goal assertion fails, no UB   => Cex        => Incorrect")
    print("  unwinding assertion fails     => bound too small => abstain")
    print("  bounds/pointer/overflow fails => malformed  => abstain")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    ap.print_help()
