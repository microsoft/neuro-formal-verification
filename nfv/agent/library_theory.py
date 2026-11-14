# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Phase 0: synthesise a documented library theory before anything is translated.

The problem this exists to solve. F5/F7/F12 traced soundness violations to
INVENTED library specs, and F20 showed every violation in the study falls on a
library-using program. The model has to say what `sum`, `sorted`, slicing or
`//` mean in the target language, and today it does so in two bad places: inline
during translation, and -- worse -- mid-proof during repair, where the axiom it
needs is precisely the axiom that closes the goal in front of it. F24 measured
the consequence in Boogie: 8.22 trusted assumptions per proof, and not one proof
resting on nothing.

The design here separates WHAT THE LIBRARY MEANS from WHAT THIS PROGRAM DOES,
and orders them:

  Phase 0 (here)   Given ONLY the Python source -- no docstring, no
                   postcondition, no label, no verifier output -- declare the
                   library operations the program uses, in the target language,
                   each preceded by an English `doc:` comment saying exactly
                   what Python does. The goal is not in view, so a declaration
                   cannot be shaped to discharge it.
  Phase 1          Translation reproduces this theory VERBATIM at the top of the
                   model and uses it instead of redeclaring the same operations.
  Phase 2          Repair sees the theory in the artifact every round, docs
                   included, and may not alter it. Edits are detected.

Two honest limitations, stated because it would be easy to overclaim:

* A `doc:` comment is INERT. The verifier does not read comments, and nothing
  here is checked against Python. The doc line makes the trusted base
  self-describing and auditable by a human or a later pass; it does not make it
  correct.
* Any axiom this phase emits is TRUSTED, exactly like one written during repair.
  The claim is not that this shrinks the trusted base but that it moves the
  trusted base out from under goal pressure, where F24 says it is written worst,
  and makes every element of it declare its own intent. Whether that trade pays
  is an empirical question, which is why it is a separate arm.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

_SYSTEM = """\
You are writing a small LIBRARY THEORY in {target}.

You will be shown a Python source file. Declare, in {target}, the library
operations and types that the file relies on and that {target} does not already
provide: built-in functions, methods of built-in types, imported module
functions, and any operator or syntactic form whose Python behaviour has edge
cases (indexing, slicing, division, comparison, iteration order, mutation).

{syntax_guide}

Requirements.

1. Every declaration is immediately preceded by a doc comment of the form
   `{c} doc: ...` -- one or more lines, in English -- stating exactly what
   Python does: the precise meaning, the behaviour at the boundaries, and which
   exceptions Python raises and when. A declaration without a `{c} doc:` comment
   is not acceptable.
2. Prefer a total definition with a BODY over an uninterpreted declaration, and
   an uninterpreted declaration over an axiom. Where an axiom is genuinely
   needed, precede it additionally with `{c} axiom-justification: ...` saying
   which documented Python behaviour it encodes and why it cannot be derived.
3. State only the library. Say NOTHING about what the Python program computes,
   whether it is correct, or what it returns. Do not translate the program and
   do not declare anything whose statement mentions this program's intent.
4. The theory must stand alone: it must type-check on its own, with no
   dependency on anything the program will later declare.
5. Omit operations whose behaviour has no edge cases worth stating. An empty
   theory is a valid answer when the program uses no library.

Output a single fenced {lang} code block containing the theory and nothing else.
If no theory is needed, output an empty code block."""

_USER = """\
```python
{source}
```"""

_REPAIR = """\
The theory does not type-check in {target}. Fix it and output the corrected
theory as a single fenced {lang} code block. Do not add anything about the
Python program; only repair the declarations.

```
{errors}
```"""

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)


def _extract(text: str) -> str | None:
    m = _FENCE.search(text or "")
    return m.group(1).strip() if m else None


@dataclass
class TheoryResult:
    ok: bool
    text: str = ""
    rounds: int = 0
    reason: str = ""
    decls: int = 0
    documented: int = 0
    axioms: int = 0
    undocumented: list[str] = field(default_factory=list)


# Declaration keywords, across the three targets. Deliberately syntactic: this
# is an audit of the artifact, not a parse.
_DECL = re.compile(
    r"^\s*(?:function|predicate|lemma|datatype|type|const|method|ghost function"
    r"|procedure|axiom|assume)\b"
)
_AXIOM = re.compile(r"^\s*(?:axiom|assume)\b")

