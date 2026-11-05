#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""LLM-as-judge: the baseline that skips the formalism entirely.

The pipeline's claim is that a verdict is worth something because a mature
verifier produced it and the model only ever translated. The obvious objection
is that the formalism is ceremony -- that a competent model, shown the same
program and the same specification, would answer as well by inspection. This
arm runs that objection as an experiment.

WHAT THE JUDGE SEES, AND WHY IT IS EXACTLY THIS

The judge is given the same three things the formal pipeline is given, and
nothing else:

  1. The program, with EVERY comment and docstring stripped. This matters more
     than it looks. HumanEval docstrings state the intended behaviour in
     English and usually give worked examples, so a judge that reads one is not
     reasoning about the code at all -- it is comparing two statements of the
     specification, one of which was written by the benchmark's author to
     describe the reference implementation. Every mutant would be detectable by
     reading prose. Stripping is what makes the task the same task.

  2. The docstring's INPUT ASSUMPTIONS, and only those. The formal pipeline
     gets these as `requires` clauses, so withholding them would rig the
     comparison the other way: the judge would be answering a question over an
     unrestricted domain that the pipeline never had to answer. They are
     extracted by a separate goal-blind pass (`_extract_assumptions`) that sees
     the docstring alone -- never the code, never the specification, never the
     label -- and must return sentences copied verbatim, which is checked here
     rather than trusted. A sentence that is not in the docstring is dropped.

     The verbatim rule buys auditability at a price: HumanEval docstrings often
     state an input constraint and the goal in ONE sentence ("Given a list of
     at least two elements, return the two closest"), and such a sentence is
     returned whole. The judge therefore sometimes sees more than the input
     assumption. That error is in the baseline's favour, which is the only
     direction in which it is safe to be wrong here.

  3. The specification, as the same Python `assert` the pipeline translates.

The judge may answer UNKNOWN, because the pipeline may abstain and a baseline
that is forced to guess is not being measured on the same scale.

WHAT THIS ARM CANNOT SHOW. A good score here is not a refutation of the
pipeline and a bad one is not a vindication of it: the judge produces an
opinion and the pipeline produces an artefact a verifier checked. The number
that matters is the one the naive baseline already established as the trap --
accuracy on correct and on buggy programs SEPARATELY, since answering
`correct` to everything scores 50% overall and knows nothing.

Usage:
    python -m nfv.run_judge --model gpt-5.6-sol --concurrency 12 \
        --out paper/new-results/judge.jsonl --resume
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import io
import json
import re
import time
import tokenize
from pathlib import Path

from nfv.metrics import ABSTAIN, CORRECT, INCORRECT, compute, render
from nfv.shared.copilot_client import CopilotChat
from nfv.shared.store import Store

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "paper" / "data" / "nl2post_safe.json"
ASSUMPTIONS = ROOT.parent / "paper" / "data" / "input_assumptions.json"


# --------------------------------------------------------------------------
# Stripping the program


def strip_docs(code: str) -> tuple[str, bool]:
    """Remove every comment and docstring, keeping the code itself intact.

    Returns (stripped, ok). `ok` is False when the result would not parse, in
    which case the original is returned unchanged -- an unparseable program
    would change what is being judged, which is worse than a leaked comment,
    and the flag is recorded per entry so the exception is visible.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, False

    drop: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            # A docstring that is the whole body cannot go without leaving a
            # syntax error behind; such a function has nothing to judge anyway.
            if len(body) > 1 or isinstance(node, ast.Module):
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    lines = code.splitlines()
    kept = [ln for i, ln in enumerate(lines, 1) if i not in drop]
    out = "\n".join(kept)

    # Comments go through the tokenizer rather than a regex, so a `#` inside a
    # string literal survives.
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(out).readline))
        toks = [t for t in toks if t.type != tokenize.COMMENT]
        out = tokenize.untokenize([(t.type, t.string, t.start, t.end, t.line)
                                   for t in toks])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass

    out = "\n".join(ln.rstrip() for ln in out.splitlines())
    out = re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"

    try:
        ast.parse(out)
    except SyntaxError:
        return code, False
    return out, True


# --------------------------------------------------------------------------
# Input assumptions: a goal-blind pass over the docstring alone

EXTRACT_SYSTEM = """\
You extract input assumptions from function documentation.

You will be shown documentation text and NOTHING else -- no implementation, no
specification, no tests. Return the sentences that constrain what the CALLER
may pass in: types, ranges, sizes, well-formedness, stated guarantees about the
input, and explicit "you may assume" clauses.

Do NOT return anything that describes what the function computes, returns, or
guarantees about its output, and do not return examples.

Copy sentences VERBATIM from the text. Do not paraphrase, merge, or shorten
them. If the text states no constraint on the input, return an empty list.

Reply with a JSON array of strings and nothing else."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


async def _extract_assumptions(chat: CopilotChat, docstring: str,
                               timeout: float) -> list[str]:
    """Sentences of `docstring` that constrain the input, verified verbatim."""
    r = await chat.ask(EXTRACT_SYSTEM, docstring.strip(), timeout=timeout)
    text = r.text.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        got = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    hay = _norm(docstring)
    # The verbatim rule is enforced, not requested. A model that paraphrases an
    # assumption has authored one, and an authored assumption is exactly the
    # move the pipeline forbids.
    return [s for s in got if isinstance(s, str) and s.strip()
            and _norm(s) in hay]


def load_assumptions() -> dict[str, dict]:
    if ASSUMPTIONS.exists():
        return json.loads(ASSUMPTIONS.read_text(encoding="utf-8"))
    return {}


def save_assumptions(d: dict[str, dict]) -> None:
    ASSUMPTIONS.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n",
                           encoding="utf-8")


