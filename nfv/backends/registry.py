"""Dafny and CBMC registry, extracted from the pinned paper source."""
from __future__ import annotations

from dataclasses import dataclass, replace

from typing import Callable

from nfv.agent import c_integrity

from nfv.agent import integrity as dafny_integrity

from nfv.backends import cbmc as cbmc_backend

from nfv.backends import dafny as dafny_backend

@dataclass(frozen=True)
class Backend:
    name: str
    lang: str               # markdown fence + `lang` passed to the audits
    comment: str            # line-comment marker used by the provenance tags
    ext: str
    supports_bounded: bool
    run: Callable           # (source, mode=, unroll=, timeout=, ...) -> result
    type_check: Callable
    integrity_check: Callable   # (model, entry) -> IntegrityReport
    syntax_guide: str
    proof_guide: str
    # The three blocks of the shared prompt that must name a language construct.
    # They are supplied per backend so that the SURROUNDING prompt text is
    # literally the same string for every arm. The Dafny values are copied
    # verbatim from the prompt used by the already-completed Dafny runs, so the
    # Dafny arm is byte-identical to those runs and remains comparable.
    spec_block: str
    pre_block: str
    lib_block: str
    # When true, Stage 2 may add whitelisted proof-side annotations to `src:`
    # lines (the freeze protects computational content rather than bytes).
    # Default False so every existing arm keeps the strict byte freeze.
    relax_src_freeze: bool = False
    # Inject authoritative Python documentation for the builtins the program
    # actually calls, so library axioms are written against a reference rather
    # than from recollection (F5/F7/F12/F20).
    python_reference: bool = False
    # Permit an abduction round that may add a precondition ONLY to discharge an
    # internal well-definedness obligation, never to make the postcondition true.
    abduce_definedness: bool = False
    # Phase 0b: synthesise the entry precondition from the source and DOCSTRING
    # before the postcondition is visible, then freeze it. Implies that the
    # abduction round is off -- the whole point is that no later phase, all of
    # which see the goal, may restrict the input domain.
    phase0_precondition: bool = False
    # Carry the Python source and the library reference into every repair round,
    # so the proof agent can see what it is proving about rather than only the
    # verifier's complaint.
    repair_context: bool = False
    # Run phase 0: synthesise a documented, type-checked library theory from the
    # Python source ALONE (no docstring, no postcondition, no label), reproduce
    # it verbatim in the model, and freeze it against repair. Moves library
    # assumptions out from under goal pressure (F5/F7/F12/F24).
    library_theory: bool = False
    # Superseded, but kept registered: F23 and F24 were measured on these arms,
    # and deleting them would make published results unreproducible. Selecting a
    # deprecated arm warns and still runs.
    deprecated: str = ""
    # THE NAIVE BASELINE. Every discipline in this pipeline is off: the prompts
    # lose the transliterator framing, the provenance tags and the rules that
    # exist to feed a gate, and NO gate rejects anything -- no provenance check,
    # no integrity check, no source freeze, no precondition freeze, no artifact
    # audit. The verdict is whatever the verifier says about whatever the model
    # wrote. This exists to price the rest of the design: a baseline that is
    # allowed to cheat, and whose unsound proofs are therefore the measurement
    # rather than a bug. One instruction survives -- that the Python may contain
    # bugs and must not be fixed -- because without it the model rewrites the
    # mutant into the reference and the benchmark measures nothing at all.
    naive: bool = False
    # A BOUNDED MODEL CHECKER, whose failures are evidence rather than the
    # absence of evidence. The other backends fail to find a proof, and that is
    # compatible with the program being correct, so their non-proofs abstain.
    # CBMC instead constructs a concrete execution -- an assignment to every
    # input and the value of every intermediate along the way -- so a failure
    # of the goal assertion refutes the postcondition outright. Setting this
    # makes the bounded stage the WHOLE run: there is no unbounded call to make,
    # since raising the unwinding bound until the tool stops objecting would
    # report a proof it never established. So a violating execution is
    # Incorrect, and everything else, including a clean run, abstains.
    two_sided: bool = False

_DAFNY_SYNTAX = ""

