#!/usr/bin/env python3
"""
Stage 2b — Proof repair (unbounded).

Runs only when the bounded phase found NO counterexample. The agent reads the
verifier's unproven-assertion diagnostics and proposes annotations: loop
invariants, decreases clauses, assert waypoints, lemmas with bodies, and
contracts on internal helper methods.

Two invariants make this sound:

1. The program is FROZEN. `check_freeze` re-runs every round and rejects any diff
   that touches a `// src:` line, so the agent cannot edit its way out of a real
   bug -- only annotate around a real proof.
2. Nothing enters the trusted base. `integrity.check` re-runs every round and
   rejects `assume`, {:verify false}, {:extern}, an axiomatised entry method,
   and weakened/tautological entry `ensures`. The entry `requires` is pinned to
   its Stage-1 (docstring-justified) value and may never be strengthened.

Exhausting the rounds yields ABSTAIN, never Incorrect: failing to find a proof is
not evidence of a bug.

PRECONDITION RESCUE (opt-in, `--precondition-rescue`)
-----------------------------------------------------
One extra round, run only after the normal rounds have failed, in which the ban
on strengthening the entry precondition is lifted and the agent is asked for the
*natural* domain restriction the documentation implies. This is deliberately the
mechanism F10 identified as unsound, so it is instrumented rather than trusted:
the added clauses are recorded, a proof obtained this way is flagged
(`rescue_used`) and must never be pooled with a clean proof, and every candidate
precondition is put through a static vacuity probe before it is accepted.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from nfv.agent import integrity, library_theory
from nfv.agent.translate import extract_code
from nfv.backends import dafny
from nfv.backends.registry import Backend, get as get_backend
from nfv.shared.copilot_client import CopilotChat

SYSTEM = """\
You are a proof engineer. The {target} program below is FROZEN. The verifier could
not prove some assertions. Propose annotations that let it complete the proof.{proof_guide}

YOU MAY ONLY ADD OR EDIT `{c} synth:` LINES:
- loop invariants and decreases clauses
- assert waypoints and lemmas (lemmas MUST have bodies)
- requires/ensures on INTERNAL helper methods
- specs for library functions

YOU MUST NOT:
- touch any `{c} src:` line - the program's logic is frozen
- change or weaken the entry method's `ensures`
- add or strengthen `requires` on the entry method (its precondition is fixed)
- use `assume`, {:verify false}, or {:extern}
- change what the program computes

AXIOMS: you may declare bodyless specs / {:axiom} for LIBRARY functions only.
They are recorded as the trusted base and inspected. Never axiomatise the
program's own logic, and never state an axiom strong enough to imply the entry
postcondition - that proves nothing and will be caught.

The program may be genuinely incorrect. If so, NO annotation will prove it, and
that is an acceptable outcome. Do not try to force verification to succeed. You
are not judging correctness.

OUTPUT: only the full revised ```{fence} code block.
"""

USER_TEMPLATE = """\
Round {round_i} of {max_rounds}. The verifier reports these unproven assertions:

{diagnostics}

Current program:
```{fence}
{model}
```
{context}"""

# The naive baseline's repair prompt: one sentence. No permission list, because
# nothing checks one -- the model may edit the program, weaken the `ensures` or
# assume the goal, and the only consequence is what the verifier then says.
NAIVE_SYSTEM = """\
You are a proof engineer. The {target} program below did not verify. Fix it so
that it verifies.

OUTPUT: only the full revised ```{fence} code block. No prose.
"""


RESCUE_SYSTEM = """\
You are a proof engineer. The {target} program below is FROZEN and could not be
proved. There are two legitimate reasons a faithful translation can fail to
verify, and this round exists to let you supply either or both.

(1) NATURAL PRECONDITION. The program may only be intended to work on a
restricted domain of inputs, and the specification does not say so. State that
domain as `requires` clauses on the entry method, each tagged
`{c} synth: precondition from docstring: <quoted phrase>`.

- The precondition must come from the DOCUMENTATION, describing the intended
  INPUT. It must be a restriction the caller is expected to respect.
- Never choose a precondition merely because it makes the proof succeed. A
  precondition that rules out the inputs on which the code misbehaves is a bug
  being hidden, not a specification.
- The documentation describes what the function RETURNS as well as what it
  ACCEPTS. Only the latter may become a `requires`.
