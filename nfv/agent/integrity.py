#!/usr/bin/env python3
"""
Integrity constraints on generated formal artifacts.

The verifier is the only oracle, but an LLM can still smuggle a verdict past it by
emitting a model that trivially verifies (or trivially fails). Prompt instructions
cannot prevent that — they are unenforceable. These are static checks on the
artifact, and a violation yields ABSTAIN, never Verified.

Banned, because each one moves an obligation into the trusted base:
  * `assume`, `{:axiom}`, `{:verify false}`, `{:extern}` — assert without proof
  * bodyless lemmas/functions with an `ensures` — axioms in disguise
  * `requires` on the ENTRY method — silently shrinks the input domain, which is
    exactly how an incorrect program is made to "verify"
  * a weakened or tautological entry `ensures` — proves nothing

`decreases *` is allowed but flagged: it drops the termination proof.

Also implements the Stage-1/Stage-2 freeze: once translation is finalized the
`// src:` lines are immutable, so the proof-repair agent can only add or edit
`// synth:` annotations.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from nfv.agent.provenance import src_fingerprint

# Comments are stripped before scanning so that a tag quoting Python source (for
# example `// src: source.py:4 | assume_something = 1`) cannot trip a keyword.
_LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

_BANNED = [
    (re.compile(r"\bassume\b"), "assume", "asserts a fact without proving it"),
    (re.compile(r"\{:verify\s+false\}"), "{:verify false}", "disables verification"),
    (re.compile(r"\{:extern\}"), "{:extern}", "body is outside the proof"),
]

# Boogie spells several of these differently. `free requires/ensures` is exactly
# an assume wearing a contract's clothes: it is assumed, never checked, so it
# belongs on the banned list alongside `assume` itself.
_BANNED_BOOGIE = _BANNED + [
    (re.compile(r"\bfree\s+(?:requires|ensures)\b"), "free requires/ensures",
     "assumed but never checked - an assume in contract form"),
]

# Declaration keywords that introduce a callable, per target language.
_CALLABLE = {
    "dafny": r"method|function|lemma|predicate",
    "boogie": r"procedure|function|implementation",
}

_TAUTOLOGIES = [
    re.compile(r"ensures\s+true\s*$", re.MULTILINE),
    re.compile(r"ensures\s+(\w+)\s*==\s*true\s*\|\|\s*\1\s*==\s*false"),
    re.compile(r"ensures\s+(\w+)\s*==\s*\1\s*$", re.MULTILINE),
    re.compile(r"ensures\s+(\w+)\s*\|\|\s*!\s*\1\s*$", re.MULTILINE),
]

_BODYLESS = re.compile(
    # A declaration that ends in `;` with no body. The negated class already spans
    # newlines, so no per-line alternation is needed -- and must not be used: the
    # earlier `(?:\n[^{};]*?)*?` form was a nested quantifier that backtracked
    # catastrophically (exponential in the number of lines before the `;`), which
    # hung whole runs once phase-0 theories made models long enough to trigger it.
    r"^[ \t]*(?:lemma|function|method|predicate|procedure)\b[^{};]*;[ \t]*$",
    re.MULTILINE,
)


@dataclass
class IntegrityReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trusted_base: list[str] = field(default_factory=list)
    # Preconditions assumed on the entry method. Legitimate when justified by the
    # docstring, but they narrow the input domain, so they are always reported: a
    # pair whose reference and mutant assume DIFFERENT preconditions is suspect,
    # since their docstrings are identical by construction.
    assumed_precondition: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            s = "integrity OK"
            if self.trusted_base:
                s += f" ({len(self.trusted_base)} trusted spec(s))"
            if self.warnings:
                s += f", {len(self.warnings)} warning(s)"
            return s
        return f"integrity FAILED: {len(self.violations)} violation(s)"


def _strip_comments(model: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", model))


def _entry_signature(model: str, entry: str, lang: str = "dafny") -> str:
    """Text of the entry method's signature, up to its opening brace.

    Tolerates attributes (`method {:axiom} f(...)`) so that an attribute cannot
    hide the entry method from the checks below.
    """
    m = re.search(
        rf"\b(?:{_CALLABLE.get(lang, _CALLABLE['dafny'])})\s+"
        rf"(?:\{{:[^}}]*\}}\s*)*{re.escape(entry)}\b", model)
    if not m:
        return ""
    rest = model[m.start():]
    brace = rest.find("{:")
    # Skip over attribute braces when locating the body.
    idx, n = 0, len(rest)
    while idx < n:
        if rest.startswith("{:", idx):
            close = rest.find("}", idx)
            idx = n if close == -1 else close + 1
            continue
        if rest[idx] == "{":
            return rest[:idx]
        idx += 1
    return rest


def _entry_has_body(model: str, entry: str, lang: str = "dafny") -> bool:
    """Whether the entry method has a body (`{ ... }`) rather than ending in `;`.

    Library functions may be axiomatised, but the program under test may not: a
    bodyless entry method would make the postcondition an assumption.
    """
    sig = _entry_signature(model, entry, lang)
    if not sig:
        return False
    tail = model[model.find(sig) + len(sig):]
    idx, n = 0, len(tail)
    while idx < n:
        if tail.startswith("{:", idx):
            close = tail.find("}", idx)
            idx = n if close == -1 else close + 1
            continue
        ch = tail[idx]
        if ch == "{":
            return True
        if ch == ";":
            return False
        idx += 1
    return False


def check(model: str, entry: str, lang: str = "dafny") -> IntegrityReport:
    """Validate a translated model. `entry` is the entry method name."""
    rep = IntegrityReport(ok=True)
    code = _strip_comments(model)

    for pattern, name, why in (_BANNED_BOOGIE if lang == "boogie" else _BANNED):
        if pattern.search(code):
            rep.violations.append(f"uses `{name}` — {why}")

    # Bodyless declarations and {:axiom} are the TRUSTED BASE, not violations.
    # Specs for external/library functions (Python's sum, len, sorted, ...) cannot
    # always be given a definition, so they are permitted and RECORDED instead.
    # A wrong or over-strong one can make an incorrect program verify, so the size
    # of this set is a reported metric and the first thing to inspect when a mutant
    # verifies -- see `trusted_base`.
    for m in _BODYLESS.finditer(code):
        decl = " ".join(m.group(0).split())[:160]
        rep.trusted_base.append(decl)
    for m in re.finditer(r"^.*\{:axiom\}.*$", code, re.MULTILINE):
        rep.trusted_base.append(" ".join(m.group(0).split())[:160])
    if lang == "boogie":
        # Boogie's top-level `axiom` is the same device by another name.
        for m in re.finditer(r"^\s*axiom\b[^;]*;", code, re.MULTILINE):
            rep.trusted_base.append(" ".join(m.group(0).split())[:160])

    sig = _strip_comments(_entry_signature(model, entry, lang))
    if not sig:
        rep.violations.append(f"entry method `{entry}` not found in model")
    else:
        if re.search(r"\brequires\b", sig):
            # A docstring-justified precondition is legitimate; an unsatisfiable one
            # makes every postcondition hold vacuously, so it is still rejected.
            for rq in re.findall(r"requires\s+([^\n]+)", sig):
                expr = rq.strip().rstrip(";")
                if re.fullmatch(r"false", expr.strip(), re.I) or \
                        re.fullmatch(r"\d+\s*==\s*\d+", expr.strip()) and \
                        len(set(re.findall(r"\d+", expr))) > 1:
                    rep.violations.append(
                        f"entry method `{entry}` has an unsatisfiable `requires {expr}` — "
                        f"this makes any postcondition hold vacuously")
                elif re.fullmatch(r"true", expr.strip(), re.I):
                    pass  # no restriction; not part of the assumed domain
                else:
                    rep.assumed_precondition.append(expr)
        if not re.search(r"\bensures\b", sig):
            rep.violations.append(f"entry method `{entry}` has no `ensures` clause")
        # An axiom on the ENTRY method assumes the very postcondition under test.
        # Library axioms are fine; this one is the whole question begged.
        if re.search(r"\{:axiom\}", sig):
            rep.violations.append(
                f"entry method `{entry}` is declared `{{:axiom}}` — that assumes the "
                f"postcondition instead of proving it")
        if not _entry_has_body(model, entry, lang):
            rep.violations.append(
                f"entry method `{entry}` has no body — the program itself must not "
                f"be axiomatised")

    for pattern in _TAUTOLOGIES:
        m = pattern.search(code)
        if m:
            rep.violations.append(
                f"tautological ensures proves nothing: {' '.join(m.group(0).split())}")
            break

    if re.search(r"decreases\s+\*", code):
        rep.warnings.append("`decreases *` — termination is not proved")

    rep.ok = not rep.violations
    return rep


def freeze_hash(model: str, comment: str = "//") -> str:
    """Hash of the program logic (`src:`-tagged content only).

    Stable across annotation changes, so Stage 2 can add invariants freely while
    any edit to program logic changes the hash.
    """
    fp = src_fingerprint(model, comment)
    payload = "\n".join(f"{ln}:{code}" for ln, code in fp)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class FreezeReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    before_hash: str = ""
    after_hash: str = ""
    # src lines that differ ONLY by whitelisted proof-side annotations, kept so a
    # run can be audited for how often the relaxation was actually exercised.
    annotated_lines: list[tuple[int, str, str]] = field(default_factory=list)


_LAMBDA_ANNOT = re.compile(
    r"""
    (?P<binders>(?:\(\s*[\w\s,:<>\[\]\.]*\s*\)|\w+)\s*)   # lambda binder list
    (?P<annots>(?:\s*(?:requires|reads)\s+(?:(?!=>).)*)+)  # clauses, up to the arrow
    (?P<arrow>=>)                                         # ... immediately before =>
    """,
    re.VERBOSE,
)


def erase_proof_annotations(line: str) -> str:
    """Delete proof-side tokens from a `src:` line, leaving computational content.

    Currently erases exactly ONE construct: `requires`/`reads` clauses inside a
    LAMBDA HEADER, i.e. `i requires 0 <= i < |s| => e` becomes `i => e`.

    Why this is semantics-preserving, and therefore safe to permit on a frozen
    line: in Dafny a lambda `requires` restricts the DOMAIN of a function value.
    It changes no value the function returns on that domain. Every use site must
    then prove its argument lies in the domain, so the well-definedness
    obligation is RELOCATED TO THE CALLER, never discharged. A buggy program
    cannot be laundered into a correct one this way -- the obligation still has
    to be met, just somewhere else. Contrast a cast or a modified index
    expression, which do change computed values; those stay banned.

    Anything not on this whitelist is left untouched and so still trips the
    freeze. Widen this function only with an argument of the same form.
    """
    prev = None
    out = line
    while prev != out:
        prev = out
        out = _LAMBDA_ANNOT.sub(lambda m: m.group("binders") + m.group("arrow"), out)
    return re.sub(r"\s+", " ", out).strip()


def check_freeze(frozen_model: str, new_model: str,
                 comment: str = "//", allow_proof_annotations: bool = False) -> FreezeReport:
    """Stage 2 may add/edit `synth:` lines only; `src:` lines are frozen.

    With `allow_proof_annotations`, the freeze is weakened from "the bytes of a
    src line are immutable" to "the COMPUTATIONAL CONTENT of a src line is
    immutable": a line still matches if it differs only by tokens that
    `erase_proof_annotations` recognises as proof-side. Every such edit is
    recorded in `annotated_lines` so a run can be audited for how often, and
    where, the relaxation was used.
    """
    before = src_fingerprint(frozen_model, comment)
    after = src_fingerprint(new_model, comment)
    rep = FreezeReport(
        ok=True,
        before_hash=freeze_hash(frozen_model, comment),
        after_hash=freeze_hash(new_model, comment),
    )

    if before != after:
        if len(before) == len(after) and [ln for ln, _ in before] == [ln for ln, _ in after]:
            # Same shape, so compare positionally. A dict keyed on the source
            # line number would collapse the several model lines that can share
            # one source line (e.g. a statement and its closing brace).
            for (ln, btxt), (_, atxt) in zip(before, after):
                if btxt == atxt:
                    continue
                if (allow_proof_annotations
                        and erase_proof_annotations(atxt) == erase_proof_annotations(btxt)):
                    rep.annotated_lines.append((ln, btxt, atxt))
                    continue
                rep.violations.append(
                    f"source line {ln} modified in stage 2:\n"
                    f"    before: {btxt!r}\n    after:  {atxt!r}")
        else:
            b = dict(before)
            a = dict(after)
            for ln in sorted(set(b) | set(a)):
                if ln not in a:
                    rep.violations.append(f"source line {ln} deleted in stage 2: {b[ln]!r}")
                elif ln not in b:
                    rep.violations.append(f"source line {ln} added in stage 2: {a[ln]!r}")
                elif b[ln] != a[ln]:
                    rep.violations.append(
                        f"source line {ln} modified in stage 2:\n"
                        f"    before: {b[ln]!r}\n    after:  {a[ln]!r}")
            if not rep.violations:
                rep.violations.append("// src: content changed (ordering differs)")

    rep.ok = not rep.violations
    return rep


_T_ENTRY = "f"

_T_OK = """// src: source.py:3 | def f(n: int) -> int:
method f(n: int) returns (r: int)
// synth: ensures translated from the postcondition
  ensures r >= 0
{
// src: source.py:4 | r = 0
  r := 0;
}
"""


def _selftest() -> int:
    """Every way an LLM can fake a proof must be rejected."""
    cases: list[tuple[str, str, bool]] = [
        ("faithful artifact", _T_OK, True),
        ("assume statement", _T_OK.replace("  r := 0;", "  assume r >= 0;\n  r := 0;"), False),
        ("{:axiom} on ENTRY method", _T_OK.replace("method f", "method {:axiom} f"), False),
        ("bodyless ENTRY method", _T_OK[:_T_OK.find("{\n// src: source.py:4")] + ";\n", False),
        ("{:verify false}", _T_OK.replace("method f", "method {:verify false} f"), False),
        ("{:extern}", _T_OK.replace("method f", "method {:extern} f"), False),
        # Shrinks the input domain, but is legitimate when the docstring says so;
        # recorded rather than rejected (see `assumed_precondition`).
        ("requires on entry (docstring-justified)", _T_OK.replace(
            "  ensures r >= 0", "  requires n > 0\n  ensures r >= 0"), True),
        ("requires false (vacuous)", _T_OK.replace(
            "  ensures r >= 0", "  requires false\n  ensures r >= 0"), False),
        ("tautological ensures", _T_OK.replace(
            "  ensures r >= 0", "  ensures r == true || r == false"), False),
        ("ensures true", _T_OK.replace("  ensures r >= 0", "  ensures true"), False),
        ("no ensures", _T_OK.replace("  ensures r >= 0\n", ""), False),
        ("missing entry method", _T_OK.replace("method f(", "method g("), False),
        # Library axioms ARE the trusted base: permitted, but recorded (below).
        ("bodyless LIBRARY lemma (axiom, allowed)",
         _T_OK + "// synth: helper\nlemma L(x: int) ensures x == x;\n", True),
        ("{:axiom} on LIBRARY function (allowed)",
         _T_OK + "// synth: library\nfunction {:axiom} PySum(s: seq<int>): int\n", True),
    ]
    failures = 0
    for name, model, expect_ok in cases:
        rep = check(model, _T_ENTRY)
        ok = rep.ok is expect_ok
        failures += 0 if ok else 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<28} -> "
              f"{'accepted' if rep.ok else 'rejected'}")
        if not ok:
            for v in rep.violations[:2]:
                print(f"         {v}")

    # `decreases *` is legal but must be surfaced, not silently accepted.
    rep = check(_T_OK.replace("  r := 0;", "  while false decreases * { }\n  r := 0;"), _T_ENTRY)
    ok = rep.ok and any("decreases" in w for w in rep.warnings)
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'decreases * warns':<28} -> "
          f"{'warned' if rep.warnings else 'silent'}")

    # Permitting library axioms is only safe if they are actually recorded, since
    # the trusted base is what gets inspected when a mutant verifies.
    rep = check(_T_OK + "// synth: library\nfunction {:axiom} PySum(s: seq<int>): int\n",
                _T_ENTRY)
    ok = rep.ok and any("PySum" in t for t in rep.trusted_base)
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'library axiom is recorded':<28} -> "
          f"trusted_base={len(rep.trusted_base)}")

    rep = check(_T_OK.replace("  ensures r >= 0", "  requires n > 0\n  ensures r >= 0"),
                _T_ENTRY)
    ok = rep.ok and rep.assumed_precondition == ["n > 0"]
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'entry precondition recorded':<28} -> "
          f"assumed_precondition={rep.assumed_precondition}")

    print()
    stage2_legal = _T_OK.replace(
        "{\n// src: source.py:4",
        "{\n// synth: invariant\n  assert 0 == 0;\n// src: source.py:4")
    fr = check_freeze(_T_OK, stage2_legal)
    failures += 0 if fr.ok else 1
    print(f"  [{'OK  ' if fr.ok else 'FAIL'}] {'stage2 adds invariant':<28} -> "
          f"{'allowed' if fr.ok else 'rejected'}")

    fr = check_freeze(_T_OK, _T_OK.replace("  r := 0;", "  r := 1;"))
    failures += 0 if not fr.ok else 1
    print(f"  [{'OK  ' if not fr.ok else 'FAIL'}] {'stage2 edits src line':<28} -> "
          f"{'allowed' if fr.ok else 'rejected'}")

    stage2_deleted = "\n".join(l for l in _T_OK.splitlines() if l.strip() != "r := 0;")
    fr = check_freeze(_T_OK, stage2_deleted)
    failures += 0 if not fr.ok else 1
    print(f"  [{'OK  ' if not fr.ok else 'FAIL'}] {'stage2 deletes src line':<28} -> "
          f"{'allowed' if fr.ok else 'rejected'}")

    print()
    if failures:
        print(f"INTEGRITY SELFTEST FAILED: {failures} case(s)")
        return 1
    print("INTEGRITY SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
