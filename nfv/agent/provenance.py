#!/usr/bin/env python3
"""
Provenance audit for translated models.

Every line the translator emits must be traceable:

    // src: source.py:12 | sorted_numbers = sorted(numbers)
    var sortedNumbers := Sorted(numbers);
    // synth: helper Sorted
    function Sorted(s: seq<real>): seq<real> { ... }

`// src:` lines are derived from the Python source and quote it verbatim.
`// synth:` lines have no source counterpart (declarations, helpers, invariants,
`decreases` clauses). Untagged code is rejected: without that rule the translator
could hide rewritten logic in lines the audit cannot see.

The audit catches the ways a translator can silently "fix" a buggy program:

  * altered statement  -> quoted text does not match the source line
  * dropped statement  -> a source line is never referenced
  * reordered logic    -> `// src:` refs are not in non-decreasing order
  * fabricated tags    -> reference to a nonexistent line
  * hidden logic       -> untagged code

It also underpins the Stage-1/Stage-2 freeze: `// src:` lines are immutable once
verification starts, so the proof agent can only touch `// synth:` lines.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

SRC_TAG = re.compile(r"^\s*//\s*src:\s*(?P<file>[^:]+):(?P<line>\d+)\s*\|\s?(?P<text>.*?)\s*$")
SYNTH_TAG = re.compile(r"^\s*//\s*synth:\s*(?P<reason>.*?)\s*$")

# SMT-LIB has no `//` comment, so the tag syntax is parameterised by the target
# language's line-comment marker. The tag GRAMMAR is identical everywhere, which
# is what keeps the audit -- and the prompt describing it -- the same across
# backends.
_TAGS: dict[str, tuple[re.Pattern, re.Pattern]] = {}


def tags_for(comment: str = "//") -> tuple[re.Pattern, re.Pattern]:
    if comment not in _TAGS:
        c = re.escape(comment)
        _TAGS[comment] = (
            re.compile(rf"^\s*{c}\s*src:\s*(?P<file>[^:]+):(?P<line>\d+)\s*\|\s?(?P<text>.*?)\s*$"),
            re.compile(rf"^\s*{c}\s*synth:\s*(?P<reason>.*?)\s*$"),
        )
    return _TAGS[comment]

# Lines that carry no logic and therefore need no tag.
_STRUCTURAL = re.compile(r"^[\s{}()\[\];]*$")


@dataclass
class ProvenanceReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    src_refs: list[int] = field(default_factory=list)
    synth_count: int = 0
    untagged: list[int] = field(default_factory=list)
    uncovered_source_lines: list[int] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return (f"provenance OK: {len(self.src_refs)} src refs, "
                    f"{self.synth_count} synth lines")
        return f"provenance FAILED: {len(self.violations)} violation(s)"


def significant_source_lines(source: str) -> set[int]:
    """1-based lines that must appear in the translation.

    Imports are exempt: `from typing import List` has no Dafny counterpart. Blank
    lines are exempt. Everything else is program logic and must be translated.
    """
    tree = ast.parse(source)
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                exempt.add(ln)

    significant = set()
    for i, raw in enumerate(source.splitlines(), start=1):
        if not raw.strip():
            continue
        if i in exempt:
            continue
        significant.add(i)
    return significant


def check(model: str, source: str, source_name: str = "source.py",
          comment: str = "//") -> ProvenanceReport:
    """Audit `model` (Dafny/Boogie/SMT-LIB text) against the Python `source`."""
    rep = ProvenanceReport(ok=True)
    src_tag, synth_tag = tags_for(comment)
    src_lines = source.splitlines()
    n_src = len(src_lines)

    current_tag: str | None = None
    referenced: set[int] = set()
    order: list[int] = []

    for idx, raw in enumerate(model.splitlines(), start=1):
        stripped = raw.strip()

        m_src = src_tag.match(raw)
        if m_src:
            current_tag = "src"
            fname = m_src.group("file").strip()
            lineno = int(m_src.group("line"))
            quoted = m_src.group("text").strip()

            if fname != source_name:
                rep.violations.append(
                    f"model line {idx}: tag names '{fname}', expected '{source_name}'")
            if not (1 <= lineno <= n_src):
                rep.violations.append(
                    f"model line {idx}: fabricated provenance, source line {lineno} "
                    f"does not exist (source has {n_src} lines)")
                continue

            actual = src_lines[lineno - 1].strip()
            if quoted != actual:
                rep.violations.append(
                    f"model line {idx}: quoted text does not match {source_name}:{lineno}\n"
                    f"    quoted: {quoted!r}\n"
                    f"    actual: {actual!r}")
            referenced.add(lineno)
            order.append(lineno)
            continue

        if synth_tag.match(raw):
            current_tag = "synth"
            rep.synth_count += 1
            continue

        if not stripped or _STRUCTURAL.match(stripped) or stripped.startswith(comment):
            continue

        if current_tag is None:
            rep.untagged.append(idx)

    if rep.untagged:
        shown = ", ".join(str(i) for i in rep.untagged[:10])
        rep.violations.append(
            f"{len(rep.untagged)} untagged code line(s) in model (lines {shown}); "
            f"all code must be {comment} src: or {comment} synth: tagged")

    significant = significant_source_lines(source)
    missing = sorted(significant - referenced)
    if missing:
        rep.uncovered_source_lines = missing
        detail = "; ".join(f"{ln}: {src_lines[ln-1].strip()!r}" for ln in missing[:5])
        rep.violations.append(
            f"{len(missing)} source line(s) never translated (dropped statements) — {detail}")

    non_decreasing = all(a <= b for a, b in zip(order, order[1:]))
    if not non_decreasing:
        bad = [(a, b) for a, b in zip(order, order[1:]) if a > b][:3]
        rep.violations.append(
            f"// src: references are not in source order (reordered logic): {bad}")

    rep.src_refs = order
    rep.ok = not rep.violations
    return rep


_T_SOURCE = """from typing import List

