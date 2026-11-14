#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Stage 1 — Translation.

Python -> Dafny, literal and statement-by-statement, followed by a strictly
SYNTACTIC repair loop driven only by `dafny resolve` diagnostics.

The stage ends in a FREEZE: once the model type-checks and passes the provenance
and integrity audits, its `// src:` lines are hashed and become immutable. Stage 2
may only touch `// synth:` lines.

Critically, this stage never sees a verification result. The only feedback the
model gets is the type checker, so no correctness signal can reach the one loop
that is able to rewrite program text.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from nfv.agent import integrity, library_theory, provenance
from nfv.backends import dafny
from nfv.backends.registry import Backend, get as get_backend
from nfv.shared.copilot_client import CopilotChat

SYSTEM = """\
You translate Python to {target}.{syntax_guide} You are a TRANSLITERATOR, not a programmer.

ROLE
Re-express the given program in {target}, statement by statement. You are NOT being
asked whether the program is correct, and must not decide that. A separate formal
verifier makes every correctness judgment.

FIDELITY - highest priority, overrides everything below
- Translate literally, statement by statement, in source order.
- The Python program MAY CONTAIN BUGS. PRESERVE THEM EXACTLY. If the source is
  wrong, your translation must be wrong in the same way.
- Never repair, improve, simplify, optimize, refactor, or normalize.
- Never reorder statements, strengthen a condition, add a guard, or handle an
  edge case the source does not handle.
- Preserve control-flow shape: loops stay loops, branches stay branches, early
  returns stay early returns.
- If a statement looks wrong, redundant, or pointless: translate it anyway,
  unchanged. Do not comment on it.

PROVENANCE - mandatory, mechanically checked, violations are rejected
- Derived from source:  {c} src: <file>:<line> | <verbatim source statement>
  The quoted text must match that source line EXACTLY, character for character
  (after stripping leading indentation).
- No source counterpart: {c} synth: <short reason>
- NO UNTAGGED LINES. Every non-blank line of your output must be preceded by a
  tag comment, or be a tag comment, or be a closing brace.
- Do not drop an executable source line. Do not invent or shift line numbers.
  {c} src: refs must appear in non-decreasing source-line order.

SPECIFICATION
{spec_block}

PRECONDITION
{pre_block}

LIBRARY FUNCTIONS
{lib_block}

SYNTAX ONLY
If the file fails to parse or type-check you will be given the verifier's
diagnostics and may fix ONLY what a diagnostic names. You will never be told
whether the program verifies, and must never adjust the translation to change a
verification outcome.

OUTPUT: only a ```{fence} code block. No prose.
"""

REPAIR_SYSTEM = SYSTEM

# ---------------------------------------------------------------------------
# The naive baseline's prompt. Two sentences and a format instruction. Gone is
# the transliterator framing, the provenance tags, the ban on `assume`, the
# rules about what a `requires` may say and what an axiom may imply -- all of it
# existed to feed a gate, and the baseline has no gates.
#
# Note what is NOT said: that the program may be buggy and must not be fixed.
# The disciplined prompt says so in five separate rules. Here the model is free
# to translate the mutant into the program it thinks was meant, and if it does,
# the mutant will verify. That is not a flaw in the baseline -- it is the most
# likely way a naive pipeline fails, and stating the rule would hide it.
# ---------------------------------------------------------------------------
NAIVE_SYSTEM = """\
Given a Python function and a postcondition, produce a {target} method that
implements the same function and carries the postcondition as an `ensures`
clause.

OUTPUT: only a ```{fence} code block. No prose.
"""

NAIVE_USER_TEMPLATE = """\
Translate to {target}.

Source file: {filename}
```python
{numbered_source}
```

Entry point: {entry_point}

Documentation:
\"\"\"{docstring}\"\"\"

Postcondition:
```python
{postcondition}
```
"""

USER_TEMPLATE = """\
Translate to {target}.

Source file: {filename}
```python
{numbered_source}
```

Entry point: {entry_point}

Documentation (for the PRECONDITION only - it describes the intended input
domain, not the behaviour of this implementation):
\"\"\"{docstring}\"\"\"

Postcondition - translate into the goal/`ensures` for this target:
```python
{postcondition}
```
{reference_block}{theory_block}"""