# C. There are no declaration keywords to key off, but a library theory is
# top-level, and C's layout puts a type or storage-class keyword at the start of
# every function definition, prototype, typedef, struct and global. Anchoring at
# column 0 with no leading whitespace therefore separates declarations from
# bodies exactly, the same trick the Lean branch uses, and a closing `}` does not
# match. C cannot be selected by comment marker -- it shares `//` with Dafny and
# Boogie -- so this dialect is chosen by `lang` instead.
_DECL_C = re.compile(
    r"^(?:typedef|struct|union|enum|static|inline|extern|const|register"
    r"|volatile|unsigned|signed|void|char|short|int|long|float|double|_Bool"
    r"|size_t|ssize_t|bool)\b"
)
# C has no axiom mechanism, so nothing here is assumed by declaration -- with one
# exception that matters more than an axiom would. A function DECLARED but not
# DEFINED is treated by CBMC as returning an arbitrary value, so any result that
# depends on it is a statement about an arbitrary function rather than about the
# program. That is precisely an unchecked assumption, and it is what the trusted
# base must count. A top-level line ending `);` is such a prototype.
_AXIOM_C = re.compile(r"^[^/\n]*\)\s*;\s*$")


def _is_c(lang: str) -> bool:
    return (lang or "").strip().lower() == "c"


def _decl_re(comment: str, lang: str = "") -> re.Pattern:
    if _is_c(lang):
        return _DECL_C
    return _DECL


def _axiom_re(comment: str, lang: str = "") -> re.Pattern:
    if _is_c(lang):
        return _AXIOM_C
    return _AXIOM


def audit(text: str, comment: str,
          lang: str = "") -> tuple[int, int, int, list[str]]:
    """(declarations, documented, axioms, undocumented) for a theory body.

    A declaration counts as documented when a `doc:` comment appears in the
    contiguous comment block immediately above it.
    """
    lines = text.splitlines()
    decl_re, axiom_re = _decl_re(comment, lang), _axiom_re(comment, lang)
    decls = documented = axioms = 0
    undocumented: list[str] = []
    for i, line in enumerate(lines):
        if not decl_re.match(line):
            continue
        decls += 1
        if axiom_re.match(line):
            axioms += 1
        has_doc = False
        j = i - 1
        while j >= 0:
            stripped = lines[j].strip()
            if not stripped:
                j -= 1
                continue
            if not stripped.startswith(comment) and not stripped.startswith(";"):
                break
            if "doc:" in stripped:
                has_doc = True
                break
            j -= 1
        if has_doc:
            documented += 1
        else:
            undocumented.append(line.strip()[:90])
    return decls, documented, axioms, undocumented


def normalise(text: str) -> str:
    """Whitespace-insensitive form, for detecting edits to a frozen theory."""
    return "\n".join(l.rstrip() for l in text.strip().splitlines() if l.strip())


_CONTRACT = re.compile(
    r"^\s*(?:free\s+)?(?:requires|ensures|modifies|reads|decreases|invariant)\b")


def _cmp(text: str, comment: str = "//") -> str:
    """Comparison form: no comments, no semicolons, whitespace collapsed.

    Semicolons are dropped because Boogie accepts a bodyless procedure written
    either `p(...) returns (r: int)` or `p(...) returns (r: int);` before its
    contract clauses, and the model re-punctuated one that way in the smoke run.
    That is not a change to what is declared, and rejecting it would be pedantry.

    In SMT-LIB `;` STARTS a comment, so dropping the character there would splice
    a comment's text into the code. Comment lines are removed by marker instead,
    and the character itself is left alone.
    """
    lines = [l for l in text.splitlines()
             if not l.strip().startswith(comment)
             and not l.strip().startswith(";;")]
    joined = " ".join(lines)
    joined = joined.replace(";", " ")
    return " ".join(joined.split())


