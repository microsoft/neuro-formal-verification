"""Pin a KNOWN killing input into a C harness, so CBMC is handed the bug.

This is the bounded-model-checking counterpart of `oracle_witness.py`, and it
exists to separate the same two abilities:

  1. can the checker FIND an input on which the program is wrong?
  2. given that input, does the translated C actually break on it?

The ordinary CBMC arm answers both at once, and its dominant failure is an
abstention -- "no violating execution found within the unwinding bound" -- which
is unattributable: the bound may have been too small, the search domain too
narrow (`nfv_int` admits |x| <= 1000, `nfv_len` admits lists of at most six),
or the translation may simply not have the bug. Pinning the dataset's own
killing test removes (1) entirely. What remains is a statement about the
TRANSLATION: a faithful C rendering of a program known to fail on this input
must fail on it here too.

WHY THIS IS SOUND IN THE DIRECTION IT CLAIMS

Adding `__CPROVER_assume` over the inputs only REMOVES executions. So

  * a counterexample found here is still a concrete execution of the harness,
    satisfying the stated precondition, that violates the translated
    postcondition -- exactly what the unpinned arm reports, on a smaller domain;
  * over-constraining cannot manufacture one. If the pinned constraints are
    contradictory there is no execution at all, CBMC reports success, and the
    entry ABSTAINS. Vacuity therefore costs recall and can never buy a
    refutation. The reachability probe below exists to tell that case apart from
    a genuine "the translation does not break on this input", not to keep the
    arm sound.

WHAT THE INSTALL IS ALLOWED TO DO

Nothing except insert assumptions. `install` returns the model with a block of
`__CPROVER_assume` lines immediately above the goal assertion, and it verifies
mechanically that deleting exactly those lines gives back the original text
byte for byte. The program, the precondition function, the postcondition
function and the goal assertion are untouched by construction rather than by
audit -- a stronger guarantee than the proof-side arm can offer, where the
witness is spliced into a signature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nfv.agent.refute import InputGuess, parse_guess
from nfv.agent.translate import number_source
from nfv.backends.cbmc import (CHAR_HI, CHAR_LO, DEFAULT_UNWIND, FLOAT_BOUND,
                               GOAL_TAG, INT_BOUND, LEN_BOUND, PRELUDE,
                               REACH_TAG, make_prelude)
from nfv.backends.registry import Backend, get as get_backend
from nfv.shared.copilot_client import CopilotChat

MARKER = "synth: oracle witness"

SYSTEM = """\
You translate a CONCRETE INPUT into C assumptions. You do not search for one,
and you do not judge whether it is a good input: it has already been
established, by execution, that this input breaks the program.

WHAT YOU ARE GIVEN
The exact Python argument values the function was called with when it failed, in
positional order, and a C translation of that function checked by CBMC. In its
`main`, arbitrary inputs are drawn from `nfv_int()`, `nfv_double()`,
`nfv_char()` and `nfv_len()`, constrained with `__CPROVER_assume`, and the goal
is a single `__CPROVER_assert` tagged "nfv-goal".

WHAT TO PRODUCE
The conditions that pin those arguments EXACTLY, written against the LOCAL
VARIABLES OF `main` -- not the parameter names of the function, unless they
happen to be the same. One condition per line, as a plain C expression, with no
`__CPROVER_assume` wrapper and no semicolon. They will be inserted verbatim as
`__CPROVER_assume(<line>);` immediately above the goal assertion.

    n == 3
    len == 2 && xs[0] == 1 && xs[1] == 1

RULES
- Reproduce the GIVEN values. Do not simplify them, do not generalise them to a
  domain, and do not substitute an input you think is better.
- Pin the value COMPLETELY. For a list or a string, fix the length variable AND
  every element up to that length. A partially pinned input leaves CBMC a choice
  and the run may then fail, or fail to fail, for a reason unrelated to the bug.
