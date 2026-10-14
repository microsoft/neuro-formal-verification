"""Reference documentation for the Python builtins a program actually calls.

Motivation. F5/F7/F12 traced soundness violations to INVENTED library specs, and
F20 showed every violation in the study falls on a library-using program. The
model is asked to axiomatise `sum`, `max`, `sorted`, slicing and so on while
being told nothing authoritative about what they mean, so it writes an axiom
from memory. This module supplies the missing reference text.

Two properties matter for the experiment:

* It is DETERMINISTIC and offline. Docstrings come from `inspect.getdoc` on the
  running interpreter's own builtins, plus a small hand-written table for
  semantics Python does not document in a usable form (slicing, `//`, `%` on
  negatives, list mutation). No network, and nothing about the program under
  test is executed -- this is a reference manual, not a test run.
* It is SELECTED BY THE SOURCE, not by the outcome. Only entries for calls the
  program actually makes are emitted, using the same AST walk that defines the
  L0-L3 tiers, so the block cannot leak anything about whether a proof succeeds.

The block is advisory. Nothing here enters the trusted base: the model must
still write its own axioms, which are still recorded and audited. The
hypothesis is only that a correct reference produces a more faithful axiom.
"""

from __future__ import annotations

import ast
import builtins
import inspect

# Semantics that matter for a faithful axiom but that Python either does not
# document or documents somewhere a docstring will not reach. Kept deliberately
# short and neutral: these state what Python DOES, never what a proof needs.
_EXTRA: dict[str, str] = {
    "slice": (
        "Sequence slicing `s[a:b]`: returns elements from index a up to but NOT\n"
        "including b. Out-of-range bounds are CLAMPED, never an error: if b >\n"
        "len(s) the slice stops at len(s), and if a >= b the result is empty.\n"
        "Negative indices count from the end: s[-1] is the last element.\n"
        "Note this differs from indexing: s[i] with i >= len(s) RAISES."
    ),
    "index": (
        "Sequence indexing `s[i]` raises IndexError when i < 0 is not a valid\n"
        "negative index or when i >= len(s). A faithful translation must make\n"
        "the index obligation explicit rather than assume it away."
    ),
    "floordiv": (
        "`a // b` is FLOOR division: it rounds toward negative infinity, so\n"
        "-7 // 2 == -4, not -3. `a % b` takes the sign of b, so -7 % 2 == 1.\n"
        "Both raise ZeroDivisionError when b == 0."
    ),
    "truediv": (
        "`a / b` is float division and raises ZeroDivisionError when b == 0."
    ),
    "list_mutation": (
        "`lst.append(x)` extends the list by one element and returns None.\n"
        "`lst.pop()` removes and returns the LAST element and raises IndexError\n"
        "on an empty list. Both mutate the list in place."
    ),
}

# Which extra notes to attach when a syntactic form appears in the source.
_FORM_NOTES = {
    "slice": ("slice",),
    "index": ("index",),
    "floordiv": ("floordiv",),
    "truediv": ("truediv",),
}

_METHOD_OWNERS = (str, list, dict, set)

# Docstrings for these are long, unhelpful, or absent; skip rather than pad the
# prompt with noise.
_SKIP = {"print", "range", "len"}

# The fixed vocabulary the example-agnostic corpus documents. Chosen from the
# Python library itself rather than from the benchmark, so that adding or
# removing entries from the dataset cannot change a single prompt.
_CORPUS_BUILTINS = (
    "abs", "all", "any", "bin", "bool", "chr", "dict", "divmod", "enumerate",
    "filter", "float", "hex", "int", "isinstance", "list", "map", "max", "min",
    "oct", "ord", "pow", "reversed", "round", "set", "sorted", "str", "sum",
    "tuple", "zip",
)
_CORPUS_METHODS = (
    "add", "append", "count", "extend", "find", "get", "index", "insert",
    "intersection", "isalpha", "isdigit", "islower", "isnumeric", "isupper",
    "join", "keys", "lower", "lstrip", "pop", "remove", "replace", "reverse",
    "rstrip", "sort", "split", "startswith", "strip", "swapcase", "title",
    "upper", "values",
)


def _builtin_doc(name: str) -> str | None:
    obj = getattr(builtins, name, None)
    if obj is None:
        return None
    doc = inspect.getdoc(obj)
    if not doc:
        return None
    # First paragraph only: the rest is usually parameter boilerplate.
    para = doc.split("\n\n")[0].strip()
    return " ".join(para.split()) if para else None


def _method_doc(attr: str) -> str | None:
    for owner in _METHOD_OWNERS:
        fn = getattr(owner, attr, None)
        if fn is None:
            continue
        doc = inspect.getdoc(fn)
        if doc:
            para = doc.split("\n\n")[0].strip()
            return f"{owner.__name__}.{attr}: " + " ".join(para.split())
    return None