_DAFNY_WD_PROOF = """\


WELL-DEFINEDNESS ON FROZEN LINES. For this run the freeze protects the
COMPUTATIONAL CONTENT of a `// src:` line, not its exact text. Dafny often
rejects a faithful translation because a subexpression is not shown to be
well-defined -- most commonly a lambda in `seq(n, i => ... s[i] ...)`, where the
index has no known bound, giving "index out of range".

You MAY therefore add a `requires` (and `reads`) clause to a LAMBDA HEADER on a
`// src:` line:

    return seq(|l|, i => l[i] + 1);                        // rejected: index out of range
    return seq(|l|, i requires 0 <= i < |l| => l[i] + 1);  // accepted

This is checked mechanically, and it is permitted because it changes no value
the program computes: it restricts the DOMAIN of a function value, so the
well-definedness obligation moves to the use site, where `seq` must still prove
every index is in range. Nothing is assumed away.

Everything else about the frozen line stays illegal, and is rejected
automatically: changing an index expression, a bound, an operator, or a cast,
and adding or removing any computation. If a line is wrong in any of those ways,
leave it alone -- the program may simply be incorrect.
"""

_DAFNY_LEMMA_PROOF = """\


PROOF STRATEGY. Do not attempt to close the goal in one step. A postcondition
that the verifier rejects outright is usually provable once the reasoning is
broken into named pieces, so work as follows.

1. State the INDUCTIVE CONTENT of each loop as an invariant that mentions every
   variable the loop writes, including the relationship between the accumulator
   and the prefix of the input processed so far.
2. Put an `assert` at each point where you believe a non-obvious fact holds --
   after a loop, on each branch of a conditional, and immediately before the
   `return`. An assert that fails tells you exactly where your reasoning and the
   verifier's part company; an assert that succeeds becomes a waypoint the
   verifier can reuse.
3. When a fact is about a RECURSIVE or QUANTIFIED property (a fold over a
   sequence, a property of every prefix, a relation between `f(s)` and
   `f(s[..|s|-1])`), state it as a `lemma` with an explicit body, and CALL that
   lemma where it is needed. Prove the lemma by induction: recurse on the
   smaller case, then let the verifier close the step.
4. Prefer several small lemmas, each with a one-line statement, over one large
   one. A lemma you cannot prove is a signal that the fact is false -- consider
   that the program may simply be incorrect.

Introduce spec `function`s / `predicate`s to name a concept you refer to more
than once; a definition cannot be wrong, whereas an axiom can.
"""

_DAFNY_PROOF = ""

_DAFNY_LEMMA4_PROOF = """\


TURNING THIS ROUND'S FAILURE INTO A NAMED FACT. The diagnostics above are the
only new information you have. Do not re-emit last round's annotations with the
asserts shuffled: each round should leave behind at least one NEW named `lemma`
or spec `function` that was not there before, or a specific reason none is
needed. Work from the failure backwards.

1. NAME THE MISSING FACT. For each unproved assertion, write down the one
   equation or implication that would close it, in terms of the definitions you
   already have. That sentence is the `ensures` of your next lemma.
2. DECIDE WHICH KIND OF LEMMA IT IS. There are two, and they are found by
   different means.

   (a) STRUCTURAL -- the fact is about a recursive definition or holds of every
   prefix. Prove it by induction: recurse on the smaller case and let the
   verifier close the step. This is the kind you find by reading the DEFINITION.

       lemma ShiftedSum(l: seq<real>)
         ensures Sum(Shift(l)) == Sum(l) + |l| as real
       { if |l| > 0 {
           assert Shift(l)[1..] == Shift(l[1..]);   // fix the shape
           ShiftedSum(l[1..]);                      // induction hypothesis
         } }

   (b) ARITHMETIC -- the fact is shallow, you can see it is true, and the
   verifier still will not take it. This happens when a term is NONLINEAR:
   division by a symbolic quantity, a product of two variables, a modulus
   against a non-literal. The solver is not missing a definition, it is
   declining to reason. Isolate the arithmetic in its own lemma over plain
   `real`/`int` parameters, away from the sequences, and prove it with the one
   or two rewriting steps the solver WILL take. This is the kind you find by
   reading the FAILURE, not the definition.

       lemma DivDistrib(a: real, n: real)
         requires n != 0.0
         ensures (a + n) / n == a / n + 1.0
       { assert n / n == 1.0;
         assert (a + n) / n == a / n + n / n; }   // distribute, don't multiply out

   A goal like `Sum(Shift(l)) / n == Sum(l) / n + 1.0` needs BOTH: (a) to relate
   the two sums, and (b) to divide through. Having proved (a) and still failing
   is the normal signal that (b) is what is left -- not that (a) was wrong.
3. CALL THE LEMMA where the fact is needed, with the arguments instantiated.
   A lemma that is never called does nothing.
4. IF THE LEMMA WILL NOT PROVE, split it. State the weakest step you are sure
   of, prove that, and build up. Keep every statement to one line if you can.
   A small fact that resists every attempt is evidence the fact is FALSE, and
   therefore evidence about the program -- report it by leaving the goal
   unproved rather than by assuming it.

Never reach for `assume`, and never state a lemma whose `ensures` is the entry
postcondition itself: that is the goal wearing a lemma's clothes, it proves
nothing, and it will be caught.
"""

