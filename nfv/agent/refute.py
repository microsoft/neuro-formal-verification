#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Formal refutation — proving a program WRONG, unboundedly.

The bounded phase already refutes, but only within an unrolling depth, so it
misses every bug whose witness needs a longer input than the bound admits. This
module gets refutations from the *proof* engine instead, where there is no bound:

    phase 0/0b   library theory + blind precondition   (UNCHANGED)
    phase 1      translate                             (UNCHANGED, byte-identical)
                 then MECHANICALLY invert the entry's postcondition
    phase 2      an agent reads the Python and guesses a violating INPUT,
                 which is installed as an extra precondition
                 then the ordinary proof-repair loop runs on that goal

If the verifier proves `requires <witness> ensures !(P)`, then for every input in
the witness domain the translated postcondition FAILS. Together with a proof that
the domain is non-empty and actually reached, that is a counterexample —
established by proof, with no bound.

WHY THE INVERSION IS MECHANICAL
The plan called for phase 1 to translate against an inverted spec. Asking the
model to write `!P` would put the single most important step -- whether the goal
really is the negation of the postcondition -- inside the untrusted component,
and a model that inverted the wrong conjunct would manufacture counterexamples
that no downstream check could catch. So translation runs with the ORIGINAL
prompt, unchanged and byte-identical to the proof arms, and this module rewrites
the entry's `ensures` clauses afterwards by text surgery:

    ensures A                     {c} synth: refutation goal
    ensures B          ---->      ensures !((A) && (B))

The negation is then correct by construction, and the refutation arm differs from
the proof arm in exactly three places: this rewrite, the witness precondition,
and the gates below. Nothing else varies, so the two are directly comparable.

WHAT MAKES A PROOF OF `!P` A COUNTEREXAMPLE
`ensures !(P)` is vacuously true of any input the method never returns from, so
the proof alone is not enough. Three things are checked:

  G1 REACHABILITY. A copy of the entry method, body intact, with its
     postcondition replaced by `ensures false`. That is exactly "assert false at
     every exit point". If the verifier PROVES it, no input in the domain reaches
     a return -- because the precondition is unsatisfiable, or an `assume`
     upstream killed the path, or the guard is never taken -- and the refutation
     is discarded. Only an explicit *failure* to prove `false` passes the gate;
     `unknown` is treated as a failure of the gate, not of the verifier.
  G2 INVERSION. The entry carries exactly one `ensures`, and it is the negation
     this module wrote. Checked mechanically against the recorded original.
  G3 INTEGRITY. The usual audit on the FINAL model: no `assume`, no `{:axiom}`
     on the entry, no bodyless entry. These already make the proof-side sound;
     they matter more here, because on this goal a false assumption does not lose
     a proof, it invents a bug report.

Termination matters in the same way and is why Dafny is the primary target:
Dafny proves termination by default, so a proved `ensures !(P)` really does
describe a returning execution. Boogie is partial-correctness, so a
non-terminating loop would satisfy any postcondition -- G1 catches the vacuous
cases but a Boogie refutation is a weaker claim, and is recorded as such.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from collections.abc import Iterable

from nfv.agent import integrity as _integrity
from nfv.agent.translate import extract_code, number_source, target_name
from nfv.backends import dafny
from nfv.backends.registry import Backend, get as get_backend
from nfv.shared.copilot_client import CopilotChat

REACH_PROBE = "NfvRefuteReachProbe"

# SMT-LIB has no signature to operate on: the whole artefact is a list of
# assertions, and the goal is the last `(assert (not P))` by the convention
# `nfv.backends.smt` enforces. Refutation there is the same surgery expressed
# differently -- drop the `not` so the solver is asked to prove that P is
# unsatisfiable on the witness domain, which is exactly "the postcondition
# fails everywhere the witness admits".




# Lean has neither a signature nor an assertion list. The goal is a THEOREM, so
# refutation negates its conclusion and installs the witness as an extra
# hypothesis binder. The header of a theorem is everything from the keyword to
# the `:=` that opens the proof; within it, the conclusion is whatever follows
# the last `:` at bracket depth zero, and everything before that is binders.


# Line comments only. Lean's `/- -/` blocks nest, so they are not matched here;
# the pipeline emits `--` tags exclusively.