# Phase-0 output, handed to translation. The theory is already type-checked and
# is frozen from here on: reproducing it verbatim is what lets a later audit say
# that every library assumption in the model was written before the goal was in
# view.
THEORY_BLOCK = """\

LIBRARY THEORY. These declarations were written before the program was translated
and form part of the trusted base. Your output file MUST begin by reproducing ALL
of them VERBATIM -- every declaration and every `{comment} doc:` comment, character for
character, including any you do not end up using. Reproduce them even if a
declaration looks unnecessary for this program; dropping, renaming, reordering or
editing any of them (including its contract clauses) invalidates the run.
Then write the program translation below them.

```{lang}
{theory}
```"""

REPAIR_TEMPLATE = """\
The file failed to type-check. Fix ONLY the errors listed below.
Do not change any logic, control flow, or the stated postcondition.
Provenance tags must be preserved.

Type-checker output:
{diagnostics}

Current program:
```{fence}
{model}
```
"""

_FENCE = re.compile(r"```[a-z0-9_+-]*\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def render_system(backend) -> str:
    """The shared SYSTEM prompt, specialised to a target language.

    Substitution is done with `str.replace`, not `str.format`: the prompts and
    the syntax guides both contain literal braces (`{ ... }`, `{:axiom}`,
    `(assert ...)`), and `format` would either choke on them or require escaping
    that makes the prompt text unreadable and easy to break.
    """
    out = NAIVE_SYSTEM if getattr(backend, "naive", False) else SYSTEM
    for key, val in (("{syntax_guide}", backend.syntax_guide),
                     ("{spec_block}", backend.spec_block),
                     ("{pre_block}", backend.pre_block),
                     ("{lib_block}", backend.lib_block),
                     ("{target}", target_name(backend)),
                     ("{fence}", backend.lang),
                     ("{c}", backend.comment)):
        out = out.replace(key, val)
    return out


# Keyed on backend.lang, NOT backend.name: keying on the arm name meant every
# new arm had to be registered here too, and forgetting silently turned into
# `translation error: KeyError` for the whole run (caught on dafny-theory-total).
_LANG_NAME = {"dafny": "Dafny", "c": "C"}


def target_name(backend) -> str:
    try:
        return _LANG_NAME[backend.lang]
    except KeyError:
        raise KeyError(
            f"backend {backend.name!r} has unknown lang {backend.lang!r}; "
            f"add it to _LANG_NAME (known: {sorted(_LANG_NAME)})") from None


PRECOND_BLOCK = """\

FROZEN PRECONDITION (phase 0b)
The precondition below was written from the source and the docstring alone,
before the specification was known. Reproduce it EXACTLY on `{entry}`, and add
no other precondition.

You may NOT add a precondition of your own, for any reason -- not to make an
index in range, not to avoid a division by zero, not to exclude an empty input.
If the program misbehaves on an input the precondition admits, that is a
property of the program and must remain visible. Restricting the input to avoid
it would hide exactly what this translation exists to expose.

{clauses}
"""


def number_source(source: str) -> str:
    lines = source.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}}: {ln}" for i, ln in enumerate(lines, 1))


def extract_code(text: str) -> str | None:
    """The largest fenced block, or plausible bare output if the fence was skipped."""
    blocks = _FENCE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    # Model ignored the fence instruction but emitted plausible target code.
    if re.search(r"^\s*(method|function|lemma|predicate|procedure|\(define-fun|\(declare-)",
                 text, re.MULTILINE):
        return text.strip() + "\n"
    return None


# Back-compat alias: the proof agent and older callers import this name.
extract_dafny = extract_code


@dataclass
class TranslationResult:
    ok: bool
    model: str | None
    rounds: int
    reason: str = ""
    freeze_hash: str | None = None
    provenance_issues: list[str] = field(default_factory=list)
    integrity_issues: list[str] = field(default_factory=list)
    synth_lines: int = 0
    trusted_base: list[str] = field(default_factory=list)
    assumed_precondition: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)