- It must be satisfiable, and must admit the function's normal inputs. It will
  be checked mechanically for vacuity and rejected if unsatisfiable.
- If the documentation states no restriction on the arguments, add NO `requires`.

(2) ASSUMPTIONS ABOUT EXTERNAL FUNCTIONS. The proof may be blocked because a
Python library or builtin you modelled as an uninterpreted function has no
stated properties. You may state the properties that function genuinely has,
each tagged `{c} synth: assumption about external function <name>: <why it is
true of the Python builtin>`.

- The assumption may ONLY describe a Python library function or builtin --
  something whose behaviour is fixed by Python, not by this program. Examples of
  the kind of fact that is legitimate: `abs` is non-negative, `len` is
  non-negative, sorting preserves multiset membership, `ord` is injective.
- NEVER state an assumption about the logic of the program under analysis, and
  never one strong enough to imply the entry method's postcondition. That proves
  the program by assumption rather than by proof.
- Prefer a DEFINITION with a body over an assumption whenever the function is
  computable: a definition cannot be wrong, an assumption can.
- State the weakest fact that unblocks the proof. Each assumption you add
  enlarges the trusted base and will be reported as such.

Return the program unchanged if neither applies; that is an acceptable answer.

You may also add the usual `{c} synth:` annotations (invariants, decreases,
asserts, lemmas with bodies) alongside the above.

YOU MUST NOT touch any `{c} src:` line, change or weaken the entry `ensures`, or
use `assume`, {:verify false} or {:extern}.

OUTPUT: only the full revised ```{fence} code block.
"""

RESCUE_TEMPLATE = """\
The normal proof rounds are exhausted and the program is still unproved.

Documentation for the function (this is the ONLY justification you may use for a
precondition; it describes the intended input domain):
\"\"\"{docstring}\"\"\"

The verifier reports these unproven assertions:

{diagnostics}

Current program:
```{fence}
{model}
```
"""


_PROBE = "NfvVacuityProbe"

ABDUCE_SYSTEM = """\
You are a proof engineer. The {target} program below is FROZEN and unproved. The
obstacle may not be the program's logic: it may be that something the proof needs
is not currently STATED anywhere. This round exists to let you state it.

There are two legitimate kinds of missing fact, and you may supply either or both.

(1) A PRECONDITION, when an internal well-definedness obligation cannot be
discharged -- an index may be out of range, a divisor may be zero, a function
may be applied outside its domain. In Python those operations are PARTIAL: the
same input would raise IndexError or ZeroDivisionError, so the intended input
domain may genuinely exclude it. Add the WEAKEST `requires` on the entry method
that makes the flagged operations well-defined, tagged
`{c} synth: precondition for well-definedness: <site>`.

(2) A STRONGER LIBRARY SPECIFICATION, when the proof stalls because a library
function you declared is under-specified -- you gave `PySum` no relation to its
argument, or `PyMax` no link to membership, so the verifier knows nothing about
it. Strengthen that spec, tagged `{c} synth: strengthened library spec: <fn>`.
- Prefer a DEFINITION (a `function` with a body, or a recursive characterisation)
  over an axiom. A definition cannot be wrong; an axiom can.
- The spec must be TRUE OF PYTHON's actual behaviour. Use the Python reference
  above where one is supplied, in preference to your recollection.
- Never state a library fact strong enough to imply the entry postcondition.
  That proves the program by assumption and will be caught and rejected.

RULES for both:
- Say the weakest thing that does the job. Never add a condition merely because
  it would also make the postcondition go through.
- Never `requires false`, and nothing that admits no input.
- Do NOT change, weaken, or strengthen the entry `ensures`. Do NOT edit the
  program's logic.
- If the real obstacle is that the program is WRONG, add nothing. Returning it
  unchanged is a correct outcome.

Anything you add here is recorded as an assumption and audited separately from
the proof, so state it honestly rather than conveniently.

OUTPUT: only the full revised ```{fence} code block.
"""

# Appended to ABDUCE_SYSTEM only for arms that ran phase 0, so every other arm's
# prompt is unchanged. This round is the ONE place a library assumption may be
# added with the goal in view, so it is also the one place that has to say what
# discipline such an assumption is held to.
AMEND_RULES = """\

THE LIBRARY THEORY AT THE TOP OF THE PROGRAM IS FROZEN. It was written before
this goal existed. Do not edit, rename, weaken or strengthen anything in it, and
do not delete it: reproduce it exactly as it stands.