# Contract keywords that may appear between the parameter list and the body.
_CONTRACT_KW = {
    "dafny": ("requires", "ensures", "decreases", "modifies", "reads"),
}


# --------------------------------------------------------------------------
# Signature surgery
# --------------------------------------------------------------------------

def _split_signature(model: str, entry: str, lang: str) -> tuple[int, int] | None:
    """Character span of the entry's signature: keyword through the char before `{`."""
    sig = _integrity._entry_signature(model, entry, lang)
    if not sig:
        return None
    start = model.find(sig)
    if start < 0:
        return None
    return start, start + len(sig)


def _clause_lines(lines: list[str], lang: str) -> list[tuple[int, int, str]]:
    """Group signature lines into contract clauses.

    Returns `(first_line, last_line_exclusive, keyword)` per clause. A clause
    starts at a line whose first token is a contract keyword and continues over
    following lines that are neither comments, blank, nor another clause start --
    which is how a multi-line `ensures forall ...` stays in one piece.
    """
    kws = _CONTRACT_KW.get(lang, _CONTRACT_KW["dafny"])
    starts: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        tok = ln.strip().split(" ", 1)[0].strip()
        if tok in kws:
            starts.append((i, tok))
    out: list[tuple[int, int, str]] = []
    for n, (i, kw) in enumerate(starts):
        limit = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        j = i + 1
        while j < limit:
            s = lines[j].strip()
            if not s or s.startswith("//") or s.startswith("/*"):
                break
            j += 1
        out.append((i, j, kw))
    return out


@dataclass
class Inversion:
    ok: bool
    model: str = ""
    original_ensures: list[str] = field(default_factory=list)
    inverted: str = ""
    reason: str = ""




def invert_entry_spec(model: str, entry: str, backend: Backend) -> Inversion:
    """Replace the entry's `ensures` clauses with a single negated conjunction.

    This is the whole of "phase 1 inverts the spec", done as text surgery so the
    negation cannot be got wrong by a model. The `ensures` lines are `synth:`
    lines, which Stage 2 is already permitted to rewrite, so the `src:` freeze
    and its hash are untouched.
    """
    lang, c = backend.lang, backend.comment
    span = _split_signature(model, entry, lang)
    if span is None:
        return Inversion(False, reason=f"entry `{entry}` not found")
    start, end = span
    sig = model[start:end]
    lines = sig.split("\n")
    clauses = _clause_lines(lines, lang)
    ens = [(i, j) for (i, j, kw) in clauses if kw == "ensures"]
    if not ens:
        return Inversion(False, reason=f"entry `{entry}` has no `ensures` to invert")

    texts: list[str] = []
    for (i, j) in ens:
        body = "\n".join(lines[i:j]).strip()
        body = re.sub(r"^ensures\b", "", body, count=1).strip()
        body = body.rstrip(";").strip()
        if not body:
            return Inversion(False, reason="empty `ensures` clause")
        texts.append(body)

    conj = " && ".join(f"({t})" for t in texts)
    # Collapse whitespace so the clause survives on one line; the expression
    # language is whitespace-insensitive in both targets.
    conj = re.sub(r"\s+", " ", conj)
    inverted = f"!({conj})"
    term = ""
    new_clause = [f"{c} synth: refutation goal: negation of the translated postcondition",
                  f"  ensures {inverted}{term}"]

    drop = {k for (i, j) in ens for k in range(i, j)}
    # The tag comment immediately above a dropped `ensures` described the clause
    # that is going away, so leaving it would attach a stale provenance note to
    # the refutation goal. Drop it with its clause.
    for (i, _j) in ens:
        if i > 0 and lines[i - 1].strip().startswith(c) and (i - 1) not in drop:
            drop.add(i - 1)
    first = min(k for k in drop) if drop else ens[0][0]
    out: list[str] = []
    for k, ln in enumerate(lines):
        if k == first:
            out.extend(new_clause)
        if k in drop:
            continue
        out.append(ln)
    return Inversion(True, model[:start] + "\n".join(out) + model[end:],
                     original_ensures=texts, inverted=inverted)