- A Python STRING is modelled as an array of character CODES. Write the codes
  (`s[0] == 97` for 'a'), or the C character literals (`s[0] == 'a'`), but never
  a string literal.
- Match the harness's own type model. If `main` holds the value in a `double`,
  write `3.0`; if a Python list of numbers became `long xs[NFV_LEN_MAX]` plus a
  length, constrain both.
- Say nothing about the return value, the postcondition, or any helper function.
  Constrain ONLY the input variables.
- The harness's bounded input domain has already been widened to accommodate
  this input, so NFV_LEN_MAX and the integer bound are large enough for it.
  Encode the value as given. Never clip, truncate or shorten it to fit.

OUTPUT
A fenced ```c block containing ONLY those condition lines. Then one line of
prose naming the input you encoded.

If the input genuinely cannot be expressed against this harness, output an empty
fenced block and explain why in one line.
"""

USER_TEMPLATE = """\
Python source:
```python
{numbered_source}
```

Entry point: {entry_point}

The postcondition the program is supposed to satisfy:
```python
{postcondition}
```

THE INPUT THAT BREAKS IT. These are the positional arguments to
`{entry_point}`, exactly as they were passed when the program failed:
```python
{entry_point}({args})
```

The C harness the conditions will be inserted into, immediately above the
`"nfv-goal"` assertion:
```c
{model}
```

Write the conditions that pin exactly this input.
"""

# The goal assertion, as a whole statement. `re.DOTALL` because a long
# postcondition application is routinely wrapped across lines.
_ASSERT_STMT = re.compile(r"__CPROVER_assert\s*\(.*?\)\s*;", re.DOTALL)


def _goal_span(model: str) -> tuple[int, int] | None:
    """`(start, end)` of the goal assertion statement, or None."""
    for m in _ASSERT_STMT.finditer(model):
        if GOAL_TAG in m.group(0):
            return m.start(), m.end()
    return None


@dataclass
class Install:
    ok: bool
    model: str = ""
    reason: str = ""
    lines: list[str] = field(default_factory=list)


def install(model: str, conditions: list[str]) -> Install:
    """Insert `__CPROVER_assume(c);` for each condition above the goal.

    The inserted block is fenced by `MARKER` comments so that it can be removed
    again exactly; the round trip is checked here rather than trusted, because
    an install that silently perturbed the harness would make the arm measure
    something other than the frozen translation.
    """
    conds = [c.strip().rstrip(";").strip() for c in conditions]
    conds = [c for c in conds if c]
    if not conds:
        return Install(False, reason="no conditions to install")
    bad = [c for c in conds if "__CPROVER_assert" in c or ";" in c
           or c.startswith("//")]
    if bad:
        return Install(False, reason=f"conditions are not expressions: {bad[:2]}")
    span = _goal_span(model)
    if span is None:
        return Install(False, reason=f"no `{GOAL_TAG}` assertion found")
    start = model.rfind("\n", 0, span[0]) + 1
    indent = model[start:span[0]]
    if indent.strip():
        # The assertion does not begin its line; inserting above it would move
        # code rather than add to it.
        return Install(False, reason="the goal assertion does not start its line")

    block = [f"{indent}// {MARKER}: the input on which the program is known to fail"]
    block += [f"{indent}__CPROVER_assume({c});" for c in conds]
    block += [f"{indent}// end {MARKER}"]
    new = model[:start] + "\n".join(block) + "\n" + model[start:]

    if _strip_installed(new) != model:
        return Install(False, reason="install did not round-trip")
    return Install(True, model=new, lines=block[1:-1])


def _strip_installed(model: str) -> str:
    """Delete the installed block, marker comments included."""
    out, drop = [], False
    for ln in model.splitlines(keepends=True):
        s = ln.strip()
        if s.startswith(f"// {MARKER}"):
            drop = True
            continue
        if drop and s.startswith(f"// end {MARKER}"):
            drop = False
            continue
        if drop:
            continue
        out.append(ln)
    return "".join(out)


def probe(model: str) -> str:
    """The same harness, asking only whether the pinned inputs admit anything.

    The goal assertion is replaced by an unconditional one. CBMC reporting it
    FAILED means some execution reaches that point, so the assumptions are
    satisfiable; reporting SUCCESS means they are not, and the pinned run's
    silence is vacuous rather than informative.
    """
    span = _goal_span(model)
    if span is None:
        return model
    return model[:span[0]] + f'__CPROVER_assert(0, "{REACH_TAG}");' + model[span[1]:]


def required_bounds(killing_test) -> dict:
    """The bounded input domain this killing input needs, and why.

    The prelude restricts what an arbitrary input can be -- integers within
    +/-1000, sequences of at most NFV_LEN_MAX elements -- because C's arithmetic
    is bounded where Python's is not, and because an unrestricted domain is
    expensive to SEARCH. Neither reason survives pinning:

      * enlarging the domain only adds executions, so a counterexample found in
        the widened harness is still a concrete execution of the program on a
        legal Python input, and
      * there is nothing left to search. The input is fixed, so the widened
        harness has essentially one execution to explore, and the cost that
        motivated the small bound is not incurred.

    So rather than declare an oversized witness inexpressible, the arm widens
    the domain to fit it exactly, and records what it widened. The widening is
    per entry and applies ONLY to the pinned run: the unpinned arm keeps the
    published bounds, or the two would be exploring different domains.

    Returns `{len, int, float, unroll, widened}` where `widened` lists the
    parameters that had to move.
    """
    need_len, need_int, need_float = 0, 0, 0.0

    def walk(v) -> None:
        nonlocal need_len, need_int, need_float
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int):
            need_int = max(need_int, abs(v))
        elif isinstance(v, float):
            need_float = max(need_float, abs(v))
        elif isinstance(v, str):
            need_len = max(need_len, len(v))
        elif isinstance(v, (list, tuple)):
            need_len = max(need_len, len(v))
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            need_len = max(need_len, len(v))
            for k, x in v.items():
                walk(k)
                walk(x)

    args = killing_test if isinstance(killing_test, list) else [killing_test]
    for a in args:
        walk(a)

    widened = []
    len_b = LEN_BOUND
    if need_len > len_b:
        len_b = need_len
        widened.append(f"NFV_LEN_MAX {LEN_BOUND} -> {len_b}")
    int_b = INT_BOUND
    if need_int > int_b:
        # Headroom of one decimal order, so that a faithful translation which
        # adds or scales the input does not overflow the assumed range and lose
        # the run to undefined behaviour on arithmetic the Python does exactly.
        int_b = 10 ** len(str(need_int))
        widened.append(f"integer bound {INT_BOUND} -> {int_b}")
    float_b = FLOAT_BOUND
    if need_float >= float_b:
        float_b = float(10 ** len(str(int(need_float))))
        widened.append(f"float bound {FLOAT_BOUND} -> {float_b}")
    # A loop over a longer sequence needs a correspondingly deeper unrolling, or
    # the execution that exhibits the bug is cut off before it reaches the end
    # of the input. Two spare iterations cover the usual `i <= n` shapes.
    unroll = max(DEFAULT_UNWIND, need_len + 2)

    return {"len": len_b, "int": int_b, "float": float_b, "unroll": unroll,
            "widened": widened, "need_len": need_len, "need_int": need_int}


def format_args(killing_test) -> str:
    """Render the stored argument list as a Python call argument string."""
    if not isinstance(killing_test, list):
        return repr(killing_test)
    return ", ".join(repr(a) for a in killing_test)


async def render(
    chat: CopilotChat,
    *,
    killing_test,
    python_source: str,
    postcondition: str,
    entry_point: str,
    model: str,
    backend: Backend | str = "cbmc",
    timeout: float = 600.0,
) -> InputGuess:
    """Encode the given killing input as conditions over `main`'s variables."""
    bk = get_backend(backend) if isinstance(backend, str) else backend
    user = USER_TEMPLATE.format(
        numbered_source=number_source(python_source),
        entry_point=entry_point,
        postcondition=postcondition.strip(),
        args=format_args(killing_test),
        model=model,
    )
    resp = await chat.ask(SYSTEM, user, timeout=timeout)
    return parse_guess(resp.text, bk)


_HARNESS = """\
long f(long n) { return n; }
int f_pre(long n) { return n >= 0; }
int f_post(long n, long return_value) { return return_value > n; }
// synth: goal
int main(void) {
  long n = nfv_int();
  __CPROVER_assume(f_pre(n));
  __CPROVER_assert(f_post(n, f(n)), "nfv-goal");
  return 0;
}
"""


def _selftest() -> int:
    results = []

    def case(name: str, ok: bool) -> None:
        results.append(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")

    ins = install(_HARNESS, ["n == 3"])
    case("a condition installs", ins.ok)
    case("the assume lands above the goal",
         ins.model.index("__CPROVER_assume(n == 3);") < ins.model.index(GOAL_TAG))
    case("the original precondition survives",
         "__CPROVER_assume(f_pre(n));" in ins.model)
    case("the install round-trips", _strip_installed(ins.model) == _HARNESS)
    case("the goal assertion is untouched",
         '__CPROVER_assert(f_post(n, f(n)), "nfv-goal");' in ins.model)

    multi = install(_HARNESS, ["len == 2", "xs[0] == 1 && xs[1] == 1", ""])
    case("empty conditions are dropped", multi.ok and len(multi.lines) == 2)

    case("a statement is refused", not install(_HARNESS, ["n = 3; f(n)"]).ok)
    case("an assertion is refused",
         not install(_HARNESS, ['__CPROVER_assert(0, "x")']).ok)
    case("no conditions is refused", not install(_HARNESS, ["  "]).ok)
    case("an untagged harness is refused",
         not install(_HARNESS.replace("nfv-goal", "other"), ["n == 3"]).ok)

    p = probe(ins.model)
    case("the probe replaces the goal", GOAL_TAG not in p and REACH_TAG in p)
    case("the probe keeps the pinned assumptions",
         "__CPROVER_assume(n == 3);" in p)

    wrapped = _HARNESS.replace(
        '  __CPROVER_assert(f_post(n, f(n)), "nfv-goal");',
        '  __CPROVER_assert(f_post(n,\n                          f(n)), "nfv-goal");')
    w = install(wrapped, ["n == 3"])
    case("a wrapped goal assertion is handled", w.ok and _strip_installed(w.model) == wrapped)

    case("arguments render as Python literals",
         format_args([[1, 2], "a", True]) == "[1, 2], 'a', True")

    case("an in-domain input needs no widening",
         required_bounds([3, [1, 2], "ab"])["widened"] == [])
    b = required_bounds(["(()()) ((())) () ((())()())"])
    case("a 27-character string widens the length bound", b["len"] == 27)
    case("and deepens the unrolling", b["unroll"] == 29)
    case("a long list widens the length bound",
         required_bounds([[0] * 20])["len"] == 20)
    case("a big integer widens the integer bound with headroom",
         required_bounds([123456])["int"] == 1000000)
    case("a nested long list is seen",
         required_bounds([[[1] * 9, 2]])["len"] == 9)
    case("booleans are not integers",
         required_bounds([True, False])["widened"] == [])
    case("the default unrolling is never lowered",
         required_bounds([[1, 2]])["unroll"] == DEFAULT_UNWIND)
    case("a widened prelude carries the new bound",
         "#define NFV_LEN_MAX 27" in make_prelude(len_bound=27))
    case("an unwidened prelude is the published one",
         make_prelude() == PRELUDE)

    bad = len(results) - sum(results)
    print()
    if bad:
        print(f"SELFTEST FAILED: {bad} case(s)")
        return 1
    print(f"SELFTEST PASSED ({len(results)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
