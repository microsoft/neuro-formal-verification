#!/usr/bin/env python3
"""
Static audit of a C artifact destined for CBMC.

The other backends' auditors defend against a model that proves the wrong thing.
Here the tool cannot prove anything at all -- the arm reports Incorrect on a
concrete violating execution and abstains otherwise -- so the threat is the
mirror image: a model that manufactures a violation. Every check below exists to
stop a counterexample that says nothing about the Python program.

WHAT CAN FAKE A REFUTATION

  * Asserting something other than the postcondition. The harness must assert
    `<entry>_post(...)` applied to the result of `<entry>(...)`; assert anything
    else and the trace refutes a proposition nobody asked about.
  * Dropping the precondition. Without `__CPROVER_assume(<entry>_pre(...))` the
    checker is free to pick inputs the docstring excluded, and will, because
    those are exactly where a program is easiest to break.
  * `__CPROVER_assume` after the assertion, or assumptions that contradict each
    other. The first is ignored on the failing path; the second makes the whole
    harness unreachable, at which point CBMC reports success for the empty set
    of executions. The reachability gate in the backend catches the live case;
    the ordering is checked here because it is visible in the text.
  * Extra `__CPROVER_assert` calls. A second assertion gives the run a second
    way to fail, and the arm would then have to guess which failure was the
    intended one. Exactly one assertion carries the goal tag.
  * `__CPROVER_assert` with the condition inverted or constant-false. Both are
    self-refuting harnesses.

WHAT IS NOT CHECKED HERE

Undefined behaviour is not detectable by reading the text -- it depends on the
values, which is precisely what CBMC computes. It is caught dynamically instead:
the built-in bounds, pointer and overflow checks stay enabled, and a run whose
trace passes through one of them is malformed rather than refuting. That
distinction is where the arm's honesty actually lives, and it is enforced in
`nfv.backends.cbmc`, not here.

Self-test:
    python -m nfv.agent.c_integrity
"""

from __future__ import annotations

import re
import sys

from nfv.agent.integrity import IntegrityReport

GOAL_TAG = "nfv-goal"

_LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Constructs that let the artifact escape the checker rather than be checked by
# it. Each is a way to make the harness say something other than what it appears
# to say.
_BANNED = [
    (re.compile(r"\b__CPROVER_assert\s*\(\s*0\s*[,)]"), "__CPROVER_assert(0, ...)",
     "an assertion that fails unconditionally refutes nothing about the program"),
    (re.compile(r"\bexit\s*\(|\babort\s*\(|\b__CPROVER_assume\s*\(\s*0\s*\)"),
     "exit/abort/assume(0)",
     "cuts the execution short, so the assertion is never reached"),
    (re.compile(r"#\s*include\b"), "#include",
     "pulls in a library CBMC models approximately; the translation must be "
     "self-contained so that a trace is a fact about the program"),
    (re.compile(r"\b__CPROVER_(?:havoc_object|allocate|assignable)\b"),
     "__CPROVER memory intrinsic",
     "manipulates the heap model directly, outside what the translation states"),
    (re.compile(r"\bnondet_(?:int|long|double|bool|uint|char)\s*\("),
     "nondet_* directly",
     "inputs must come from `nfv_int()` / `nfv_double()` / `nfv_len()`, which "
     "bound them to a size C can represent; an unrestricted `nondet_` overflows "
     "and the run is lost to undefined behaviour"),
    (re.compile(r"\bunion\b"), "union",
     "type-punning bits into a float manufactures NaN, infinity and denormals, "
     "which are not values the documentation describes"),
]

_ASSERT = re.compile(r"__CPROVER_assert\s*\(")
_ASSUME = re.compile(r"__CPROVER_assume\s*\(")