def install_precondition(model: str, entry: str, clauses: list[str],
                         backend: Backend, why: str = "refutation witness") -> str:
    """Add `requires` clauses to the entry, immediately before its body."""
    if not clauses:
        return model
    lang, c = backend.lang, backend.comment
    span = _split_signature(model, entry, lang)
    if span is None:
        return model
    start, end = span
    term = ""
    add = [f"{c} synth: {why}"]
    for cl in clauses:
        cl = cl.strip().rstrip(";").strip()
        cl = re.sub(r"^requires\b", "", cl, count=1).strip()
        if cl:
            add.append(f"  requires {cl}{term}")
    sig = model[start:end].rstrip()
    return model[:start] + sig + "\n" + "\n".join(add) + "\n" + model[end:]


def entry_ensures(model: str, entry: str, backend: Backend) -> list[str]:
    """The entry's `ensures` clauses, as written."""
    span = _split_signature(model, entry, backend.lang)
    if span is None:
        return []
    lines = model[span[0]:span[1]].split("\n")
    out = []
    for (i, j, kw) in _clause_lines(lines, backend.lang):
        if kw == "ensures":
            body = "\n".join(lines[i:j]).strip()
            body = re.sub(r"^ensures\b", "", body, count=1).strip().rstrip(";").strip()
            out.append(re.sub(r"\s+", " ", body))
    return out


# --------------------------------------------------------------------------
# G1 — reachability
# --------------------------------------------------------------------------

def _probe_source(model: str, entry: str, backend: Backend) -> str | None:
    """A copy of the entry method whose postcondition is `ensures false`.

    Keeping the BODY is the whole point: a probe built from the preconditions
    alone would pass happily while an `assume` inside the body was making the
    real goal vacuous. `ensures false` is "assert false at every exit point", so
    it fails exactly when some input in the domain returns.
    """
    lang = backend.lang
    span = _split_signature(model, entry, lang)
    if span is None:
        return None
    start, end = span
    # Find the matching close brace of the body so the copy is self-contained.
    rest = model[end:]
    if not rest.lstrip().startswith("{"):
        return None
    open_at = end + (len(rest) - len(rest.lstrip()))
    depth, idx = 0, open_at
    while idx < len(model):
        if model.startswith("{:", idx):
            close = model.find("}", idx)
            if close == -1:
                return None
            idx = close + 1
            continue
        if model[idx] == "{":
            depth += 1
        elif model[idx] == "}":
            depth -= 1
            if depth == 0:
                break
        idx += 1
    if depth != 0:
        return None
    body = model[open_at:idx + 1]

    sig = model[start:end]
    lines = sig.split("\n")
    ens = [(i, j) for (i, j, kw) in _clause_lines(lines, lang) if kw == "ensures"]
    drop = {k for (i, j) in ens for k in range(i, j)}
    kept = [ln for k, ln in enumerate(lines) if k not in drop]
    sig2 = "\n".join(kept)
    # Rename only the declared name, not recursive call sites: a recursive call
    # should still target the real method.
    sig2 = re.sub(rf"\b{re.escape(entry)}\b", REACH_PROBE, sig2, count=1)
    term = ""
    return f"\n\n{sig2.rstrip()}\n  ensures false{term}\n{body}\n"






def reachable(model: str, entry: str, backend: Backend,
              timeout: float = 60.0) -> tuple[bool | None, str]:
    """G1: can any admitted input actually return?

    `True`  -> `false` could NOT be proved, so some execution returns: gate passes.
    `False` -> `false` WAS proved: the goal is vacuous; discard the refutation.
    `None`  -> inconclusive; treated by the caller as a gate failure.
    """
    probe = _probe_source(model, entry, backend)
    if probe is None:
        return None, "could not build the reachability probe"
    try:
        res = backend.run(model + probe, mode="unbounded", timeout=timeout,
                          filter_symbol=REACH_PROBE)
    except Exception as exc:
        return None, f"probe error: {type(exc).__name__}: {exc}"
    if res.outcome is dafny.Outcome.VERIFIED:
        return False, "VACUOUS: `ensures false` was proved, so no admitted input returns"
    if res.outcome is dafny.Outcome.VERIFICATION_FAILED:
        return True, "reachable: `false` could not be proved at the exit points"
    return None, f"inconclusive reachability probe ({res.outcome.value})"


# --------------------------------------------------------------------------
# Phase 2 — the input guess
# --------------------------------------------------------------------------