def _doc_key(entry: dict) -> str:
    """Key by task, salted by the docstring, so an edited corpus re-extracts.

    A reference and its mutant share a docstring by construction, so this also
    halves the extraction cost and guarantees the pair is judged under
    identical assumptions -- if they differed, a verdict difference could come
    from the assumptions rather than from the bug.
    """
    h = hashlib.sha256(entry["docstring"].encode("utf-8")).hexdigest()[:12]
    return f"{entry['task_id']}::{h}"


# --------------------------------------------------------------------------
# The judge

JUDGE_SYSTEM = """\
You are a program verification judge.

You are given a Python function, the assumptions a caller must satisfy, and an
assertion about the function's return value. Decide whether the assertion holds
for EVERY input satisfying the assumptions -- not for typical inputs, not for
the inputs a test suite would try. A single input on which the assertion fails
makes the answer INCORRECT.

Reason as carefully as you need to, then end your reply with exactly one line:

VERDICT: CORRECT      -- the assertion holds on every admissible input
VERDICT: INCORRECT    -- some admissible input makes the assertion fail
VERDICT: UNKNOWN      -- you cannot determine which

UNKNOWN is a real answer and is preferred to a guess. It is scored separately,
not as a mistake."""

JUDGE_USER = """\
Function under judgement:

```python
{code}
```

Assumptions on the input:
{assumptions}

Assertion, evaluated after the call, where `return_value` is what \
`{entry_point}` returned:

```python
{postcondition}
```

Does the assertion hold for every input satisfying the assumptions?"""

_VERDICT = re.compile(r"VERDICT:\s*\**\s*(CORRECT|INCORRECT|UNKNOWN)", re.I)


def parse_verdict(text: str) -> tuple[str, str]:
    """Map the reply to a prediction. The LAST verdict line wins."""
    hits = _VERDICT.findall(text or "")
    if not hits:
        return ABSTAIN, "no verdict line in reply"
    v = hits[-1].lower()
    if v == "correct":
        return CORRECT, "judge: correct"
    if v == "incorrect":
        return INCORRECT, "judge: incorrect"
    return ABSTAIN, "judge: unknown"


