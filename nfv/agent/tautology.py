# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Vacuity gate: reject a translated postcondition that asserts nothing.

Motivation, from the record rather than from theory. Every unsound proof this
project has produced -- `HumanEval_133`, `151`, `116`, stable across two prompts
and two budgets -- comes from ONE channel: the translation flattens a signature,
and a conjunct that was discriminating in Python becomes trivially true in the
target. `isinstance(x, int)` against a Python value is a real test; `IsInt(x)`
against a Dafny `x: int` is a tautology. The proof is then valid about a program
that can no longer express the bug.

Detection is mechanical and uses the verifier itself, not a syntactic denylist:

    for each top-level conjunct C of the entry's `ensures`,
    ask whether C is provable with NO program at all --
    a lemma over universally quantified inputs AND output, under the entry's
    own preconditions, with an empty body.

If the verifier proves that, then C holds for every value the entry could
possibly return, so it constrains the program in no way. That is exactly what a
vacuous conjunct is, and it is decided by the same oracle that decides the
proof, so the gate inherits the verifier's soundness rather than a heuristic's.

Note the gate keeps `requires`: a conjunct implied by the precondition alone is
equally vacuous with respect to the body.

The gate ABSTAINS the entry. It never converts to a verdict: a vacuous conjunct
means the translation is uninformative, not that the program is wrong.

Self-test (string handling only, no verifier needed):
    python -m nfv.agent.tautology
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from nfv.agent import refute
from nfv.backends import dafny

TAUT_PROBE = "NfvVacuityProbe"

# ---------------------------------------------------------------------------
# Gate A: type-test conjuncts on the PYTHON side.
#
# The stored postconditions of `HumanEval_133` and `151` are
#
#     assert isinstance(return_value, int) and return_value >= 0
#
# and the translations of both are `returns (r: int) ensures r >= 0`. The
# isinstance conjunct did not become a vacuous Dafny expression -- it vanished,
# absorbed into the type declaration. So there is nothing on the target side to
# probe, and the verifier-based gate below cannot see it. It has to be caught
# where it still exists, in the Python specification.
#
# Why this is the right call rather than a technicality: in Python that conjunct
# is a real test, and it is exactly the test the mutant fails. Once the
# translator commits to `seq<int> -> int`, the bug is inexpressible and the
# remaining conjunct is provable. The verdict would be a valid proof about a
# program that cannot exhibit the defect, which is worse than no verdict.
# ---------------------------------------------------------------------------

_TYPE_FUNCS = {"isinstance", "issubclass"}
_QUANTIFIERS = {"all", "any"}