def _strip_comments(model: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", model))


def _fn_body(code: str, name: str) -> str | None:
    """The text of `name`'s definition, or None when it is not defined.

    A declaration without a body (`int f(long n);`) is deliberately not matched:
    an undefined function is nondeterministic to CBMC, so a "counterexample"
    against it is a statement about an arbitrary function, not the program.

    The return type is not constrained. Translations legitimately return a
    `struct` -- that is how a Python function returning a list is expressed in C,
    since a C function cannot return an array -- so matching a fixed list of
    builtin type keywords would reject exactly the entries that did the hard
    part correctly. A definition is instead recognised structurally: the name,
    a balanced parameter list, then `{`, with a return type in front of it
    rather than a call context.
    """
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", code):
        before = code[:m.start()].rstrip()
        # A call site is preceded by an operator or a separator; a definition is
        # preceded by its return type, i.e. by an identifier, a `*`, or the `}`
        # that closes an inline `struct { ... }` return type. The last is not a
        # corner case: it is how a Python function returning a list is written
        # in C, since a C function cannot return an array.
        if not before or not re.search(r"[\w\*\}]$", before):
            continue
        if re.search(r"\breturn$", before):
            continue
        depth, i = 0, m.end() - 1
        while i < len(code):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        else:
            continue
        j = i + 1
        while j < len(code) and code[j].isspace():
            j += 1
        if j >= len(code) or code[j] != "{":
            continue  # a declaration, or a call
        depth, k = 0, j
        while k < len(code):
            if code[k] == "{":
                depth += 1
            elif code[k] == "}":
                depth -= 1
                if depth == 0:
                    return code[m.start():k + 1]
            k += 1
    return None


def _returns_constant(body: str, value: str) -> bool:
    stmts = re.findall(r"return\s+([^;]+);", body)
    return len(stmts) == 1 and stmts[0].strip() == value


def check(model: str, entry: str) -> IntegrityReport:
    """Validate a C artifact. `entry` is the Python function's name."""
    rep = IntegrityReport(ok=True)
    code = _strip_comments(model)

    for pattern, name, why in _BANNED:
        if pattern.search(code):
            rep.violations.append(f"uses `{name}` — {why}")

    # The program. Must be defined, not merely declared: CBMC treats an
    # undefined function as returning an arbitrary value, and a trace through
    # one refutes nothing.
    prog = _fn_body(code, entry)
    if prog is None:
        rep.violations.append(
            f"`{entry}` is not defined with a body — an undefined function is "
            f"nondeterministic to CBMC, so a counterexample against it is about "
            f"an arbitrary function rather than the program")

    # The precondition. Always present, so that its absence cannot be confused
    # with "no restriction"; `return 1;` is how "no restriction" is written.
    pre = _fn_body(code, f"{entry}_pre")
    if pre is None:
        rep.violations.append(
            f"no `{entry}_pre` — the assumed input domain must be stated "
            f"explicitly, as `return 1;` when the documentation restricts nothing")
    else:
        if _returns_constant(pre, "0"):
            rep.violations.append(
                f"`{entry}_pre` is `0` — an empty input domain leaves no "
                f"execution for the checker to examine")
        elif not _returns_constant(pre, "1"):
            expr = re.findall(r"return\s+([^;]+);", pre)
            if expr:
                rep.assumed_precondition.append(" ".join(expr[0].split())[:200])

    post = _fn_body(code, f"{entry}_post")
    if post is None:
        rep.violations.append(f"no `{entry}_post` — nothing states the goal")
    elif _returns_constant(post, "1"):
        rep.violations.append(
            f"`{entry}_post` is `1` — a tautological postcondition cannot be "
            f"violated, so the arm could never refute anything")
    elif _returns_constant(post, "0"):
        rep.violations.append(
            f"`{entry}_post` is `0` — an unsatisfiable postcondition is violated "
            f"by every input, so a counterexample says nothing about the program")

    # The harness. This is what the other backends get for free from a method
    # signature, so it is where most of the ways to cheat live.
    main = _fn_body(code, "main")
    if main is None:
        rep.violations.append("no `main` — nothing drives the program")
        rep.ok = not rep.violations
        return rep

    asserts = _ASSERT.findall(main)
    if len(asserts) == 0:
        rep.violations.append("`main` asserts nothing")
    elif len(asserts) > 1:
        rep.violations.append(
            f"{len(asserts)} `__CPROVER_assert` calls in `main`; exactly one is "
            f"required, or a failure cannot be attributed to the postcondition")
    if main.count(GOAL_TAG) != 1:
        rep.violations.append(
            f"the goal assertion must carry the comment \"{GOAL_TAG}\" exactly "
            f"once; found {main.count(GOAL_TAG)}")

    goal = re.search(r"__CPROVER_assert\s*\((.*)\)\s*;", main, re.DOTALL)
    goal_txt = goal.group(1) if goal else ""
    flat = " ".join(goal_txt.split())
    if goal is not None:
        if f"{entry}_post" not in flat:
            rep.violations.append(
                f"the assertion does not mention `{entry}_post` — the refuted "
                f"proposition is not the translated postcondition")
        elif f"{entry}(" not in flat.replace(" ", "") and \
                f"{entry}_post" in flat and \
                not re.search(rf"\b{re.escape(entry)}\s*\(", flat):
            rep.violations.append(
                f"the assertion does not apply `{entry}_post` to the result of "
                f"`{entry}(...)` — the postcondition is not being asserted about "
                f"the program")
        if re.match(r"^\s*!", flat):
            rep.violations.append(
                "the asserted condition is negated — the harness refutes the "
                "postcondition by construction")

    if not _ASSUME.search(main):
        rep.violations.append(
            f"`main` does not `__CPROVER_assume({entry}_pre(...))` — without it "
            f"the checker may choose inputs the documentation excluded, which is "
            f"exactly where a counterexample is easiest to find and least "
            f"meaningful")
    elif f"{entry}_pre" not in main:
        rep.violations.append(
            f"`main` assumes something, but not `{entry}_pre` — the constraint "
            f"the checker works under is not the stated input domain")
    else:
        first_assert = _ASSERT.search(main)
        pre_assumes = [m.start() for m in _ASSUME.finditer(main)]
        if first_assert and any(p > first_assert.start() for p in pre_assumes):
            rep.violations.append(
                "an `__CPROVER_assume` appears after the assertion, where it "
                "cannot constrain the failing execution")

    rep.ok = not rep.violations
    return rep


_HARNESS = """\
// src: source.py:1 | def f(n):
int f(int n) { return %s; }

// synth: precondition from docstring: "non-negative"
int f_pre(int n) { return %s; }

// synth: postcondition from provided assertion
int f_post(int n, int return_value) { return %s; }

// synth: goal
int main(void) {
  int n = nfv_int();
%s
  return 0;
}
"""

_BODY = ("  __CPROVER_assume(f_pre(n));\n"
         "  __CPROVER_assert(f_post(n, f(n)), \"nfv-goal\");")
_GOOD = _HARNESS % ("n", "n >= 0", "return_value >= 0", _BODY)


def _case(name: str, model: str, expect_ok: bool, needle: str = "") -> bool:
    rep = check(model, "f")
    ok = rep.ok == expect_ok and (not needle or
                                  any(needle in v for v in rep.violations))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"         ok={rep.ok} violations={rep.violations}")
    return ok