async def judge_entry(chat: CopilotChat, entry: dict, assumptions: list[str],
                      args) -> dict:
    started = time.time()
    code, ok = strip_docs(entry["code"])
    block = ("\n".join(f"- {s.strip()}" for s in assumptions)
             if assumptions else "- (none stated; the input is unconstrained)")
    prompt = JUDGE_USER.format(code=code, assumptions=block,
                               entry_point=entry["entry_point"],
                               postcondition=entry["postcondition"].strip())
    rec = {
        "entry_id": entry["entry_id"], "task_id": entry["task_id"],
        "entry_point": entry["entry_point"], "label": entry["label"],
        "backend": "llm-judge", "model": args.model, "phase_reached": "judge",
        "stripped_ok": ok, "stripped_code": code,
        "input_assumptions": assumptions,
    }
    try:
        r = await chat.ask(JUDGE_SYSTEM, prompt, timeout=args.llm_timeout)
    except Exception as e:                                  # noqa: BLE001
        rec.update(prediction=ABSTAIN, reason=f"llm error: {type(e).__name__}",
                   wall_s=round(time.time() - started, 2))
        return rec
    pred, why = parse_verdict(r.text)
    rec.update(prediction=pred, reason=why, judge_reply=r.text,
               input_tokens=r.input_tokens, output_tokens=r.output_tokens,
               nano_aiu=r.nano_aiu, api_calls=r.api_calls,
               wall_s=round(time.time() - started, 2))
    return rec


# --------------------------------------------------------------------------


async def main_async(args) -> int:
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    entries = data["entries"]
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        entries = [e for e in entries
                   if any(k in e["entry_id"] or k in e["task_id"] for k in keys)]
    if args.limit:
        entries = entries[:args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    store = Store(out)
    done = store.done_ids() if args.resume else set()
    todo = [e for e in entries if e["entry_id"] not in done]
    print(f"llm-as-judge: {len(todo)} to run, {len(done)} already in {out}")
    if args.report_only:
        recs = store.records()
        print(render(compute(recs), title=out.stem, phase="judge"))
        return 0
    if not todo:
        print(render(compute(store.records()), title=out.stem, phase="judge"))
        return 0

    cache = load_assumptions()
    async with CopilotChat(model=args.model, concurrency=args.concurrency,
                           allow_emu=args.allow_emu) as chat:
        # Phase 1. Extraction is separate and comes first so that no judgement
        # can influence an assumption. Both members of a pair share one call.
        missing = {}
        for e in todo:
            k = _doc_key(e)
            if k not in cache:
                missing[k] = e
        if missing:
            print(f"extracting input assumptions for {len(missing)} docstrings "
                  f"(goal-blind: docstring only)")
            sem = asyncio.Semaphore(args.concurrency)

            async def one(k: str, e: dict):
                async with sem:
                    try:
                        got = await _extract_assumptions(chat, e["docstring"],
                                                         args.llm_timeout)
                    except Exception as exc:                # noqa: BLE001
                        print(f"  ! {k}: {type(exc).__name__}")
                        return
                    cache[k] = {"task_id": e["task_id"], "sentences": got,
                                "docstring": e["docstring"]}

            await asyncio.gather(*(one(k, e) for k, e in missing.items()))
            save_assumptions(cache)
            print(f"assumptions written to {ASSUMPTIONS}")

        # Phase 2. The judgements.
        sem2 = asyncio.Semaphore(args.concurrency)
        n = [0]

        async def run_one(e: dict):
            async with sem2:
                sents = cache.get(_doc_key(e), {}).get("sentences", [])
                rec = await judge_entry(chat, e, sents, args)
                store.append(rec)
                n[0] += 1
                print(f"[{n[0]}/{len(todo)}] {rec['entry_id']:<44} "
                      f"{rec['prediction']:<9} (label {rec['label']})")

        await asyncio.gather(*(run_one(e) for e in todo))
        usage = chat.usage

    recs = store.records()
    print(render(compute(recs), title=out.stem, phase="judge"))
    print("\n--- LLM usage ---")
    print(usage.render())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-as-judge baseline")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default=str(ROOT / "results" / "judge.jsonl"))
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--llm-timeout", type=float, default=600.0)
    ap.add_argument("--allow-emu", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