def declarations(text: str, comment: str = "//", lang: str = "") -> list[str]:
    """Each declaration in `text`, signature and contract clauses together.

    Compared declaration by declaration rather than as one block: the model
    legitimately interleaves its own declarations with the theory's, so
    requiring the theory to survive as a contiguous run rejects artifacts that
    changed nothing about what the theory says. Trailing `requires`/`ensures`
    lines are absorbed into the declaration they belong to -- stopping at the
    signature would let an edited postcondition pass unnoticed.

    SMT-LIB has no contract clauses; a declaration is one balanced s-expression,
    so it is closed on parenthesis depth instead. Lean has neither: a declaration
    runs to the next column-0 keyword, which the language guarantees is a real
    boundary because continuation lines must be indented.
    """
    lines = [l for l in text.splitlines() if not l.strip().startswith(comment)]
    decl_re = _decl_re(comment, lang)
    stmts: list[str] = []
    i = 0
    while i < len(lines):
        if not decl_re.match(lines[i]):
            i += 1
            continue
        cur = [lines[i]]
        depth = lines[i].count("{") - lines[i].count("}")
        i += 1
        while i < len(lines) and (depth > 0 or not cur[-1].rstrip().endswith((";", "}"))):
            cur.append(lines[i])
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        while i < len(lines) and _CONTRACT.match(lines[i]):
            cur.append(lines[i])
            i += 1
        stmts.append(_cmp("\n".join(cur), comment))
    return stmts


def code_only(text: str, comment: str = "//") -> str:
    """`text` with comments dropped and all whitespace collapsed."""
    lines = [l for l in text.splitlines() if not l.strip().startswith(comment)]
    return " ".join(" ".join(lines).split())


def contains(model: str, theory: str, comment: str = "//",
             lang: str = "") -> bool:
    """True when every declaration of `theory` survives, unaltered, in `model`.

    Reproducing a theory is a claim about DECLARATIONS, not about line breaks or
    ordering. Requiring byte equality cost 2 of 6 entries in the first Boogie
    smoke run, and in both cases the entire difference was formatting. Layout is
    therefore tolerated; any change to what is declared is not.
    """
    body = _cmp(model, comment)
    return all(d in body for d in declarations(theory, comment, lang))


def docs_intact(model: str, theory: str, comment: str = "//",
                lang: str = "") -> bool:
    """True when the theory's declarations still carry `doc:` comments in `model`.

    Comment text is not compared byte for byte (see `code_only`), so this is what
    stops the model from quietly dropping the documentation while keeping the
    declarations it documents.

    The comparison is against the number of declarations the theory itself
    documents, NOT against its declaration count. Phase 0 admits a theory with
    an undocumented declaration --- it records the exception in
    `theory_undocumented` rather than rejecting it --- and comparing against the
    declaration count would then demand of the reproduction a document the
    original never had, so a byte-for-byte copy of the frozen theory would fail
    the check that exists to require exactly that copy. The two counts coincide
    whenever phase 0 documented everything, which is why this only ever bit the
    theories that used the exception.
    """
    n_theory = audit(theory, comment, lang)[1]
    if not n_theory:
        return True
    decls, documented, _, _ = audit(model, comment, lang)
    return documented >= n_theory


# Declarations that enter the trusted base: a bodyless callable, an {:axiom}
# attribute, or Boogie's top-level `axiom`. Kept in step with `integrity.check`,
# which is what actually reports the trusted base.
_BODYLESS = re.compile(
    # Linear form; see the note in integrity.py -- the previous nested-quantifier
    # version backtracked catastrophically on long theories.
    r"^[ \t]*(?:lemma|function|method|predicate|procedure)\b[^{};]*;[ \t]*$",
    re.MULTILINE,
)


def theory_lines(model: str, theory: str) -> set[int]:
    """Indices of the model lines occupied by the frozen theory, when it was
    reproduced line for line. Best-effort only: `amendments` does not rely on it,
    because re-wrapping legitimately breaks line-level matching."""
    want = [l.strip() for l in theory.splitlines() if l.strip()]
    if not want:
        return set()
    idx = [i for i, l in enumerate(model.splitlines()) if l.strip()]
    lines = model.splitlines()
    for start in range(len(idx) - len(want) + 1):
        run = idx[start:start + len(want)]
        if all(lines[i].strip() == w for i, w in zip(run, want)):
            return set(range(run[0], run[-1] + 1))
    return set()