_DAFNY_SPEC = """- The postcondition is given as a Python `assert`. Translate it into ONE `ensures`
  clause meaning exactly the same thing. Do NOT weaken it. No tautologies.
- Do NOT use `assume` anywhere."""

_DAFNY_PRE = """You are given the function's documentation. If it states a restriction on the
ARGUMENTS -- "given a positive number", "non-empty list", "the input will be
valid" -- express it as a `requires` clause on the entry method, tagged
`// synth: precondition from docstring: <quoted phrase>`.
- ONLY what the documentation actually states. Never add a `requires` merely
  because it would help the proof.
- The documentation describes the intended input domain, NOT the behaviour of this
  particular implementation, which may be buggy.
- If the documentation states no restriction on the arguments, emit NO `requires`.
- Never use `requires false` or any condition that cannot be satisfied.
- For Python library calls, declare a function/lemma capturing their semantics,
  tagged `// synth:`."""

_DAFNY_LIB = """Specs for Python library functions (sum, len, sorted, any, ...) MAY be axioms:
declare them bodyless with an `ensures`, or use `{:axiom}`, tagged `// synth:`.
These are recorded as the trusted base.
- Prefer a real definition when one is easy, e.g.
  `function PySum(s: seq<int>): int { if |s| == 0 then 0 else s[0] + PySum(s[1..]) }`
  with `decreases |s|` if needed. A definition cannot be wrong; an axiom can.
- Keep axioms MINIMAL and faithful. Never state an axiom strong enough to imply
  the entry method's postcondition - that would prove the program by assumption.
- Axiomatise only library functions, never the logic of the program itself."""

_C_SYNTAX = """\
 The file is checked by CBMC, a bounded model checker for C. Write self-contained
C99: NO `#include` lines and no library calls, because CBMC models the standard
library only approximately and a counterexample must be a fact about YOUR code.
Line comments are `//`. Structure the artifact as exactly four definitions, in
this order: `<entry>` (the transliterated program), `int <entry>_pre(...)` (the
input domain, `return 1;` if unrestricted), `int <entry>_post(..., <T> return_value)`
(the specification), and `int main(void)`, which chooses arbitrary inputs with
`nondet_int()` / `nondet_long()`, constrains them with
`__CPROVER_assume(<entry>_pre(...));`, and then asserts
`__CPROVER_assert(<entry>_post(..., <entry>(...)), "nfv-goal");`.
Obtain EVERY arbitrary input by calling `nfv_int()` (an arbitrary Python integer,
as `long`), `nfv_double()` (an arbitrary float), `nfv_char()` (an arbitrary
printable character, as `long`) or `nfv_len()` (an arbitrary list or string
length, at most `NFV_LEN_MAX`). These are already defined; do not define or
declare them, and do NOT call `nondet_int` / `nondet_long` / `nondet_double`
directly or build a value by type-punning bits through a union. They restrict
inputs to a size C can represent without overflow, which is what makes the check
meaningful at all.
Python ints become `long`; floats become `double`; a Python list becomes an array
of size `NFV_LEN_MAX` plus an explicit length parameter from `nfv_len()`, since C
arrays do not carry a length. A Python STRING is such an array too, whose
elements are character codes from `nfv_char()` -- never from `nfv_int()`, which
would admit values that are not characters. Predicates return `int` used as a
truth value."""