async def translate(
    chat: CopilotChat,
    entry: dict,
    source_path: Path,
    *,
    backend: Backend | str = "dafny",
    max_rounds: int = 3,
    timeout: float = 240.0,
    reference_block: str = "",
    theory: str = "",
    precondition: list[str] | None = None,
) -> TranslationResult:
    bk = get_backend(backend) if isinstance(backend, str) else backend
    naive = getattr(bk, "naive", False)
    source = source_path.read_text(encoding="utf-8")
    prompt = (NAIVE_USER_TEMPLATE if naive else USER_TEMPLATE).format(
        target=target_name(bk),
        filename=source_path.name,
        numbered_source=number_source(source),
        entry_point=entry["entry_point"],
        docstring=(entry.get("docstring") or "").strip() or "(no documentation)",
        postcondition=entry["postcondition"].strip(),
        reference_block=("\n" + reference_block) if reference_block.strip() else "",
        theory_block=(THEORY_BLOCK.replace("{lang}", bk.lang)
                      .replace("{comment}", bk.comment)
                      .replace("{theory}", theory)
                      if theory.strip() else ""),
    )

    transcript: list[dict] = []
    model_text: str | None = None
    system_prompt = render_system(bk)
    # Carried on the SYSTEM prompt, not the first user turn, so the constraint
    # is still in force during the syntax-repair rounds. When no precondition
    # was authorised the block still appears, stating that none is permitted --
    # an empty list is a positive claim ("no restriction"), not an absence.
    if precondition is not None:
        clauses = "\n".join(precondition) if precondition else "(none: the entry point takes every input of its declared types)"
        system_prompt += PRECOND_BLOCK.replace(
            "{entry}", entry["entry_point"]).replace("{clauses}", clauses)
    system, user = system_prompt, prompt

    for round_i in range(1, max_rounds + 1):
        resp = await chat.ask(system, user, timeout=timeout)
        transcript.append({"round": round_i, "prompt": user, "response": resp.text,
                           "elapsed_s": resp.elapsed_s})

        candidate = extract_code(resp.text)
        if candidate is None:
            return TranslationResult(False, model_text, round_i,
                                     f"no {bk.lang} code block in response",
                                     transcript=transcript)
        model_text = candidate

        # Off the event loop: see the note in invariant_agent.prove(). The
        # type-checker is a blocking subprocess and would otherwise stall every
        # other entry running on this loop.
        tc = await asyncio.to_thread(bk.type_check, model_text)
        if tc.outcome is dafny.Outcome.VERIFIED:
            break
        if round_i == max_rounds:
            return TranslationResult(False, model_text, round_i,
                                     f"type check failed after {max_rounds} rounds",
                                     transcript=transcript)

        diagnostics = ((tc.stdout or "") + (tc.stderr or "")).strip() or "unknown type error"
        system = system_prompt
        user = REPAIR_TEMPLATE.format(diagnostics=diagnostics[:4000], model=model_text,
                                      fence=bk.lang)
    else:  # pragma: no cover - loop always breaks or returns
        return TranslationResult(False, model_text, max_rounds, "exhausted rounds",
                                 transcript=transcript)

    assert model_text is not None

    prov = provenance.check(model_text, source, source_name=source_path.name,
                            comment=bk.comment)
    integ = bk.integrity_check(model_text, entry["entry_point"])
    theory_issues: list[str] = []
    if theory.strip():
        if not library_theory.contains(model_text, theory, bk.comment, bk.lang):
            theory_issues.append(
                "phase-0 library theory was not reproduced: a declaration was "
                "modified, dropped or reordered")
        elif not library_theory.docs_intact(model_text, theory, bk.comment,
                                            bk.lang):
            theory_issues.append(
                "phase-0 library theory lost its `doc:` comments")
    if not prov.ok or not integ.ok or theory_issues:
        # The naive baseline is allowed to fail all of these. They are still
        # COMPUTED and recorded -- what the baseline does wrong is the finding,
        # and it can only be reported if it is measured -- but they reject
        # nothing. This is the difference between observing a violation and
        # acting on one.
        if not naive:
            return TranslationResult(
                False, model_text, round_i, "artifact audit failed",
                provenance_issues=list(prov.violations),
                integrity_issues=list(integ.violations) + theory_issues,
                synth_lines=prov.synth_count, transcript=transcript,
            )

    return TranslationResult(
        True, model_text, round_i, "ok",
        freeze_hash=integrity.freeze_hash(model_text, bk.comment),
        provenance_issues=list(prov.violations),
        integrity_issues=list(integ.violations) + theory_issues,
        synth_lines=prov.synth_count, transcript=transcript,
        trusted_base=list(integ.trusted_base),
        assumed_precondition=list(integ.assumed_precondition),
    )