def amendments(model: str, theory: str, comment: str, lang: str = "dafny",
               baseline: str = "") -> list[tuple[int, str]]:
    """Trusted declarations in `model` that are new since `baseline`.

    An amendment is an assumption written WITH THE GOAL IN VIEW. Everything the
    frozen theory declared, and everything already present in the model that
    entered this round, is excluded -- otherwise translation-time library
    declarations would be charged to the abduction round that never wrote them.
    Reporting the rest separately is the entire point: it is what makes "blind
    vs pressured" a measurable distinction rather than an assertion.
    """
    known = code_only(theory, comment) + " " + code_only(baseline, comment)
    out: list[tuple[int, str]] = []

    def _add(start_char: int, text: str) -> None:
        collapsed = code_only(text, comment)
        if collapsed and collapsed in known:
            return
        out.append((model.count("\n", 0, start_char), " ".join(text.split())[:160]))

    for m in _BODYLESS.finditer(model):
        _add(m.start(), m.group(0))
    for m in re.finditer(r"^.*\{:axiom\}.*$", model, re.MULTILINE):
        _add(m.start(), m.group(0))
    if lang == "c":
        # C has no axiom keyword, but a function DECLARED and never DEFINED is
        # read by CBMC as returning an arbitrary value, so anything depending on
        # it holds for an arbitrary function rather than for the program. That
        # is the C trusted base, and it is what must be charged to the round
        # that wrote it. A top-level line ending `);` is such a prototype.
        lines = model.splitlines()
        pos = 0
        for l in lines:
            if _DECL_C.match(l) and _AXIOM_C.match(l):
                _add(pos, l)
            pos += len(l) + 1
    # A line can match more than one pattern; keep one entry per line.
    seen: set[int] = set()
    uniq = []
    for i, t in sorted(out):
        if i not in seen:
            seen.add(i)
            uniq.append((i, t))
    return uniq


def _preceding_comments(model: str, i: int, comment: str) -> str:
    lines = model.splitlines()
    j, block = i - 1, []
    while j >= 0:
        s = lines[j].strip()
        if not s:
            j -= 1
            continue
        if not s.startswith(comment) and not s.startswith(";"):
            break
        block.append(s)
        j -= 1
    return "\n".join(block)


def amendment_issues(model: str, theory: str, comment: str, entry_point: str,
                     lang: str = "dafny", baseline: str = ""
                     ) -> tuple[list[str], list[str]]:
    """(amendment texts, reasons to reject).

    An amendment is held to the same standard phase 0 was: it must say what
    Python behaviour it encodes, and why it had to be an assumption. It may not
    mention the entry point -- an axiom naming the program under test is the one
    shape that begs the question outright.

    Honest limit: this cannot detect an axiom that merely IMPLIES the
    postcondition without naming it. No syntactic check can. The defence there
    is that the amendment is recorded, not that it is prevented.
    """
    found = amendments(model, theory, comment, lang, baseline)
    texts = [t for _, t in found]
    issues: list[str] = []
    entry_re = re.compile(rf"\b{re.escape(entry_point)}\b")
    for i, text in found:
        ctx = _preceding_comments(model, i, comment)
        if "doc:" not in ctx:
            issues.append(f"amendment without a `doc:` comment: {text}")
        if "axiom-justification:" not in ctx:
            issues.append(f"amendment without `axiom-justification:`: {text}")
        if entry_re.search(text):
            issues.append(f"amendment names the entry point `{entry_point}`: {text}")
    return texts, issues


_PRE_SYSTEM = """\
You are stating the DOMAIN of a Python function, in {target}.

You will be shown a Python source file and the function's docstring. Your job is
to write the precondition -- the constraint on the inputs that the docstring
says the caller must satisfy.

{syntax_guide}

Requirements.

1. A clause may be written ONLY if the docstring states or directly implies it.
   Each clause is immediately preceded by `{c} doc: ...` quoting or closely
   paraphrasing the words of the docstring that license it.
2. If the docstring places no constraint on the inputs, output an EMPTY code
   block. That is the expected answer and is always acceptable. A function that
   is total over its argument types has no precondition.
3. Do NOT write a clause merely because the code would misbehave without it
   (an index that could go out of range, a division that could be by zero, an
   empty list). If the docstring does not restrict the input, the function is
   claimed to work on that input, and excluding it would hide a real bug.
4. Do NOT restrict the input to make any particular property hold. You have not
   been shown the property, and you must not guess at one.
5. Output only the clauses, one per line, in {target} syntax, in a single code
   block. No signature, no body, no other declarations.

Write the clauses exactly as they would appear on the entry declaration, for
example `requires n >= 0` for Dafny or Boogie, and as a single boolean
expression per line for SMT-LIB.
"""