SYSTEM = """\
You find INPUTS THAT BREAK PROGRAMS, expressed as a {target} precondition.

You are given a Python function, its intended postcondition, and a {target}
translation of it whose goal has been NEGATED: the verifier is being asked to
prove that the postcondition FAILS. That proof only goes through on a domain
where the program really is wrong, so your job is to name such a domain.

WHAT TO PRODUCE
One or more `requires` clauses that, taken together, describe inputs on which you
believe the postcondition does NOT hold. Prefer the SMALLEST domain you are
confident about -- ideally a single concrete input, pinned exactly:

    requires n == 3
    requires |s| == 2 && s[0] == 1 && s[1] == 1

A fully concrete input is worth far more than a clever general one: it is easy
for the verifier to evaluate, and it either works or it does not.

HOW TO FIND IT
Read the Python for the boundary the author got wrong -- an off-by-one, an empty
or single-element input, a branch that never fires, a base case, an accumulator
that starts at the wrong value, a comparison that should have been strict. Then
pick the smallest input that drives execution through it. Trace that input by
hand through the code and confirm the result really does violate the
postcondition before you answer.

CONSTRAINTS
- Your clauses must be SATISFIABLE and must be consistent with any precondition
  already on the method. A domain that admits no input proves the negated goal
  vacuously; that is checked, and it is scored as a failure, not a refutation.
- Constrain ONLY the parameters, and mention nothing else. Do not mention the
  return value, do not restate the postcondition, and do not constrain the
  value of a library or helper function -- writing `Lib(x) == 3` asserts
  something about the environment rather than choosing an input, and a
  refutation that needs such an assertion is not evidence that the program is
  wrong. If you believe a library call returns a particular value on your
  input, you do not need to say so: the verifier already knows the library's
  specification, and will work it out from the input alone.
- Write {target} expression syntax, using the program's own parameter names.
{pre_block}

OUTPUT
A fenced ```{fence} block containing ONLY the `requires` lines, then one line of
prose after the block explaining, in a sentence, what goes wrong on that input.
"""

USER_TEMPLATE = """\
Python source:
```python
{numbered_source}
```

Entry point: {entry_point}

Documentation:
\"\"\"{docstring}\"\"\"

Intended postcondition (the property the program is supposed to satisfy; the
verifier has been asked to prove its NEGATION):
```python
{postcondition}
```

The model under test, with the negated goal:
```{fence}
{model}
```
{history}
Give the `requires` clauses describing an input on which the postcondition fails.
"""

HISTORY_TEMPLATE = """
ATTEMPTS ALREADY MADE -- do not repeat these, and take the outcome into account:
{items}
"""


@dataclass
class InputGuess:
    ok: bool
    clauses: list[str] = field(default_factory=list)
    rationale: str = ""
    reason: str = ""
    raw: str = ""


_REQ = re.compile(r"^\s*requires\b(.*)$", re.MULTILINE)

# Identifiers that are part of the expression language rather than a reference to
# something in the model, so seeing them in a witness means nothing.
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_STRLIT = re.compile(r'"[^"]*"' + r"|'[^']*'")
_NOISE = frozenset("""
true false forall exists in int real bool nat char string seq set iset multiset
map imap array object old if then else let match case var this fresh
""".split())


def entry_param_names(model: str, entry: str, backend: Backend) -> list[str]:
    """Names of the entry method's INPUT parameters (not its return values)."""
    lang = backend.lang
    kw = "method"
    m = re.search(rf"\b{kw}\s+(?:\{{:[^}}]*\}}\s*)*{re.escape(entry)}\s*\(([^)]*)\)", model)
    if not m:
        return []
    names: list[str] = []
    depth, cur = 0, ""
    for ch in m.group(1) + ",":
        if ch in "<[(":
            depth += 1
        elif ch in ">])":
            depth -= 1
        if ch == "," and depth == 0:
            lhs = cur.split(":")[0]
            names += [t.strip() for t in lhs.split(",") if t.strip()]
            cur = ""
            continue
        cur += ch
    return [n for n in names if n]


