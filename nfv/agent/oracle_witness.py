"""Render a KNOWN killing input as a target-language precondition.

This exists to separate two abilities that the ordinary refutation arm measures
together:

  1. can the model FIND an input on which the program is wrong?
  2. given that input, can the pipeline PROVE the postcondition fails on it?

The refutation arm answers (1) and (2) jointly, so a failure is unattributable.
Here the input is handed over from the dataset's killing test, so a failure is
attributable to (2) -- to translation and proof, not to search.

This is deliberately a *rendering* task, not a search task. The model is given
concrete Python argument values and asked to pin them in the target's syntax.
It gets no history, no retries with different witnesses, and no licence to pick
a different input: any answer that is not the given one invalidates the
ablation. The mechanical alternative -- literal-to-literal mapping -- is not
usable here because translations invent their own type models (tagged unions
for Python's numeric tower, sequences for lists, datatypes for tuples), so the
rendering has to be aware of the signature that was actually produced.
"""
from __future__ import annotations

from nfv.agent.refute import InputGuess, parse_guess
from nfv.agent.translate import number_source, target_name
from nfv.backends.registry import Backend, get as get_backend
from nfv.shared.copilot_client import CopilotChat

SYSTEM = """\
You translate a CONCRETE INPUT into a {target} precondition. You do not search
for one, and you do not judge whether it is a good input: it has already been
established, by execution, that this input breaks the program.

WHAT YOU ARE GIVEN
The exact Python argument values the function was called with when it failed,
in positional order, and a {target} translation of that function whose goal has
been NEGATED (the verifier is being asked to prove the postcondition FAILS).

WHAT TO PRODUCE
`requires` clauses that pin those arguments EXACTLY, in the target's syntax,
using the method's own parameter names:

    requires n == 3
    requires |s| == 2 && s[0] == 1 && s[1] == 1

RULES
- Reproduce the GIVEN values. Do not simplify them, do not generalise them to a
  domain, and do not substitute an input you think is better. If the given
  values cannot be expressed in this signature, say so instead of approximating.
- Pin the value COMPLETELY. For a sequence, fix its length and every element.
  A partially constrained input leaves the verifier a choice, and the proof may
  then fail for a reason that has nothing to do with the bug.
- Match the TYPE MODEL of the translation, which may not be a literal mirror of
  Python. If the signature takes `seq<PythonNumber>` and the value is `[1, 2.5]`,
  write the constructors that model those numbers. If it takes `real` and the
  value is `3`, write `3.0`. Read the signature before you write.
- Constrain ONLY the parameters. Say nothing about the return value, the
  postcondition, or the value of any library or helper function.

OUTPUT
A fenced ```{fence} block containing ONLY the `requires` lines. Then one line of
prose naming the input you encoded.

If the input genuinely cannot be expressed against this signature, output an
empty fenced block and explain why in one line.
"""

USER_TEMPLATE = """\
Python source:
```python
{numbered_source}
```

Entry point: {entry_point}

The postcondition the program is supposed to satisfy (the verifier has been
asked to prove its NEGATION):
```python
{postcondition}
```

THE INPUT THAT BREAKS IT. These are the positional arguments to
`{entry_point}`, exactly as they were passed when the program failed:
```python
{entry_point}({args})
```

The model under test, with the negated goal:
```{fence}
{model}
```

Write the `requires` clauses that pin exactly this input.
"""


def format_args(killing_test) -> str:
    """Render the stored argument list as a Python call argument string.

    The killing test is stored as a JSON list of positional arguments. `repr`
    is used rather than `json.dumps` so the model sees Python literals
    (`True`, `None`, `'x'`) rather than JSON ones (`true`, `null`, `"x"`).
    """
    if not isinstance(killing_test, list):
        return repr(killing_test)
    return ", ".join(repr(a) for a in killing_test)


async def render(
    chat: CopilotChat,
    *,
    killing_test,
    python_source: str,
    postcondition: str,
    entry_point: str,
    model: str,
    backend: Backend | str = "dafny",
    timeout: float = 600.0,
) -> InputGuess:
    """Phase 2', oracle variant: encode the given killing input as `requires`."""
    bk = get_backend(backend) if isinstance(backend, str) else backend
    system = (SYSTEM.replace("{target}", target_name(bk))
                    .replace("{fence}", bk.lang))
    user = USER_TEMPLATE.format(
        numbered_source=number_source(python_source),
        entry_point=entry_point,
        postcondition=postcondition.strip(),
        args=format_args(killing_test),
        fence=bk.lang,
        model=model,
    )
    resp = await chat.ask(system, user, timeout=timeout)
    return parse_guess(resp.text, bk)


def _selftest() -> int:
    cases = [
        ([[1.0, 2.0], 0.3], "[1.0, 2.0], 0.3"),
        (["(())"], "'(())'"),
        ([3.5], "3.5"),
        ([[1, 2, -3]], "[1, 2, -3]"),
        ([True, None], "True, None"),
        ([], ""),
    ]
    ok = True
    for kt, want in cases:
        got = format_args(kt)
        good = got == want
        ok &= good
        print(f"  [{'OK  ' if good else 'FAIL'}] {kt!r} -> {got!r}")
    print("PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