_PRE_USER = """\
Python source:

```python
{source}
```

Docstring:

```text
{docstring}
```
"""


@dataclass
class PreconditionResult:
    ok: bool
    items: list[str] = field(default_factory=list)
    text: str = ""
    rounds: int = 0
    reason: str = ""
    undocumented: list[str] = field(default_factory=list)


def _pre_clauses(text: str, comment: str) -> tuple[list[str], list[str]]:
    """(clauses, clauses lacking a `doc:` justification) from a phase-0b block."""
    clauses: list[str] = []
    undocumented: list[str] = []
    pending_doc = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(comment):
            if "doc:" in line:
                pending_doc = True
            continue
        expr = line
        if expr.lower().startswith("requires"):
            expr = expr[len("requires"):].strip()
        expr = expr.rstrip(";").strip()
        if not expr or expr.lower() == "true":
            continue
        clauses.append(expr)
        if not pending_doc:
            undocumented.append(expr)
        pending_doc = False
    return clauses, undocumented


def precondition_violations(found: list[str], frozen: list[str]) -> list[str]:
    """Preconditions present in a model that phase 0b did not authorise.

    This is what makes the phase worth anything. Asking translation to use the
    frozen precondition is a request; comparing what it actually emitted is the
    check. Comparison is on normalised text, so re-spacing is not a violation,
    but a genuinely different or strengthened clause is.
    """
    ok = {_norm_expr(x) for x in frozen}
    return [f for f in found if _norm_expr(f) not in ok]


def _norm_expr(s: str) -> str:
    """Normalise a precondition for comparison.

    Only formatting is normalised -- spacing, brackets, a trailing semicolon.
    Anything that changes the meaning of the clause must survive, so this
    deliberately does no reasoning: `0 <= n` and `n >= 0` are equivalent but
    compare as different, which errs towards reporting a violation rather than
    silently accepting a rewritten domain.
    """
    s = s.strip().rstrip(";").strip()
    # Multi-character operators are matched first so `>=` is never split into
    # `>` and `=`, which would make `n >= 0` and `n > 0` normalise alike.
    toks = re.findall(r"<==>|==>|<==|[<>=!]=|&&|\|\||\w+|\S", s)
    return " ".join(toks)


async def synthesize_precondition(chat, source: str, docstring: str, backend, *,
                                  timeout: float = 240.0) -> PreconditionResult:
    """Phase 0b: the input domain, written before the goal is visible.

    Phase 0 removes goal pressure from library assumptions but leaves the other
    way to make a proof succeed: restrict the inputs. In the pilot that is
    exactly what happened -- a mutant was proved with an EMPTY trusted base
    because the pipeline abduced `requires 1 <= n`, excluding the only input on
    which it was wrong, with nothing in the docstring licensing the bound.

    So the precondition is authored here instead: from the source and the
    DOCSTRING, with the postcondition, the verifier output and the label all
    still unseen. A precondition written without knowing which input fails
    cannot be shaped to exclude it.
    """
    if chat is None or not source.strip():
        return PreconditionResult(True, [], "", 0, "no source")

    target = backend.lang.capitalize()
    system = (_PRE_SYSTEM.replace("{target}", target)
                         .replace("{c}", backend.comment)
                         .replace("{syntax_guide}", backend.syntax_guide.strip()))
    user = _PRE_USER.format(source=source, docstring=(docstring or "").strip()
                            or "(no docstring provided)")
    try:
        resp = await chat.ask(system, user, timeout=timeout)
    except Exception as exc:
        return PreconditionResult(False, [], "", 1, f"{type(exc).__name__}: {exc}")

    body = _extract(resp.text)
    if body is None:
        # No code block is a legitimate way to say "no constraint".
        return PreconditionResult(True, [], "", 1, "no code block; empty precondition")

    items, undoc = _pre_clauses(body, backend.comment)
    return PreconditionResult(True, items, body.strip(), 1, "", undoc)