def datatype_constructors(model: str) -> set[str]:
    """Constructor names declared by the model's own `datatype` definitions.

    A constructor is a way of WRITING A VALUE, not a reference to anything in the
    environment. `lst == [PythonFloat(-1.0)]` picks an input exactly as
    `n == 3` does; the only difference is that the value domain is a tagged union
    rather than a machine type. Treating such a name as a foreign symbol
    penalises precisely the faithful translations -- the ones that model Python's
    dynamic values properly instead of flattening them to `seq<int>` -- which is
    the opposite of the intended incentive.
    """
    out: set[str] = set()
    for body in re.finditer(
            r"\bdatatype\s+\w+(?:<[^>]*>)?\s*=(.*?)(?=\n\s*\n|\n(?:datatype|function|"
            r"predicate|method|procedure|lemma|type|const|class)\b|\Z)", model, re.S):
        txt = body.group(1)
        # `Finite(realValue: real) | PositiveInfinity | Unsupported`
        out.update(re.findall(r"([A-Za-z_]\w*)\s*\(", txt))
        out.update(re.findall(r"(?:=|\|)\s*([A-Za-z_]\w*)\s*(?![\w(])", txt))
    return {c for c in out if c not in _NOISE}


def witness_foreign_symbols(clauses: list[str], params: list[str],
                            constructors: Iterable[str] = ()) -> list[str]:
    """Identifiers in the witness that are not input parameters.

    A witness is supposed to pin the INPUT. When it instead constrains a library
    term -- `PyIntValue(PyPowHalf(n)) == 0`, observed in the smoke run -- it is
    no longer only choosing an input: it is asserting something about the
    environment, and if that assertion is false the refutation is spurious. On
    the proof side this would be called enlarging the trusted base. It is
    recorded rather than rejected, because the constraint is often true and the
    underlying bug real, but a refutation carrying one is not reported as clean.

    String and character literals are stripped first. Their contents are DATA,
    not references to anything in the model: the witness `test == "a a aa"` is
    a pure input constraint, and reading `a` and `aa` out of it as environment
    symbols wrongly disqualifies a clean refutation. Measured on the full run,
    this mis-flagged three refutations.

    Datatype constructors are exempt for the same reason: they denote values, not
    environment facts. See `datatype_constructors`.
    """
    keep = set(params) | set(constructors)
    out: list[str] = []
    for cl in clauses:
        for m in _IDENT.finditer(_STRLIT.sub(" ", cl)):
            tok = m.group(0)
            if tok in keep or tok in _NOISE or tok in out:
                continue
            out.append(tok)
    return out


def parse_guess(text: str, backend: Backend) -> InputGuess:
    code = extract_code(text) or ""
    found = [m.group(1).strip().rstrip(";").strip() for m in _REQ.finditer(code)]
    if not found:
        # The model may have dropped the keyword and given bare expressions.
        found = [ln.strip().rstrip(";").strip() for ln in code.splitlines()
                 if ln.strip() and not ln.strip().startswith(backend.comment)]
    found = [f for f in found if f]
    if not found:
        return InputGuess(False, reason="no `requires` clause in the response", raw=text)
    tail = text.split("```")[-1].strip() if "```" in text else ""
    return InputGuess(True, clauses=found, rationale=tail[:400], raw=text)


async def guess_input(
    chat: CopilotChat,
    *,
    python_source: str,
    docstring: str,
    postcondition: str,
    entry_point: str,
    model: str,
    backend: Backend | str = "dafny",
    history: list[str] | None = None,
    timeout: float = 600.0,
) -> InputGuess:
    """Phase 2: name a domain on which the postcondition should fail."""
    bk = get_backend(backend) if isinstance(backend, str) else backend
    system = (SYSTEM.replace("{target}", target_name(bk))
                    .replace("{fence}", bk.lang)
                    .replace("{pre_block}", ""))
    hist = ""
    if history:
        hist = HISTORY_TEMPLATE.format(
            items="\n".join(f"  {i}. {h}" for i, h in enumerate(history, 1)))
    user = USER_TEMPLATE.format(
        numbered_source=number_source(python_source),
        entry_point=entry_point,
        docstring=(docstring or "").strip() or "(no documentation)",
        postcondition=postcondition.strip(),
        fence=bk.lang,
        model=model,
        history=hist,
    )
    resp = await chat.ask(system, user, timeout=timeout)
    g = parse_guess(resp.text, bk)
    return g


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

_M = """\
// synth: library
function {:axiom} PyLen(s: seq<int>): int

// src: source.py:1 | def f(n):
method f(n: int) returns (r: seq<int>)
// synth: precondition from docstring
  requires n >= 0
// synth: postcondition from provided assertion
  ensures |r| == n + 1
  ensures n >= 0
// synth: method body
{
// src: source.py:2 | return [0]
  return [0];
}
"""