def f(n: int) -> int:
    r = 0
    if n > 0:
        r = n
    return r
"""

_T_GOOD = """// src: source.py:3 | def f(n: int) -> int:
method f(n: int) returns (r: int)
// synth: ensures translated from the postcondition
  ensures r >= 0
{
// src: source.py:4 | r = 0
  r := 0;
// src: source.py:5 | if n > 0:
  if n > 0 {
// src: source.py:6 | r = n
    r := n;
  }
// src: source.py:7 | return r
  return r;
}
"""


def _selftest() -> int:
    """Each attack the audit exists to stop must actually be caught."""
    cases: list[tuple[str, str, bool]] = [
        ("faithful translation", _T_GOOD, True),
        # Statement altered while claiming fidelity.
        ("altered quote", _T_GOOD.replace(
            "// src: source.py:6 | r = n", "// src: source.py:6 | r = n + 1"), False),
        # The classic silent bug-fix: drop the offending line.
        ("dropped statement", "\n".join(
            ln for ln in _T_GOOD.splitlines()
            if "source.py:6" not in ln and ln.strip() != "r := n;"), False),
        # Logic hidden where the audit cannot see it.
        ("untagged code", _T_GOOD.replace(
            "// src: source.py:4 | r = 0\n", ""), False),
        ("fabricated line number", _T_GOOD.replace("source.py:6", "source.py:99"), False),
        ("wrong file", _T_GOOD.replace("source.py:4", "other.py:4"), False),
    ]
    failures = 0
    for name, model, expect_ok in cases:
        rep = check(model, _T_SOURCE)
        ok = rep.ok is expect_ok
        failures += 0 if ok else 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<26} -> "
              f"{'accepted' if rep.ok else 'rejected'}")
        if not ok:
            for v in rep.violations[:2]:
                print(f"         {v.splitlines()[0]}")

    # Reordering must be caught even though every line is present and correct.
    lines = _T_GOOD.splitlines()
    i4 = next(i for i, l in enumerate(lines) if "source.py:4" in l)
    i7 = next(i for i, l in enumerate(lines) if "source.py:7" in l)
    reordered = lines[:i4] + lines[i7:i7 + 2] + lines[i4:i7] + lines[i7 + 2:]
    rep = check("\n".join(reordered), _T_SOURCE)
    ok = not rep.ok
    failures += 0 if ok else 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {'reordered logic':<26} -> "
          f"{'accepted' if rep.ok else 'rejected'}")

    print()
    if failures:
        print(f"PROVENANCE SELFTEST FAILED: {failures} case(s)")
        return 1
    print("PROVENANCE SELFTEST PASSED")
    return 0


def src_fingerprint(model: str, comment: str = "//") -> list[tuple[int, str]]:
    """The `src:`-tagged content of a model, used to enforce the freeze.

    Returns (source_line, translated_code) pairs. Stage 2 may not change this.
    """
    src_tag, synth_tag = tags_for(comment)
    out: list[tuple[int, str]] = []
    pending: int | None = None
    for raw in model.splitlines():
        m = src_tag.match(raw)
        if m:
            pending = int(m.group("line"))
            continue
        if synth_tag.match(raw):
            pending = None
            continue
        stripped = raw.strip()
        if pending is not None and stripped and not stripped.startswith(comment):
            out.append((pending, stripped))
    return out


if __name__ == "__main__":
    import sys

    raise SystemExit(_selftest())