async def synthesize(chat, source: str, backend, *, max_rounds: int = 2,
                     timeout: float = 240.0) -> TheoryResult:
    """Build the library theory for one program. Independent of every other."""
    if chat is None or not source.strip():
        return TheoryResult(True, "", 0, "no source")

    target = backend.lang.capitalize()
    system = (_SYSTEM.replace("{target}", target)
                     .replace("{lang}", backend.lang)
                     .replace("{c}", backend.comment)
                     .replace("{syntax_guide}", backend.syntax_guide.strip()))
    user = _USER.format(source=source)

    text = ""
    for round_i in range(1, max_rounds + 1):
        try:
            resp = await chat.ask(system, user, timeout=timeout)
        except Exception as exc:
            return TheoryResult(False, text, round_i,
                                f"{type(exc).__name__}: {exc}")
        body = _extract(resp.text)
        if body is None:
            return TheoryResult(False, text, round_i, "no code block in response")
        text = body
        if not normalise(text):
            return TheoryResult(True, "", round_i, "empty theory")

        tc = await asyncio.to_thread(backend.type_check, text)
        if getattr(tc, "exit_code", 0) == 0:
            # The theory is model-authored, not translated from any source line,
            # so it needs a `synth:` tag or the provenance audit will reject
            # every declaration in it as untagged code. Emitting the tag as part
            # of the frozen text means verbatim reproduction also pins the
            # theory's placement in the model.
            tag = f"{backend.comment} synth: library theory (phase 0)"
            if not text.lstrip().startswith(tag):
                text = tag + "\n" + text
            decls, documented, axioms, undoc = audit(text, backend.comment)
            return TheoryResult(True, text, round_i, "", decls, documented,
                                axioms, undoc)
        diagnostics = ((getattr(tc, "stdout", "") or "")
                       + (getattr(tc, "stderr", "") or "")).strip() or "unknown type error"
        user = _REPAIR.replace("{target}", target).replace("{lang}", backend.lang) \
                      .replace("{errors}", diagnostics[:3000])

    return TheoryResult(False, text, max_rounds, "theory does not type-check")


# --------------------------------------------------------------------------
# selftest


def _selftest() -> int:
    """Regression: a verbatim copy of the frozen theory must pass `docs_intact`.

    Phase 0 admits a theory that leaves a declaration undocumented -- it records
    the exception rather than rejecting the theory -- so the reproduction check
    must compare against what the theory documents, not against how many
    declarations it has. Comparing against the declaration count made a
    byte-for-byte copy fail the check whose whole purpose is to require one.
    """
    documented_all = (
        "// doc: floored modulo\n"
        "function PyMod(a: int, b: int): int { a % b }\n"
        "\n"
        "// doc: exact multiplication\n"
        "function PyMul(a: int, b: int): int { a * b }\n"
    )
    has_undocumented = documented_all + (
        "\n"
        "function PyAdd(a: int, b: int): int { a + b }\n"
    )
    dropped_docs = (
        "function PyMod(a: int, b: int): int { a % b }\n"
        "function PyMul(a: int, b: int): int { a * b }\n"
    )

    fails = 0

    def check(label: str, got: bool, want: bool) -> None:
        nonlocal fails
        if got != want:
            fails += 1
            print(f"  FAIL {label}: got {got}, want {want}")
        else:
            print(f"  ok   {label}")

    check("verbatim copy of a fully documented theory",
          docs_intact(documented_all, documented_all), True)
    check("verbatim copy of a theory with an undocumented declaration",
          docs_intact(has_undocumented, has_undocumented), True)
    check("model documents its own extra declaration too",
          docs_intact(has_undocumented + "// doc: extra\nfunction X(): int { 0 }\n",
                      has_undocumented), True)
    check("model drops the doc comments",
          docs_intact(dropped_docs, documented_all), False)
    check("empty theory is vacuously intact",
          docs_intact(dropped_docs, ""), True)

    d, doc, _, undoc = audit(has_undocumented, "//")
    check("audit still reports the undocumented declaration",
          (d, doc, len(undoc)) == (3, 2, 1), True)

    print("FAILED" if fails else "all library_theory doc checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
