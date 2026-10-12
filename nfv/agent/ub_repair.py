#!/usr/bin/env python3
"""
Repair loop for a bounded model checker: fix the TRANSLATION, never the goal.

The proof-oriented backends repair a proof. There is no proof here, so this loop
exists for a different reason, and it is worth being precise about what that
reason is.

C's arithmetic is partial where Python's is total. `int(x)` always works;
`(long)x` is undefined once x leaves the integer range. Python integers are
unbounded; a C `long` overflows, and signed overflow is undefined. A Python list
carries its length; a C array does not, so an index the Python could never reach
becomes a buffer overrun. A transliteration that ignores any of this produces a
C program whose executions include ones the Python program does not have, and
CBMC will happily find a "counterexample" among them.

Those executions are caught -- the built-in checks stay enabled and a trace that
passes through one makes the result malformed rather than refuting -- but merely
catching them costs the entry. The model is told what undefined behaviour the
checker found and given the chance to write C that does not have it. That is a
repair of the translation, in exactly the sense the other backends repair a
proof, and it is subject to the same permission boundary:

  * the frozen `src:` lines may not change (checked every round),
  * the phase-0 library theory may not change,
  * the artifact must still pass the C integrity audit, so the goal assertion
    still asserts the postcondition about the program, and
  * the entry precondition may be dropped but never strengthened -- otherwise
    the loop would have a trivial winning move, namely to assume away the inputs
    on which the program is examined.

A round that violates any of these is discarded and the previous artifact kept.
The loop stops as soon as the run is CONCLUSIVE: a counterexample that does not
pass through undefined behaviour, or a clean run that found none. It never
"succeeds" in the sense of establishing correctness, because CBMC cannot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from nfv.agent import integrity, library_theory
from nfv.agent.invariant_agent import USER_TEMPLATE, extract_code, render

SYSTEM = """\
You are a C engineer. The program below was checked by CBMC, a bounded model
checker, and the check was INCONCLUSIVE: the C has UNDEFINED BEHAVIOUR, so the
execution CBMC found says nothing about the Python program it was translated
from.

Your job is to fix the TRANSLATION so that it has no undefined behaviour. It is
NOT to make the assertion pass. The Python program may well be buggy; if it is,
the corrected translation should still be refuted, and that is the right answer.

WHAT USUALLY CAUSES THIS

- Signed overflow. Python integers are unbounded. Use `long` throughout, and
  where a sum or product can still exceed it, say so in `{entry}_pre` ONLY if
  the documentation supports a bound.
- Float to integer conversion. `(long)x` is undefined outside the integer range,
  whereas Python's `int(x)` is total. Guard the conversion or restrict the input
  exactly as the documentation does.
- Array bounds. A C array does not carry its length. Size every array to a bound
  the precondition guarantees, and check every index against the length
  parameter before using it.
- Nondeterministic inputs built by type-punning bits through a union. That
  manufactures NaN, infinity and denormals. Call `nondet_long()`,
  `nondet_double()` or `nondet_int()` instead.

WHAT YOU MAY NOT DO

- Do not modify any line tagged `{c} src:` — that is the program under test.
- Do not weaken `{entry}_post`, negate the assertion, or remove it.
- Do not add an `__CPROVER_assume` that is not `{entry}_pre`, and do not
  strengthen `{entry}_pre` beyond what the documentation states. Excluding the
  inputs on which the program misbehaves is hiding a bug, not fixing a
  translation.
- Do not add `#include` lines.

OUTPUT: only the full revised ```{fence} code block. No prose.
"""


@dataclass
class RepairResult:
    result: object          # the final CbmcResult
    model: str              # the artifact it was obtained from
    rounds: int = 0
    rejected: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)


async def repair_ub(
    chat,
    bk,
    *,
    frozen_model: str,
    entry_point: str,
    result,
    theory: str = "",
    base_precondition: list[str] | None = None,
    max_rounds: int = 3,
    unroll: int | None = None,
    verify_timeout: float = 300.0,
    llm_timeout: float = 600.0,
) -> RepairResult:
    """Re-translate until the check is conclusive or the round budget runs out."""
    base_precondition = base_precondition or []
    current = frozen_model
    out = RepairResult(result=result, model=current)
    system = render(SYSTEM.replace("{entry}", entry_point), bk)

    round_i = 0
    while round_i < max_rounds and result.is_malformed:
        round_i += 1
        diagnostics = "\n".join(result.wellformedness_issues) + "\n\n" + \
            ((result.stdout or "") + (result.stderr or "")).strip()
        user = USER_TEMPLATE.format(round_i=round_i, max_rounds=max_rounds,
                                    diagnostics=diagnostics[:4000], model=current,
                                    fence=bk.lang, context="")
        resp = await chat.ask(system, user, timeout=llm_timeout)
        out.transcript.append({"round": round_i, "response": resp.text,
                               "elapsed_s": resp.elapsed_s})

        candidate = extract_code(resp.text)
        if candidate is None:
            out.rejected.append(f"round {round_i}: no {bk.lang} block")
            continue

        frz = integrity.check_freeze(frozen_model, candidate, bk.comment,
                                     allow_proof_annotations=bk.relax_src_freeze)
        if not frz.ok:
            out.rejected.append(
                f"round {round_i}: freeze violation: {'; '.join(frz.violations)}")
            continue
        if theory.strip() and not library_theory.contains(candidate, theory, bk.comment, bk.lang):
            out.rejected.append(f"round {round_i}: library theory modified")
            continue

        integ = bk.integrity_check(candidate, entry_point)
        if not integ.ok:
            out.rejected.append(
                f"round {round_i}: integrity: {'; '.join(integ.violations)}")
            continue
        added = [p for p in integ.assumed_precondition if p not in base_precondition]
        if added:
            # The one move that would let this loop cheat: assume away the inputs
            # on which the program is examined.
            out.rejected.append(
                f"round {round_i}: strengthened entry precondition: {'; '.join(added)}")
            continue

        result = await asyncio.to_thread(
            bk.run, candidate, mode="bounded", unroll=unroll, timeout=verify_timeout)
        current = candidate
        out.result, out.model, out.rounds = result, current, round_i

    out.result, out.model = result, current
    out.rounds = round_i
    return out