You MAY add a new declaration BELOW it, as an AMENDMENT. Every amendment must
carry both of these comments immediately above it:

  {c} doc: <what Python actually does, in English>
  {c} axiom-justification: <which documented Python behaviour this encodes, and
      why it cannot be derived from a definition>

An amendment that names the entry method is rejected outright: an assumption
about the program under test is the question begged. Prefer a `function` with a
body, or a lemma you actually prove, over an axiom -- neither costs you anything
in trust, and both are checked.

Amendments are recorded SEPARATELY from the frozen theory, precisely because
they were written with the goal in view. State them honestly rather than
conveniently.
"""

ABDUCE_TEMPLATE = """\
The program is unproved. The verifier reports:

{diagnostics}

Current program:
```{fence}
{model}
```
{context}"""

# Verifier messages that denote an internal well-definedness obligation rather
# than a failure of the stated postcondition. Used to (a) decide whether the
# abduction round may run at all and (b) check afterwards that it discharged
# what it claimed to.
_DEFINEDNESS_ERR = re.compile(
    r"index out of range"
    r"|division by zero"
    r"|precondition for this call could not be proved"
    r"|function precondition could not be proved"
    r"|out of bounds"
    r"|upper bound below lower bound or above length"
    r"|lower bound must not exceed upper bound"
    r"|value does not satisfy the subset constraints",
    re.I,
)


def definedness_errors(result) -> list[str]:
    """The well-definedness errors in a verifier result, if any."""
    text = list(getattr(result, "errors", None) or [])
    if not text:
        text = ((getattr(result, "stdout", "") or "")).splitlines()
    return [e for e in text if _DEFINEDNESS_ERR.search(e)]


from .translate import target_name as _target_name


def render(prompt: str, backend) -> str:
    """Specialise a system prompt to a target language.

    Uses `str.replace` rather than `str.format` because these prompts contain
    literal braces (`{:verify false}`) that `format` would misread.
    """
    for key, val in (("{proof_guide}", backend.proof_guide),
                     ("{target}", _target_name(backend)),
                     ("{fence}", backend.lang),
                     ("{c}", backend.comment)):
        prompt = prompt.replace(key, val)
    return prompt


def _entry_params(model: str, entry: str, lang: str = "dafny") -> str | None:
    """Parameter list of the entry method, for building the vacuity probe."""
    kw = "method"
    m = re.search(rf"{kw}\s+{re.escape(entry)}\s*\(([^)]*)\)", model)
    return m.group(1) if m else None


def is_vacuous(model: str, entry: str, preconditions: list[str],
               timeout: float = 60.0, backend=None) -> bool | None:
    """True if `preconditions` are unsatisfiable, i.e. prove anything vacuously.

    Purely static: builds a probe procedure carrying the same parameters and
    preconditions whose body is `assert false`, and asks the verifier to prove
    just that one symbol. A *successful* proof of `false` means the domain is
    empty, so any postcondition would hold for the wrong reason.

    Returns None when the probe cannot be built or the verifier is inconclusive,
    so the caller can distinguish "unknown" from "safe".
    """
    bk = get_backend(backend) if isinstance(backend, str) else backend
    # Dispatch on the LANGUAGE, not the arm name: see `pins_input_size`.
    lang = bk.lang if bk is not None else "dafny"
    params = _entry_params(model, entry, lang)
    if params is None or not preconditions:
        return None
    body = "\n".join(f"  requires {p}" for p in preconditions)
    probe = f"\n\nmethod {_PROBE}({params})\n{body}\n{{ assert false; }}\n"
    run = bk.run if bk is not None else dafny.run_dafny
    res = run(model + probe, mode="unbounded", timeout=timeout, filter_symbol=_PROBE)
    if res.outcome is dafny.Outcome.VERIFIED:
        return True     # `false` was proved => the precondition admits no input
    if res.outcome is dafny.Outcome.VERIFICATION_FAILED:
        return False    # could not prove false => satisfiable as far as we can tell
    return None


_PROBE_B = "NfvSizePinProbe"
_SIZED = re.compile(r"\b(seq|string|set|iset|multiset|map|imap)\b")


def _split_params(params: str) -> list[tuple[str, str]]:
    """`"a: seq<int>, b: int"` -> `[("a","seq<int>"), ("b","int")]`.

    Splits on top-level commas only, so generic arguments (`map<int,int>`) do
    not break the parse, and handles Dafny's shared-type form (`a, b: int`).
    """
    parts, depth, cur = [], 0, ""
    for ch in params:
        if ch in "<[(":
            depth += 1
        elif ch in ">])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)

    out: list[tuple[str, str]] = []
    pending: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if ":" in p:
            nm, ty = p.split(":", 1)
            for q in pending + [nm]:
                out.append((q.strip(), ty.strip()))
            pending = []
        else:
            pending.append(p)
    return out


def pins_input_size(model: str, entry: str, preconditions: list[str],
                    timeout: float = 60.0, backend=None) -> bool | None:
    """True if `preconditions` force every admitted input to the same size.

    `is_vacuous` is not a strong enough test for a bad precondition. A domain
    can be non-empty and still be degenerate: `|numbers| == 0` admits exactly
    one input, so the empty sequence, and it makes the postcondition trivial
    while passing a satisfiability check. That is how the HumanEval/9 mutant was
    proved (F23).

    The test is semantic, not syntactic, so it does not have to enumerate the
    syntactic forms that pin a size. Two independent copies of the parameters
    are admitted under the same preconditions, and the verifier is asked to
    prove that a collection parameter has the same cardinality in both:

        method probe(x: seq<int>, x_b: seq<int>)
          requires P(x) requires P(x_b)
        { assert |x| == |x_b|; }

    A successful proof means the precondition determines the size, i.e. it
    admits only one shape of input. A precondition that merely *relates* sizes
    (`|guess| == |game|`) is not provable this way and is correctly kept.

    Returns None when the probe cannot be built or the verifier is
    inconclusive, so the caller can distinguish "unknown" from "safe".
    """
    bk = get_backend(backend) if isinstance(backend, str) else backend
    # Dispatch on the LANGUAGE, not the arm name. `name in ("z3", "boogie")`
    # silently mis-dispatched every derived arm -- `boogie-plus` matched neither
    # branch, so `_entry_params` searched for `method` in a Boogie file, found
    # nothing, and this gate returned "unknown" for the whole arm.
    lang = bk.lang if bk is not None else "dafny"
    params = _entry_params(model, entry, lang)
    if params is None or not preconditions:
        return None
    parsed = _split_params(params)
    sized = [nm for nm, ty in parsed if _SIZED.search(ty)]
    if not parsed or not sized:
        return False    # nothing whose size could be pinned

    suffix = "_nfvb"
    rename = {nm: nm + suffix for nm, _ in parsed}

    def _ren(expr: str) -> str:
        out = expr
        for nm, new in rename.items():
            out = re.sub(rf"\b{re.escape(nm)}\b", new, out)
        return out

    decls = ", ".join(f"{nm}: {ty}" for nm, ty in parsed)
    decls_b = ", ".join(f"{rename[nm]}: {ty}" for nm, ty in parsed)
    reqs = "\n".join(f"  requires {p}" for p in preconditions)
    reqs_b = "\n".join(f"  requires {_ren(p)}" for p in preconditions)
    # Disjunction, so ONE verifier call answers "is any parameter pinned".
    goal = " || ".join(f"|{nm}| == |{rename[nm]}|" for nm in sized)
    probe = (f"\n\nmethod {_PROBE_B}({decls}, {decls_b})\n{reqs}\n{reqs_b}\n"
             f"{{ assert {goal}; }}\n")

    run = bk.run if bk is not None else dafny.run_dafny
    try:
        res = run(model + probe, mode="unbounded", timeout=timeout,
                  filter_symbol=_PROBE_B)
    except Exception:
        return None
    if res.outcome is dafny.Outcome.VERIFIED:
        return True     # size is determined => the domain is degenerate
    if res.outcome is dafny.Outcome.VERIFICATION_FAILED:
        return False
    return None


@dataclass
class ProofResult:
    proved: bool
    model: str
    rounds: int
    reason: str = ""
    verify_seconds: float = 0.0
    rejected_rounds: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    # A proof reached only after the precondition ban was lifted. Kept separate
    # from `proved` at every reporting layer: it rests on a domain the agent
    # chose while looking at a failing proof.
    rescue_used: bool = False
    rescue_preconditions: list[str] = field(default_factory=list)
    # Assumptions about Python builtins added in the rescue round. Tracked apart
    # from the preconditions because they fail differently: a bad precondition
    # shrinks the domain (F10), a bad assumption asserts something false about
    # the world (F7). Both enlarge the trusted base and both are reported.
    rescue_assumptions: list[str] = field(default_factory=list)
    # Whitelisted proof-side annotations attached to frozen `src:` lines, when
    # the backend permits them. Recorded verbatim (before/after) so a run can be
    # audited for how often, and where, the relaxation was exercised.
    src_annotations: list[dict] = field(default_factory=list)
    # Preconditions added by the definedness-abduction round. Reported apart
    # from `proved` for the same reason as `rescue_preconditions`: the claim is
    # about a restricted domain, so it is a different claim from a proof of the
    # documented function. Unlike rescue, admission is gated on the abduction
    # having discharged a specific well-definedness obligation.
    abduced_definedness: list[str] = field(default_factory=list)
    # Library specs strengthened by the abduction round. These ENLARGE THE
    # TRUSTED BASE and are deliberately not gated on discharging a specific
    # obligation, so a proof resting on them is a weaker claim. Reported apart
    # from `proved` at every layer.
    abduced_library: list[str] = field(default_factory=list)
    # Trusted declarations added in 2c, with the goal in view, as opposed to the
    # phase-0 theory written blind. Kept apart so the two can be compared.
    theory_amendments: list[str] = field(default_factory=list)


async def prove(
    chat: CopilotChat,
    frozen_model: str,
    entry_point: str,
    *,
    max_rounds: int = 5,
    timeout: float = 600.0,
    verify_budget_s: float = 60.0,
    precondition_rescue: bool = False,
    docstring: str = "",
    python_source: str = "",
    reference_block: str = "",
    theory: str = "",
    backend: Backend | str = "dafny",
) -> ProofResult:
    """Iteratively annotate `frozen_model` until Dafny proves it, or the budget runs out.

    The budget is measured in *verifier* seconds, not rounds. Proof effort is what
    we are actually rationing, and it is the part that varies wildly between
    entries; capping rounds alone would give a hard entry the same effort as a
    trivial one. `max_rounds` is a safety stop against a model that loops without
    making progress.

    The cap is 5 because the measured return beyond that is negligible and the
    risk is not: on gpt-5.5, 80% of proofs land within one round and only 4 of 73
    needed more than five, while proofs that took many rounds were markedly more
    likely to be unsound (unsound mean 2.40 rounds vs 1.06 for sound, and no
    unsound proof ever verified at round 0). Extra rounds buy little coverage and
    mostly extend the agent's opportunity to weaken the obligation.

    `precondition_rescue` adds ONE further round after those are exhausted, in
    which the entry precondition may be strengthened if the agent can tie it to
    the docstring. A proof from that round is returned with `rescue_used=True`
    and must be reported separately.
    """
    bk = get_backend(backend) if isinstance(backend, str) else backend
    naive = getattr(bk, "naive", False)
    system = render(NAIVE_SYSTEM if naive else SYSTEM, bk)
    current = frozen_model
    transcript: list[dict] = []
    rejected: list[str] = []
    src_annotations: list[dict] = []
    abduced_definedness: list[str] = []
    abduced_library: list[str] = []
    theory_amendments: list[str] = []
    spent = 0.0
    base_precondition = bk.integrity_check(frozen_model, entry_point).assumed_precondition
    base_trusted = set(bk.integrity_check(frozen_model, entry_point).trusted_base or [])

    # Context carried into every repair round. Empty unless the backend asks for
    # it, which keeps the rendered prompt of the existing arms byte-identical.
    context = ""
    if bk.repair_context:
        parts = []
        if python_source.strip():
            parts.append("The Python source this model was translated from "
                         "(for reference; it is NOT to be changed and may itself "
                         "be buggy):\n```python\n" + python_source.strip() + "\n```")
        if reference_block.strip():
            parts.append(reference_block.strip())
        context = ("\n" + "\n\n".join(parts) + "\n") if parts else ""

    def _remaining() -> float:
        return verify_budget_s - spent

    # Off the event loop: the verifier is a blocking subprocess, and running it
    # inline stalls every other entry sharing this loop until it returns. That
    # is invisible for a fast backend but serialises the whole run for a slow
    # one. Same call, same timeout, so no verdict can change -- only overlap.
    res = await asyncio.to_thread(
        bk.run, current, mode="unbounded", timeout=max(5.0, _remaining()))
    spent += res.duration_s
    if res.outcome is dafny.Outcome.VERIFIED:
        return ProofResult(True, current, 0, "verified without annotation",
                           verify_seconds=spent, transcript=transcript)

    round_i = 0
    while round_i < max_rounds and _remaining() > 0:
        round_i += 1
        diagnostics = ((res.stdout or "") + (res.stderr or "")).strip() or "verification failed"
        user = USER_TEMPLATE.format(round_i=round_i, max_rounds=max_rounds,
                                    diagnostics=diagnostics[:4000], model=current,
                                    fence=bk.lang, context=context)
        resp = await chat.ask(system, user, timeout=timeout)
        transcript.append({"round": round_i, "response": resp.text,
                           "elapsed_s": resp.elapsed_s})

        candidate = extract_code(resp.text)
        if candidate is None:
            rejected.append(f"round {round_i}: no {bk.lang} block")
            continue

        # The freeze is a permission boundary, checked mechanically every round.
        # The naive baseline has no permission boundary: it may rewrite the
        # program, weaken the goal or add a precondition, and nothing stops it.
        # Violations are still COMPUTED and recorded, because what the baseline
        # does when unpoliced is the measurement.
        frz = integrity.check_freeze(frozen_model, candidate, bk.comment,
                                     allow_proof_annotations=bk.relax_src_freeze)
        if not frz.ok:
            rejected.append(f"round {round_i}: freeze violation: {'; '.join(frz.violations)}")
            if not naive:
                continue
        # The phase-0 theory is `synth:`-tagged, so the src-line freeze does not
        # cover it. It gets its own freeze: its whole point is that it was
        # written before the goal was in view, which is worthless if repair can
        # edit it once the goal IS in view.
        if theory.strip() and not library_theory.contains(candidate, theory, bk.comment, bk.lang):
            rejected.append(f"round {round_i}: library theory modified")
            continue
        for ln, btxt, atxt in frz.annotated_lines:
            rec = {"round": round_i, "source_line": ln, "before": btxt, "after": atxt}
            if rec not in src_annotations:
                src_annotations.append(rec)

        integ = bk.integrity_check(candidate, entry_point)
        if not integ.ok:
            rejected.append(f"round {round_i}: integrity: {'; '.join(integ.violations)}")
            if not naive:
                continue

        # A precondition is only legitimate when justified by the docstring, which
        # Stage 2 never sees. Stage 2 sees failed proofs instead -- exactly the
        # pressure that produces a domain-shrinking `requires`. So the entry
        # precondition is frozen at its Stage-1 value: it may be dropped, never
        # strengthened.
        added = [p for p in integ.assumed_precondition if p not in base_precondition]
        if added:
            rejected.append(
                f"round {round_i}: strengthened entry precondition: {'; '.join(added)}")
            if not naive:
                continue

        res = await asyncio.to_thread(
            bk.run, candidate, mode="unbounded", timeout=max(5.0, _remaining()))
        spent += res.duration_s
        # Only adopt a candidate that passed both audits.
        current = candidate
        if res.outcome is dafny.Outcome.VERIFIED:
            return ProofResult(True, current, round_i, "verified",
                               verify_seconds=spent, rejected_rounds=rejected,
                               transcript=transcript, src_annotations=src_annotations,
                       abduced_definedness=abduced_definedness,
                       abduced_library=abduced_library,
                                   theory_amendments=theory_amendments)

    exhausted = "verify budget" if _remaining() <= 0 else "round cap"

    # ---- definedness abduction -------------------------------------------
    # Runs only when the program is unproved BECAUSE an internal obligation is
    # undischarged. It is kept apart from `precondition_rescue`: that round may
    # narrow the domain for any reason, this one may do so only to make partial
    # operations total, and is gated on actually having done that.
    if bk.abduce_definedness and _remaining() > 0:
        pre_errs = definedness_errors(res)
        # Runs on ANY unproved program, not only a definedness failure: an
        # under-specified library is just as likely to be the obstacle, and it
        # shows up as a bare postcondition failure.
        if True:
            round_i += 1
            diag = "\n".join(pre_errs[:12]) if pre_errs else (
                ((res.stdout or "") + (res.stderr or "")).strip()[:4000] or "verification failed")
            user = ABDUCE_TEMPLATE.format(diagnostics=diag, fence=bk.lang,
                                          model=current, context=context)
            abduce_system = ABDUCE_SYSTEM + (AMEND_RULES if theory.strip() else "")
            resp = await chat.ask(render(abduce_system, bk), user, timeout=timeout)
            transcript.append({"round": round_i, "abduce": True, "response": resp.text,
                               "elapsed_s": resp.elapsed_s})
            candidate = extract_code(resp.text)
            if candidate is None:
                rejected.append(f"abduce round {round_i}: no {bk.lang} block")
            else:
                frz = integrity.check_freeze(frozen_model, candidate, bk.comment,
                                             allow_proof_annotations=bk.relax_src_freeze)
                integ = bk.integrity_check(candidate, entry_point)
                added = [p for p in integ.assumed_precondition
                         if p not in base_precondition] if integ.ok else []
                new_lib = [a for a in (integ.trusted_base or [])
                           if a not in base_trusted] if integ.ok else []
                if not frz.ok:
                    rejected.append(
                        f"abduce round {round_i}: freeze violation: {'; '.join(frz.violations)}")
                elif theory.strip() and not library_theory.contains(candidate, theory, bk.comment, bk.lang):
                    rejected.append(f"abduce round {round_i}: library theory modified")
                elif not integ.ok:
                    rejected.append(
                        f"abduce round {round_i}: integrity: {'; '.join(integ.violations)}")
                elif theory.strip() and library_theory.amendment_issues(
                        candidate, theory, bk.comment, entry_point, bk.lang, current)[1]:
                    _, why = library_theory.amendment_issues(
                        candidate, theory, bk.comment, entry_point, bk.lang, current)
                    rejected.append(f"abduce round {round_i}: {'; '.join(why[:3])}")
                else:
                    vac = is_vacuous(candidate, entry_point, integ.assumed_precondition,
                                     timeout=max(5.0, _remaining()), backend=bk)
                    if vac is True:
                        rejected.append(
                            f"abduce round {round_i}: vacuous precondition: {'; '.join(added)}")
                    elif vac is None and added:
                        rejected.append(
                            f"abduce round {round_i}: vacuity undecided: {'; '.join(added)}")
                    else:
                        # Satisfiability is not enough. `|numbers| == 0` admits
                        # exactly one input and trivializes the postcondition
                        # while passing the vacuity probe -- that is how the
                        # HumanEval/9 mutant was proved (F23). Reject a
                        # precondition that DETERMINES the input size, unless
                        # Stage 1 had already pinned it, in which case the
                        # abduction is not what made the domain degenerate.
                        degenerate = False
                        if added:
                            pin = pins_input_size(
                                candidate, entry_point, integ.assumed_precondition,
                                timeout=max(5.0, _remaining()), backend=bk)
                            if pin is True:
                                base_pin = pins_input_size(
                                    candidate, entry_point, base_precondition,
                                    timeout=max(5.0, _remaining()), backend=bk
                                ) if base_precondition else False
                                degenerate = base_pin is not True
                        if degenerate:
                            rejected.append(
                                f"abduce round {round_i}: degenerate precondition "
                                f"(determines the input size): {'; '.join(added)}")
                        else:
                            res2 = await asyncio.to_thread(
                                bk.run, candidate, mode="unbounded",
                                timeout=max(5.0, _remaining()))
                            spent += res2.duration_s
                            # Gate on PRECONDITIONS only. A precondition claimed
                            # for well-definedness must actually have discharged
                            # a well-definedness obligation; if those errors
                            # survive, it bought something else and is refused.
                            # Library specs are not gated this way -- they are
                            # allowed to help the postcondition, which is exactly
                            # why they are recorded as trusted base and reported
                            # separately.
                            still = definedness_errors(res2)
                            if (added and pre_errs and still
                                    and res2.outcome is not dafny.Outcome.VERIFIED):
                                rejected.append(
                                    f"abduce round {round_i}: definedness obligations remain: "
                                    f"{'; '.join(s.split('): ')[-1] for s in still[:3])}")
                            else:
                                current, res = candidate, res2
                                abduced_definedness = added
                                abduced_library = new_lib
                                if theory.strip():
                                    theory_amendments = library_theory.amendment_issues(
                                        candidate, theory, bk.comment, entry_point, bk.lang, current)[0]
                                for ln, btxt, atxt in frz.annotated_lines:
                                    rec = {"round": round_i, "source_line": ln,
                                           "before": btxt, "after": atxt}
                                    if rec not in src_annotations:
                                        src_annotations.append(rec)
                                if res2.outcome is dafny.Outcome.VERIFIED:
                                    why = []
                                    if added:
                                        why.append("precondition")
                                    if new_lib:
                                        why.append("library spec")
                                    return ProofResult(
                                        True, current, round_i,
                                        f"verified after abduction ({', '.join(why)})" if why
                                        else "verified after abduction without new assumption",
                                        verify_seconds=spent, rejected_rounds=rejected,
                                        transcript=transcript,
                                        src_annotations=src_annotations,
                                        abduced_definedness=abduced_definedness,
                                        abduced_library=abduced_library,
                                   theory_amendments=theory_amendments)

    if precondition_rescue and _remaining() > 0:
        round_i += 1
        diagnostics = ((res.stdout or "") + (res.stderr or "")).strip() or "verification failed"
        user = RESCUE_TEMPLATE.format(docstring=docstring.strip(),
                                      diagnostics=diagnostics[:4000], model=current,
                                      fence=bk.lang)
        resp = await chat.ask(render(RESCUE_SYSTEM, bk), user, timeout=timeout)
        transcript.append({"round": round_i, "rescue": True, "response": resp.text,
                           "elapsed_s": resp.elapsed_s})
        candidate = extract_code(resp.text)
        ok, added = False, []
        if candidate is None:
            rejected.append(f"rescue round {round_i}: no {bk.lang} block")
        else:
            frz = integrity.check_freeze(frozen_model, candidate, bk.comment,
                                         allow_proof_annotations=bk.relax_src_freeze)
            integ = bk.integrity_check(candidate, entry_point)
            if not frz.ok:
                rejected.append(
                    f"rescue round {round_i}: freeze violation: {'; '.join(frz.violations)}")
            elif theory.strip() and not library_theory.contains(candidate, theory, bk.comment, bk.lang):
                rejected.append(f"rescue round {round_i}: library theory modified")
            elif not integ.ok:
                rejected.append(
                    f"rescue round {round_i}: integrity: {'; '.join(integ.violations)}")
            else:
                # This is the one round that may strengthen the domain, so it is
                # the one round where an empty domain must be ruled out: an
                # unsatisfiable precondition proves every postcondition.
                added = [p for p in integ.assumed_precondition if p not in base_precondition]
                new_assumptions = [
                    ln.strip() for ln in candidate.splitlines()
                    if "synth: assumption about external function" in ln
                ]
                vac = is_vacuous(candidate, entry_point, integ.assumed_precondition,
                                 timeout=max(5.0, _remaining()), backend=bk)
                if vac is True:
                    rejected.append(
                        f"rescue round {round_i}: vacuous precondition "
                        f"(admits no input): {'; '.join(integ.assumed_precondition)}")
                elif vac is None and added:
                    rejected.append(
                        f"rescue round {round_i}: vacuity of precondition undecided: "
                        f"{'; '.join(added)}")
                else:
                    ok = True
        if ok:
            res = await asyncio.to_thread(
                bk.run, candidate, mode="unbounded", timeout=max(5.0, _remaining()))
            spent += res.duration_s
            current = candidate
            if res.outcome is dafny.Outcome.VERIFIED:
                # A proof only because the domain shrank, or because a new fact
                # about the world was assumed, is a different claim from a proof
                # of the documented function. `added` and `new_assumptions` are
                # what separate them.
                why = []
                if added:
                    why.append("precondition")
                if new_assumptions:
                    why.append("external assumption")
                return ProofResult(True, current, round_i,
                                   f"verified after rescue ({', '.join(why)})" if why
                                   else "verified in rescue round without new assumption",
                                   verify_seconds=spent, rejected_rounds=rejected,
                                   transcript=transcript,
                                   rescue_used=bool(added or new_assumptions),
                                   rescue_preconditions=added,
                                   rescue_assumptions=new_assumptions,
                                   src_annotations=src_annotations,
                                   abduced_definedness=abduced_definedness,
                                   abduced_library=abduced_library,
                                   theory_amendments=theory_amendments)

    return ProofResult(False, current, round_i,
                       f"no proof found ({exhausted} exhausted) (abstain)",
                       verify_seconds=spent, rejected_rounds=rejected,
                       transcript=transcript, src_annotations=src_annotations)