_C_PROOF = """\


TARGET LANGUAGE: C, checked by CBMC. There are no invariants, no annotations and
no proof to write: CBMC explores every execution up to a loop-unwinding bound and
either exhibits an input that violates the assertion or reports nothing found.

Your only job when the check comes back is to FIX THE TRANSLATION, never to make
the assertion easier to satisfy.

1. If CBMC reports a parse or conversion error, the C is malformed: repair it.
2. If CBMC reports an ARRAY BOUNDS, POINTER, OVERFLOW or CONVERSION failure,
   your translation has undefined behaviour and the result is discarded -- it
   says nothing about the Python program, because Python has no such behaviour.
   Fix the cause: take every input from `nfv_int()` / `nfv_double()` /
   `nfv_len()` so it is already small, size arrays to `NFV_LEN_MAX`, use `long`
   rather than `int` for Python integers, guard every index against the length
   parameter, and remember that `(long)x` on a `double` is undefined outside the
   integer range whereas Python's `int(x)` is total.
3. If CBMC reports an UNWINDING ASSERTION failure, a loop was not unrolled far
   enough. Bound the loop through the precondition where the documentation
   permits it; otherwise leave it, and the entry is reported as inconclusive.
4. If CBMC violates the goal assertion, the check is DONE. Do not weaken
   `<entry>_post`, do not strengthen `<entry>_pre`, and do not touch `<entry>`.

Never add an `__CPROVER_assume` that is not the stated precondition: it would
exclude the inputs on which the program is examined.
"""

_C_SPEC = """\
- The postcondition is given as a Python `assert`. Translate it into the body of
  `int <entry>_post`, meaning exactly the same thing. Do NOT weaken it. No
  tautologies: `return 1;` is rejected, and so is `return 0;`.
- The goal is the single `__CPROVER_assert` in `main`, tagged "nfv-goal", whose
  condition must be `<entry>_post(<args>, <entry>(<args>))`. Assert nothing else,
  and never assert a negated or constant condition."""

_C_PRE = """\
You are given the function's documentation. If it states a restriction on the
ARGUMENTS -- "given a positive number", "non-empty list", "the input will be
valid" -- express it as the body of `int <entry>_pre`, tagged
`// synth: precondition from docstring: <quoted phrase>`, and assume it in `main`
with `__CPROVER_assume(<entry>_pre(<args>));` BEFORE the assertion.
- ONLY what the documentation actually states. Never add a constraint merely
  because it would silence a failure.
- The documentation describes the intended input domain, NOT the behaviour of this
  particular implementation, which may be buggy.
- If the documentation states no restriction on the arguments, write
  `int <entry>_pre(...) { return 1; }`. Always define it.
- Never write `return 0;` or any condition that cannot be satisfied: it would
  leave the checker no execution to examine."""

_C_LIB = """\
Python library functions (sum, len, sorted, any, ...) must be WRITTEN OUT as
ordinary C functions, tagged `// synth:`. There is no axiom mechanism here and
none is needed: CBMC executes what you write, so a definition is both the
specification and the implementation.
- Keep them small and faithful; a loop is usually enough.
- An undefined function is NOT acceptable. CBMC treats a function it cannot see
  as returning an arbitrary value, so a counterexample found against one is a
  statement about an arbitrary function, not about the program."""

def _dafny_integrity(model: str, entry: str):
    return dafny_integrity.check(model, entry, lang="dafny")