def _selftest() -> int:
    print("c_integrity selftest\n")
    results = [
        _case("well-formed artifact passes", _GOOD, True),

        _case("undefined entry is rejected",
              _GOOD.replace("int f(int n) { return n; }", "int f(int n);"),
              False, "not defined with a body"),
        _case("missing precondition is rejected",
              _GOOD.replace("int f_pre(int n) { return n >= 0; }", ""),
              False, "no `f_pre`"),
        _case("empty input domain is rejected",
              _HARNESS % ("n", "0", "return_value >= 0", _BODY),
              False, "empty input domain"),
        _case("unrestricted domain is allowed",
              _HARNESS % ("n", "1", "return_value >= 0", _BODY), True),
        _case("tautological postcondition is rejected",
              _HARNESS % ("n", "n >= 0", "1", _BODY), False, "tautological"),
        _case("unsatisfiable postcondition is rejected",
              _HARNESS % ("n", "n >= 0", "0", _BODY), False, "unsatisfiable"),

        _case("asserting something else is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assume(f_pre(n));\n"
                          "  __CPROVER_assert(n == n, \"nfv-goal\");"),
              False, "not the translated postcondition"),
        _case("negated assertion is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assume(f_pre(n));\n"
                          "  __CPROVER_assert(!(f_post(n, f(n))), \"nfv-goal\");"),
              False, "negated"),
        _case("dropped precondition is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assert(f_post(n, f(n)), \"nfv-goal\");"),
              False, "does not `__CPROVER_assume"),
        _case("assuming something else is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assume(n == 3);\n"
                          "  __CPROVER_assert(f_post(n, f(n)), \"nfv-goal\");"),
              False, "not the stated input domain"),
        _case("assumption after the assertion is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assert(f_post(n, f(n)), \"nfv-goal\");\n"
                          "  __CPROVER_assume(f_pre(n));"),
              False, "after the assertion"),
        _case("a second assertion is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assume(f_pre(n));\n"
                          "  __CPROVER_assert(f_post(n, f(n)), \"nfv-goal\");\n"
                          "  __CPROVER_assert(n >= 0, \"extra\");"),
              False, "exactly one is required"),
        _case("assert(0) is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assume(f_pre(n));\n"
                          "  __CPROVER_assert(0, \"nfv-goal\");"),
              False, "unconditionally"),
        _case("assume(0) is rejected",
              _HARNESS % ("n", "n >= 0", "return_value >= 0",
                          "  __CPROVER_assume(0);\n"
                          "  __CPROVER_assert(f_post(n, f(n)), \"nfv-goal\");"),
              False, "never reached"),
        _case("#include is rejected",
              "#include <stdlib.h>\n" + _GOOD, False, "self-contained"),
        _case("missing goal tag is rejected",
              _GOOD.replace('"nfv-goal"', '"whatever"'), False, "nfv-goal"),
        _case("missing main is rejected",
              _GOOD.split("// synth: goal")[0], False, "no `main`"),
        _case("raw nondet_ input is rejected",
              _GOOD.replace("nfv_int()", "nondet_long()"), False,
              "bound them to a size C can represent"),
        _case("union type-punning is rejected",
              _GOOD.replace("int n = nfv_int();",
                            "union { long b; double v; } u; u.b = 1; int n = u.b;"),
              False, "manufactures NaN"),
        # A Python function returning a list becomes a struct-returning C
        # function, because a C function cannot return an array. The return type
        # then ends in `}`, which a naive definition-matcher reads as the end of
        # a previous block and so treats the function as undeclared -- rejecting
        # exactly the translations that handled the hard case correctly.
        _case("struct return type is a definition",
              """\
struct r { long v[8]; long n; } f(long xs[8], long len) { struct r o; o.n = 0; return o; }
int f_pre(long xs[8], long len) { return len >= 0; }
int f_post(long xs[8], long len, struct r return_value) { return return_value.n >= 0; }
// synth: goal
int main(void) {
  long len = nfv_len(); long xs[8];
  __CPROVER_assume(f_pre(xs, len));
  __CPROVER_assert(f_post(xs, len, f(xs, len)), "nfv-goal");
  return 0;
}
""", True),
    ]
    # The stated precondition must be recorded, not merely permitted: the run
    # compares it against what phase 0b authorised.
    rep = check(_GOOD, "f")
    ok = rep.assumed_precondition == ["n >= 0"]
    results.append(ok)
    print(f"  [{'OK  ' if ok else 'FAIL'}] assumed precondition is recorded "
          f"-> {rep.assumed_precondition}")

    bad = len(results) - sum(results)
    print()
    if bad:
        print(f"SELFTEST FAILED: {bad} case(s)")
        return 1
    print(f"SELFTEST PASSED ({len(results)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