def collect(source: str) -> tuple[list[str], list[str]]:
    """(builtin/method names called, syntactic forms used) for one program."""
    tree = ast.parse(source)
    local = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    names: list[str] = []
    forms: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id not in local:
                names.append(f.id)
            elif isinstance(f, ast.Attribute):
                names.append("." + f.attr)
        elif isinstance(n, ast.Subscript):
            forms.add("slice" if isinstance(n.slice, ast.Slice) else "index")
        elif isinstance(n, ast.BinOp):
            if isinstance(n.op, ast.FloorDiv) or isinstance(n.op, ast.Mod):
                forms.add("floordiv")
            elif isinstance(n.op, ast.Div):
                forms.add("truediv")
    # Deduplicate, preserving first-seen order so the block is stable.
    seen: set[str] = set()
    ordered = [x for x in names if not (x in seen or seen.add(x))]
    return ordered, sorted(forms)


def reference_corpus() -> str:
    """The WHOLE Python reference, identical for every entry.

    `reference_block` tailors its selection to the builtins a given program
    calls, which makes the prompt example-specific: 145 distinct blocks across
    the 206-entry benchmark. That is a property of the entry leaking into the
    prompt, so the arms are not strictly comparable and the model is told, by
    omission, which operations matter.

    The corpus is small enough that the tailoring buys nothing worth having
    (~55 lines, ~4.8k characters), so this hands the same text to everyone. The
    content is library documentation either way; only the selection changes.
    """
    lines: list[str] = []
    for name in sorted(_CORPUS_BUILTINS):
        if name in _SKIP:
            continue
        doc = _builtin_doc(name)
        if doc:
            lines.append(f"- `{name}`: {doc}")
    for attr in sorted(_CORPUS_METHODS):
        doc = _method_doc(attr)
        if doc:
            lines.append(f"- `.{attr}`: {doc}")
    for key in sorted(_EXTRA):
        lines.append("- " + _EXTRA[key].replace("\n", "\n  "))
    return (
        "PYTHON REFERENCE (authoritative; use it when writing specs or axioms "
        "for these\noperations, in preference to your own recollection). It "
        "describes the library,\nnot this program, and says nothing about "
        "whether this program is correct. The\nsame reference is supplied for "
        "every program, so most entries below will be\nirrelevant to this "
        "one.\n\n" + "\n".join(lines) + "\n"
    )


def reference_block(source: str) -> str:
    """The documentation block for a program, or "" when it needs none."""
    names, forms = collect(source)
    lines: list[str] = []

    for name in names:
        if name in _SKIP:
            continue
        doc = _method_doc(name[1:]) if name.startswith(".") else _builtin_doc(name)
        if doc:
            label = name if name.startswith(".") else name
            lines.append(f"- `{label}`: {doc}")

    extras: list[str] = []
    for form in forms:
        for key in _FORM_NOTES.get(form, ()):
            if key not in extras:
                extras.append(key)
    if any(n in (".append", ".pop") for n in names):
        extras.append("list_mutation")

    for key in extras:
        body = _EXTRA[key].replace("\n", "\n  ")
        lines.append(f"- {body}")

    if not lines:
        return ""
    return (
        "PYTHON REFERENCE (authoritative; use it when writing specs or axioms "
        "for these\noperations, in preference to your own recollection). It "
        "describes the library,\nnot this program, and says nothing about "
        "whether this program is correct.\n\n" + "\n".join(lines) + "\n"
    )


def _selftest() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    b = reference_block("def f(l):\n    return sum(l) / len(l)\n")
    check("sum documented", "`sum`" in b)
    check("division note emitted", "ZeroDivisionError" in b)
    check("len skipped", "`len`" not in b)

    b2 = reference_block("def f(l):\n    return [max(l[:i + 1]) for i in range(len(l))]\n")
    check("max documented", "`max`" in b2)
    check("slice clamping documented", "CLAMPED" in b2)

    b3 = reference_block("def f(n):\n    return n + 1\n")
    check("no calls -> empty block", b3 == "")

    b4 = reference_block("def f(l):\n    l.append(1)\n    return l\n")
    check("append documented", "append" in b4)

    check("deterministic", reference_block("def f(l):\n    return sum(l)\n")
          == reference_block("def f(l):\n    return sum(l)\n"))

    # The corpus must not vary with the program: that is the whole point of
    # having it, so it is worth a test rather than a comment.
    check("corpus is example-agnostic",
          reference_corpus() == reference_corpus())
    check("corpus mentions operations the program never calls",
          "`sorted`" in reference_corpus() and "`zip`" in reference_corpus())

    print("\nPYDOCS SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