BACKENDS: dict[str, Backend] = {
    "dafny": Backend(
        name="dafny", lang="dafny", comment="//", ext=".dfy",
        supports_bounded=True,
        run=dafny_backend.run_dafny,
        type_check=dafny_backend.type_check,
        integrity_check=_dafny_integrity,
        syntax_guide=_DAFNY_SYNTAX,
        proof_guide=_DAFNY_PROOF,
        spec_block=_DAFNY_SPEC,
        pre_block=_DAFNY_PRE,
        lib_block=_DAFNY_LIB,
    ),
    "dafny-theory": Backend(
        # dafny-plus, with the prompt-side Python reference REPLACED by a
        # phase-0 library theory: the same knowledge, but written as documented
        # declarations in the artifact rather than as prose in the prompt, and
        # written before the postcondition is visible. The delta against
        # dafny-plus is exactly (python_reference off, library_theory on), so a
        # difference is attributable to where the library knowledge lives.
        name="dafny-theory", lang="dafny", comment="//", ext=".dfy",
        supports_bounded=True,
        run=dafny_backend.run_dafny,
        type_check=dafny_backend.type_check,
        integrity_check=_dafny_integrity,
        syntax_guide=_DAFNY_SYNTAX,
        proof_guide=_DAFNY_WD_PROOF + _DAFNY_LEMMA_PROOF,
        spec_block=_DAFNY_SPEC,
        pre_block=_DAFNY_PRE,
        lib_block=_DAFNY_LIB,
        #
        # DEFAULTS CHANGED: the precondition is now authored in phase 0b (blind,
        # from source + docstring, before the goal is visible) and the
        # goal-visible abduction round is OFF. Use `--abduce` to restore the
        # old behaviour and `--no-phase0b` to drop the blind precondition; the
        # pair reproduces the pre-change arm exactly.
        relax_src_freeze=True,
        python_reference=False,
        abduce_definedness=False,
        phase0_precondition=True,
        repair_context=True,
        library_theory=True,
    ),
    "cbmc": Backend(
        name="cbmc", lang="c", comment="//", ext=".c",
        supports_bounded=True,
        two_sided=True,
        run=cbmc_backend.run_cbmc,
        type_check=cbmc_backend.type_check,
        integrity_check=c_integrity.check,
        syntax_guide=_C_SYNTAX,
        proof_guide=_C_PROOF,
        spec_block=_C_SPEC,
        pre_block=_C_PRE,
        lib_block=_C_LIB,
        phase0_precondition=True,
        repair_context=True,
    ),
    "cbmc-theory": Backend(
        name="cbmc-theory", lang="c", comment="//", ext=".c",
        supports_bounded=True,
        two_sided=True,
        run=cbmc_backend.run_cbmc,
        type_check=cbmc_backend.type_check,
        integrity_check=c_integrity.check,
        syntax_guide=_C_SYNTAX,
        proof_guide=_C_PROOF,
        spec_block=_C_SPEC,
        pre_block=_C_PRE,
        lib_block=_C_LIB,
        phase0_precondition=True,
        repair_context=True,
        library_theory=True,
    ),
}
for _name, _bk in BACKENDS.items():
    if not getattr(_bk, "two_sided", False):
        continue
    if not _bk.supports_bounded:
        raise AssertionError(f"{_name}: a two-sided backend must support bounded mode")
    try:
        _bk.run("", mode="unbounded", timeout=1.0)
    except ValueError:
        pass  # correct: the backend refuses
    except Exception as _exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"{_name}: `run(mode='unbounded')` must raise ValueError, not "
            f"{type(_exc).__name__}") from _exc
    else:
        raise AssertionError(
            f"{_name} accepted mode='unbounded'. A bounded model checker cannot "
            f"establish a proof, and a run that found no counterexample within "
            f"the unwinding must never be reported as one.")

BACKENDS["dafny-theory-lemma4"] = replace(
    BACKENDS["dafny-theory"],
    name="dafny-theory-lemma4",
    proof_guide=_DAFNY_WD_PROOF + _DAFNY_LEMMA_PROOF + _DAFNY_LEMMA4_PROOF,
)

BACKENDS["dafny-naive"] = replace(BACKENDS["dafny"], name="dafny-naive", naive=True)

def get(name: str) -> Backend:
    try:
        return BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"unknown backend {name!r}; expected one of {sorted(BACKENDS)}") from None