def _is_type_test(node: ast.AST) -> bool:
    """Whether an expression tests only the TYPE of something."""
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name in _TYPE_FUNCS:
            return True
        if name in _QUANTIFIERS and node.args:
            arg = node.args[0]
            if isinstance(arg, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                return _is_type_test(arg.elt)
            return False
        return False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _is_type_test(node.operand)
    if isinstance(node, ast.BoolOp):
        return all(_is_type_test(v) for v in node.values)
    if isinstance(node, ast.Compare):
        # `type(x) == int` / `type(x) is int`
        def _is_type_call(e):
            return (isinstance(e, ast.Call) and isinstance(e.func, ast.Name)
                    and e.func.id == "type")
        return _is_type_call(node.left) or any(_is_type_call(c) for c in node.comparators)
    return False


def python_conjuncts(postcondition: str) -> list[ast.expr]:
    """Top-level `and` conjuncts of the stored postcondition."""
    src = (postcondition or "").strip()
    if not src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    if len(tree.body) != 1:
        return []
    stmt = tree.body[0]
    expr = stmt.test if isinstance(stmt, ast.Assert) else getattr(stmt, "value", None)
    if expr is None:
        return []
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        return list(expr.values)
    return [expr]


def type_test_conjuncts(postcondition: str) -> list[str]:
    """Conjuncts of the Python postcondition that only test a type.

    Returned unparsed so the run record can name exactly what was rejected.
    """
    out = []
    for c in python_conjuncts(postcondition):
        if _is_type_test(c):
            try:
                out.append(ast.unparse(c))
            except Exception:
                out.append("<unprintable conjunct>")
    return out


def split_conjuncts(expr: str) -> list[str]:
    """Split on top-level `&&`, respecting nesting and string literals.

    Splitting matters: the defect is a single dead conjunct inside an otherwise
    meaningful specification, so testing the postcondition as a whole would miss
    every case this gate exists for.
    """
    out, depth, buf, i = [], 0, [], 0
    in_str: str | None = None
    while i < len(expr):
        c = expr[i]
        if in_str:
            buf.append(c)
            if c == "\\" and i + 1 < len(expr):
                buf.append(expr[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in "\"'":
            in_str = c
            buf.append(c)
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if depth == 0 and expr.startswith("&&", i):
            out.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [c for c in out if c]


def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for k, c in enumerate(s):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and k != len(s) - 1:
                    return s
        s = s[1:-1].strip()
    return s


_SIG_RE = re.compile(
    r"^\s*(?:ghost\s+)?method\s+(?P<name>\w+)\s*(?P<tp><[^>]*>)?\s*"
    r"\((?P<params>.*?)\)\s*returns\s*\((?P<outs>.*?)\)",
    re.DOTALL,
)


def probe_source(model: str, entry: str, backend, conjunct: str) -> str | None:
    """A lemma asserting `conjunct` over arbitrary inputs and outputs.

    The entry's out-parameters become ordinary lemma parameters, which is what
    universally quantifies the result: the lemma must hold for EVERY value the
    method could return, not merely the one it does.
    """
    if backend.lang != "dafny":
        return None
    span = refute._split_signature(model, entry, backend.lang)
    if span is None:
        return None
    sig = model[span[0]:span[1]]
    m = _SIG_RE.match(sig)
    if not m:
        return None
    params = m.group("params").strip()
    outs = m.group("outs").strip()
    if not outs:
        return None
    allp = ", ".join(x for x in (params, outs) if x)

    lines = sig.split("\n")
    keep = []
    for (i, j, kw) in refute._clause_lines(lines, backend.lang):
        if kw == "requires":
            keep.extend(lines[i:j])
    reqs = ("\n".join(keep).rstrip() + "\n") if keep else ""

    body = _strip_outer_parens(conjunct)
    tp = m.group("tp") or ""
    return (f"\n\nlemma {TAUT_PROBE}{tp}({allp})\n{reqs}"
            f"  ensures {body}\n{{\n}}\n")


@dataclass
class Vacuity:
    ok: bool                       # True => gate passes (nothing vacuous)
    vacuous: list[str] = field(default_factory=list)
    type_tests: list[str] = field(default_factory=list)
    checked: int = 0
    reason: str = ""
    inconclusive: list[str] = field(default_factory=list)


def check(model: str, entry: str, backend, *, timeout: float = 60.0,
          max_conjuncts: int = 12, postcondition: str = "") -> Vacuity:
    """Both gates: type-test conjuncts on the Python side, vacuous ones on the target.

    `postcondition` is the stored Python specification. When supplied, Gate A
    runs first and short-circuits: it needs no verifier call, and an entry whose
    specification is partly a type test cannot be rescued by anything the target
    side shows.
    """
    tt = type_test_conjuncts(postcondition) if postcondition else []
    if tt:
        return Vacuity(
            ok=False, type_tests=tt, checked=len(python_conjuncts(postcondition)),
            reason="type-test conjunct absorbed by the target type system: "
                   + "; ".join(tt)[:200])

    if backend.lang != "dafny":
        return Vacuity(ok=True, reason="target-side gate defined for dafny only")
    try:
        clauses = refute.entry_ensures(model, entry, backend)
    except Exception as exc:
        return Vacuity(ok=True, reason=f"could not read ensures: {exc}")
    conjuncts: list[str] = []
    for cl in clauses:
        conjuncts.extend(split_conjuncts(cl))
    if not conjuncts:
        return Vacuity(ok=True, reason="no postcondition to check")

    vacuous, inconclusive = [], []
    for c in conjuncts[:max_conjuncts]:
        probe = probe_source(model, entry, backend, c)
        if probe is None:
            inconclusive.append(c)
            continue
        try:
            res = backend.run(model + probe, mode="unbounded", timeout=timeout,
                              filter_symbol=TAUT_PROBE)
        except Exception:
            inconclusive.append(c)
            continue
        if res.outcome is dafny.Outcome.VERIFIED:
            vacuous.append(c)
        elif res.outcome is not dafny.Outcome.VERIFICATION_FAILED:
            # A type error in the probe says the conjunct mentions something the
            # lemma context lacks; that is a probe limitation, not evidence of
            # vacuity, so it must not reject the entry.
            inconclusive.append(c)

    return Vacuity(
        ok=not vacuous,
        vacuous=vacuous,
        checked=len(conjuncts),
        inconclusive=inconclusive,
        reason=("vacuous postcondition conjunct: " + "; ".join(vacuous)[:200])
        if vacuous else "",
    )


_MODEL = """method f(x: int, xs: seq<int>) returns (return_value: int)
  requires |xs| > 0
  ensures IsInt(return_value) && return_value >= 0
{
  return 0;
}
"""


def _selftest() -> int:
    fails = 0

    def check_(name, cond):
        nonlocal fails
        if not cond:
            fails += 1
            print(f"  [FAIL] {name}")
        else:
            print(f"  [OK  ] {name}")

    check_("splits top-level &&",
           split_conjuncts("a && b") == ["a", "b"])
    check_("does not split inside parens",
           split_conjuncts("(a && b) || c") == ["(a && b) || c"])
    check_("does not split inside a forall body",
           len(split_conjuncts("forall i :: 0 <= i < n ==> (p(i) && q(i))")) == 1)
    check_("splits three",
           split_conjuncts("a && b && c") == ["a", "b", "c"])
    check_("ignores && inside a string literal",
           split_conjuncts('s == \"a && b\" && t == 1') == ['s == \"a && b\"', "t == 1"])
    check_("empty input yields nothing", split_conjuncts("") == [])
    check_("strips redundant outer parens",
           _strip_outer_parens("((a + b))") == "a + b")
    check_("keeps meaningful parens",
           _strip_outer_parens("(a) && (b)") == "(a) && (b)")

    class BK:
        lang = "dafny"

    probe = probe_source(_MODEL, "f", BK(), "IsInt(return_value)")
    check_("probe is a lemma", probe is not None and "lemma " + TAUT_PROBE in probe)
    check_("probe universally quantifies the OUT parameter",
           probe is not None and "return_value: int" in probe.split("\n")[2])
    check_("probe keeps the precondition",
           probe is not None and "requires |xs| > 0" in probe)
    check_("probe drops the original ensures",
           probe is not None and probe.count("ensures") == 1)
    check_("probe body is empty",
           probe is not None and probe.rstrip().endswith("{\n}"))

    # --- Gate A: type-test conjuncts on the Python side.
    check_("isinstance conjunct flagged",
           type_test_conjuncts("assert isinstance(return_value, int) and return_value >= 0")
           == ["isinstance(return_value, int)"])
    check_("the surviving conjunct is not flagged",
           len(type_test_conjuncts("assert isinstance(return_value, int) and return_value >= 0")) == 1)
    check_("a pure value spec is untouched",
           type_test_conjuncts("assert return_value >= 0") == [])
    check_("negated isinstance flagged",
           type_test_conjuncts("assert not isinstance(return_value, str)") != [])
    check_("type() comparison flagged",
           type_test_conjuncts("assert type(return_value) == int") != [])
    check_("elementwise type test flagged",
           type_test_conjuncts("assert all(isinstance(v, int) for v in return_value)") != [])
    check_("all() over a real property NOT flagged",
           type_test_conjuncts("assert all(v >= 0 for v in return_value)") == [])
    check_("issubclass flagged",
           type_test_conjuncts("assert issubclass(type(return_value), int)") != [])
    check_("mixed conjunct with a real test is NOT flagged",
           type_test_conjuncts("assert isinstance(return_value, int) or return_value >= 0") == [])
    check_("malformed postcondition is ignored rather than raising",
           type_test_conjuncts("assert ((") == [])
    check_("empty postcondition yields nothing",
           type_test_conjuncts("") == [])
    check_("three conjuncts split correctly",
           len(python_conjuncts("assert a >= 0 and b >= 0 and c >= 0")) == 3)

    print("FAILED" if fails else "PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
