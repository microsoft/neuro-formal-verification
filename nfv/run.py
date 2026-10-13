#!/usr/bin/env python3
"""
Single entry point for the neuro-formal verification pipeline.

    STAGE 1  TRANSLATION                    STAGE 2  VERIFICATION
      translate -> syntax repair -> [FREEZE] -> bounded (Cex?) -> proof repair
      (may edit program text)                   (program text immutable)

The verdict is a pure function of the verifier's exit status:

    bounded Cex        -> Incorrect
    unbounded Verified -> Correct
    anything else      -> abstain

The LLM never decides the verdict, never sees the label, and never learns whether
a previous attempt agreed with it.

Usage:
    python -m nfv.run --backend dafny --model gpt-5.5 --concurrency 8
    python -m nfv.run --backend dafny --limit 10 --out paper/new-results/smoke.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import replace
from pathlib import Path

from nfv.agent import (c_witness, invariant_agent, library_theory,
                       oracle_witness, pydocs, refute, tautology, translate,
                       ub_repair)
from nfv.backends import dafny
from nfv.backends.registry import BACKENDS as _BACKENDS
from nfv.backends.registry import get as get_backend
from nfv.metrics import ABSTAIN, CORRECT, INCORRECT, compute, render
from nfv.shared.account import AccountError, detect_account
from nfv.shared.copilot_client import CopilotChat, new_usage_scope
from nfv.shared.store import Store

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "paper" / "data" / "nl2post_safe.json"
WORK = ROOT / "work"


def _vacuity_gate(entry: dict, args: argparse.Namespace, rec: dict) -> dict | None:
    """Abstain up front when the specification is partly a type test.

    Runs on the stored PYTHON postcondition, so it costs nothing: no
    translation, no LLM call, no verifier call. That placement is deliberate --
    an entry whose specification will lose its discriminating conjunct to the
    target's type system cannot be rescued by a better translation, so paying
    for one is waste.

    Returns a finished record to short-circuit with, or None to proceed.
    """
    if not getattr(args, "vacuity_gate", False):
        return None
    tt = tautology.type_test_conjuncts(entry.get("postcondition", ""))
    if not tt:
        return None
    rec["prediction"] = ABSTAIN
    rec["phase_reached"] = "vacuity_gate"
    rec["vacuity_type_tests"] = tt
    rec["reason"] = ("vacuity gate: type-test conjunct is absorbed by the target "
                     "type system -- " + "; ".join(tt))[:300]
    return rec


def _early_stop(attempt_log: list[dict], args, started: float) -> str | None:
    """Should this entry stop redrawing? Returns a reason, or None to continue.

    Both rules are OFF by default so existing arms stay comparable; they exist
    because a run's wall time is dominated by entries that never prove.

    `--stop-after-repeats N`: F83 measured that 39% of multi-attempt entries had
    EVERY attempt fail identically, so once N consecutive attempts produce the
    same outcome string the remaining attempts are near-certain to waste an LLM
    round trip each. `--entry-deadline S` is the blunt backstop: no single entry
    may keep drawing past S seconds. Neither can turn an abstention into a proof
    or a proof into an abstention -- they only stop attempts that would have
    happened anyway, so they trade recall for wall time and never affect
    soundness.
    """
    deadline = getattr(args, "entry_deadline", 0.0) or 0.0
    if deadline > 0 and (time.time() - started) > deadline:
        return f"entry deadline of {deadline:.0f}s exceeded after {len(attempt_log)} attempts"

    n = getattr(args, "stop_after_repeats", 0) or 0
    if n > 0 and len(attempt_log) >= n:
        recent = [str(a.get("outcome", "")) for a in attempt_log[-n:]]
        if len(set(recent)) == 1 and recent[0]:
            return f"{n} consecutive attempts failed identically: {recent[0]}"[:300]
    return None


async def _pin_oracle_witness(chat, bk, args, entry: dict, rec: dict,
                              *, model: str, py_source: str):
    """Replace the checker's SEARCH with the dataset's killing input.

    Returns the CBMC result for the pinned harness, or None when the witness
    could not be installed -- in which case the caller keeps the ordinary
    result, and `oracle_reason` records why the ablation did not apply.

    Pinning only removes executions, so it cannot manufacture a counterexample:
    the failure mode is an abstention, and the reachability probe distinguishes
    "the pinned assumptions admit nothing" from "the translation does not break
    on this input", which is the answer the ablation exists to obtain.
    """
    entry_point = entry["entry_point"]
    kt = entry.get("killing_test")
    rec["oracle_witness"] = True
    rec["oracle_installed"] = False
    if kt is None:
        rec["oracle_reason"] = "oracle mode: entry has no killing test"
        return None
    rec["killing_test"] = kt

    # The bounded input domain is widened to fit this input exactly, and only
    # for the pinned run. Enlarging the domain adds executions, so it cannot
    # invalidate anything, and with the input fixed there is no search over the
    # larger domain to pay for. What the arm must not do is accept a DIFFERENT
    # input: the whole point is that the counterexample is the dataset's, not
    # one CBMC found for itself.
    bnd = c_witness.required_bounds(kt)
    rec["oracle_bounds"] = {k: bnd[k] for k in ("len", "int", "float", "unroll")}
    rec["oracle_widened"] = bnd["widened"]
    prelude = c_witness.make_prelude(len_bound=bnd["len"], int_bound=bnd["int"],
                                     float_bound=bnd["float"])
    unroll = max(args.unroll or 0, bnd["unroll"])

    g = await c_witness.render(
        chat, killing_test=kt, python_source=py_source,
        postcondition=entry["postcondition"], entry_point=entry_point,
        model=model, backend=bk, timeout=args.llm_timeout)
    rec["witness"] = g.clauses
    rec["witness_rationale"] = g.rationale
    if not g.ok or not g.clauses:
        rec["oracle_reason"] = f"witness not encodable: {g.reason or g.rationale}"
        return None

    inst = c_witness.install(model, g.clauses)
    if not inst.ok:
        rec["oracle_reason"] = f"witness install rejected: {inst.reason}"
        return None
    # The install is mechanical and round-trip checked, but the audit is run
    # again anyway: it is what certifies that the goal still asserts the
    # postcondition about the program, and an arm that skipped it here would be
    # reporting refutations under weaker guarantees than the arm it is compared
    # against.
    integ = bk.integrity_check(inst.model, entry_point)
    if not integ.ok:
        rec["oracle_reason"] = ("pinned harness failed the integrity audit: "
                                + "; ".join(integ.violations))
        return None
    rec["oracle_installed"] = True
    rec["oracle_assumes"] = inst.lines
    rec["model_pinned"] = inst.model

    probe = await asyncio.to_thread(
        bk.run, c_witness.probe(inst.model), mode="bounded",
        unroll=unroll, timeout=args.verify_timeout, prelude=prelude)
    rec["oracle_reachable"] = bool(probe.is_cex)

    res = await asyncio.to_thread(
        bk.run, inst.model, mode="bounded", unroll=unroll,
        timeout=args.verify_timeout, prelude=prelude)
    rec["oracle_outcome"] = res.outcome.value
    return res


async def run_entry(
    chat: CopilotChat,
    entry: dict,
    args: argparse.Namespace,
) -> dict:
    entry_id = entry["entry_id"]
    bk = _resolve_backend(args)
    started = time.time()
    _usage = new_usage_scope()
    rec: dict = {
        "entry_id": entry_id,
        "task_id": entry.get("task_id"),
        "entry_point": entry.get("entry_point"),
        "label": entry.get("label"),
        "backend": args.backend,
        "model": args.model,
        "prediction": ABSTAIN,
        "reason": "",
        "phase_reached": "translation",
    }

    source_path = WORK / entry_id / "source.py"

    def _finish(r: dict) -> dict:
        r["nano_aiu"] = _usage.nano_aiu
        r["api_calls"] = _usage.api_calls
        r["input_tokens"] = _usage.input_tokens
        r["output_tokens"] = _usage.output_tokens
        r["transport_retries"] = _usage.transport_retries
        return r

    if not source_path.exists():
        rec["reason"] = "missing materialized source (run python -m paper.prepare)"
        rec["wall_s"] = time.time() - started
        return _finish(rec)

    gated = _vacuity_gate(entry, args, rec)
    if gated is not None:
        rec["wall_s"] = time.time() - started
        return _finish(gated)

    # ---- Phase 0: documented library theory, written before the goal is visible
    # Runs once per entry, from the Python source alone. Independent of every
    # other entry: nothing is cached or shared, so no entry's theory can be
    # influenced by another's.
    py_source = source_path.read_text(encoding="utf-8")
    # NOTE (RESULTS.md F59): reference_block is TAILORED to the builtins this
    # entry calls -- 145 distinct blocks across the 206-entry benchmark, empty
    # for 14 -- so the prompt is a function of the entry. pydocs.reference_corpus()
    # exists to fix that (one 6.8k-char corpus for everyone) and is deliberately
    # NOT wired in here: switching it would change the prompt of the already-run
    # `-plus` arms and invalidate their recorded results. Only the deprecated
    # `-plus` arms reach this line; every phase-0b theory arm has
    # python_reference off, so ref_block is empty by construction.
    ref_block = pydocs.reference_block(py_source) if bk.python_reference else ""
    rec["reference_block_chars"] = len(ref_block)

    theory = ""
    if bk.library_theory:
        th = await library_theory.synthesize(chat, py_source, bk,
                                             timeout=args.llm_timeout)
        rec["theory_ok"] = th.ok
        rec["theory_rounds"] = th.rounds
        rec["theory_reason"] = th.reason
        rec["theory_decls"] = th.decls
        rec["theory_documented"] = th.documented
        rec["theory_axioms"] = th.axioms
        rec["theory_undocumented"] = th.undocumented
        rec["theory_chars"] = len(th.text)
        if not th.ok:
            # A theory that will not type-check must not be forced into the
            # prompt: it would poison translation for a reason unrelated to the
            # program. Fall through with no theory and record why.
            rec["theory_reason"] = f"unused: {th.reason}"
        else:
            theory = th.text
            rec["theory"] = th.text

    # ---- Phase 0b: the input DOMAIN, also written before the goal is visible.
    # Phase 0 stops the model assuming its way to a proof through the library;
    # this stops it doing so through the precondition. Authored from the source
    # and docstring only, then frozen: no later phase may restrict the domain.
    # `None` means "no freeze in force" and MUST stay None for every other arm:
    # an empty list is a real constraint (no precondition permitted), and would
    # change the frozen baseline prompts if it leaked into them.
    rec["cfg_phase0b"] = bool(getattr(bk, "phase0_precondition", False))
    rec["cfg_theory"] = bool(getattr(bk, "library_theory", False))
    rec["cfg_abduce"] = bool(getattr(bk, "abduce_definedness", False))
    precondition: list[str] | None = None
    if getattr(bk, "phase0_precondition", False):
        pre = await library_theory.synthesize_precondition(
            chat, py_source, entry.get("docstring", ""), bk,
            timeout=args.llm_timeout)
        rec["phase0_pre_ok"] = pre.ok
        rec["phase0_pre_reason"] = pre.reason
        rec["phase0_precondition"] = pre.items
        rec["phase0_pre_undocumented"] = pre.undocumented
        # If phase 0b failed to produce a usable answer, the safe reading is
        # that nothing licenses a domain restriction, so freeze the empty set
        # rather than handing the decision back to a goal-aware phase.
        precondition = pre.items if pre.ok else []

    # ---- Stage 1+2, retried end to end -------------------------------------
    # Provability is a property of the TRANSLATION, not of the Python program:
    # two runs of this pipeline disagree on ~10% of entries because stage 1
    # samples a different encoding each time. So a failed attempt is retried
    # from translation, not from repair -- re-repairing the same dead encoding
    # is what does not work. Attempts stop at the first proof.
    attempts_used = 0
    attempt_log: list[dict] = []
    tr = pr = None
    for attempt in range(1, max(1, args.attempts) + 1):
        stop = _early_stop(attempt_log, args, started)
        if attempt > 1 and stop:
            rec["early_stop"] = stop
            break
        attempts_used = attempt
        try:
            tr = await translate.translate(
                chat, entry, source_path, backend=bk,
                max_rounds=args.max_syntax_rounds, timeout=args.llm_timeout,
                reference_block=ref_block, theory=theory,
                precondition=precondition,
            )
        except Exception as exc:
            rec["reason"] = f"translation error: {type(exc).__name__}: {exc}"
            rec["wall_s"] = time.time() - started
            return _finish(rec)

        rec["translation_rounds"] = tr.rounds
        rec["synth_lines"] = tr.synth_lines
        rec["trusted_base"] = tr.trusted_base
        rec["assumed_precondition"] = tr.assumed_precondition
        rec["model_dafny"] = tr.model
        if not tr.ok:
            attempt_log.append({"attempt": attempt, "outcome": f"translation: {tr.reason}"})
            rec["reason"] = f"translation: {tr.reason}"
            rec["provenance_issues"] = tr.provenance_issues
            rec["integrity_issues"] = tr.integrity_issues
            continue

        # Asking for the frozen precondition is a request; checking it is what
        # makes the phase mean anything. But the check has to survive the fact
        # that translation may legitimately RE-ENCODE an authorised clause:
        # phase 0b writes `number > 0.0` and translation emits
        # `FloatGreater(number, FloatZero())`, which is the same constraint
        # through the library theory. Comparing those as text reports a
        # violation that is not one, and at attempts=1 that silently converts a
        # provable entry into an abstention.
        #
        # So enforcement is strict exactly where it is unambiguous: if phase 0b
        # authorised NOTHING, then any precondition at all is a domain
        # restriction invented with the goal in view, and no re-encoding
        # argument can explain it. That is the case the pilot's unsound proof
        # actually took. When phase 0b did authorise clauses, a mismatch is
        # recorded for triage but not rejected, because we cannot yet tell
        # "re-encoded" from "strengthened" without an entailment check.
        if getattr(bk, "phase0_precondition", False):
            extra = library_theory.precondition_violations(
                tr.assumed_precondition, precondition)
            if extra:
                rec["phase0_pre_violations"] = extra
            if extra and not precondition:
                attempt_log.append({"attempt": attempt,
                                    "outcome": f"unauthorised precondition: {extra}"})
                rec["reason"] = ("translation added a precondition phase 0b did not "
                                 f"authorise: {'; '.join(extra)}")
                continue

        frozen = tr.model
        rec["freeze_hash"] = tr.freeze_hash
        rec["phase_reached"] = "bounded"

        # ---- Stage 2a: bounded refutation (no LLM; Cex is sound for refutation)
        if args.phase in ("bounded", "both") and bk.supports_bounded:
            b = await asyncio.to_thread(
                bk.run, frozen, mode="bounded", unroll=args.unroll,
                timeout=args.verify_timeout)
            rec["bounded_outcome"] = b.outcome.value
            rec["bounded_exit"] = b.exit_code
            # A bounded model checker meets undefined behaviour constantly, not
            # occasionally: C's arithmetic is partial where Python's is total, so
            # a first-pass transliteration routinely admits executions the Python
            # program does not have. Discarding those entries outright would
            # measure the translation's first draft rather than the tool. The
            # model is shown the undefined behaviour and given a few rounds to
            # write C without it, under the same freeze, integrity and
            # precondition audits every other repair loop runs under.
            if getattr(bk, "two_sided", False) and b.is_malformed \
                    and args.max_proof_rounds > 0:
                rr = await ub_repair.repair_ub(
                    chat, bk, frozen_model=frozen, entry_point=entry["entry_point"],
                    result=b, theory=theory,
                    base_precondition=list(rec.get("assumed_precondition") or []),
                    max_rounds=min(3, args.max_proof_rounds), unroll=args.unroll,
                    verify_timeout=args.verify_timeout, llm_timeout=args.llm_timeout)
                b = rr.result
                rec["ub_repair_rounds"] = rr.rounds
                rec["ub_repair_rejected"] = rr.rejected
                rec["bounded_outcome"] = b.outcome.value
                rec["bounded_exit"] = b.exit_code
                if not b.is_malformed:
                    rec["model_dafny"] = rr.model
            # ---- Oracle witness: hand the checker the input, not the search.
            # Pinning happens AFTER undefined-behaviour repair so the harness
            # being pinned is the one the ordinary arm would have reported on,
            # and the two are comparable entry by entry.
            if getattr(args, "oracle_witness", False) \
                    and getattr(bk, "two_sided", False):
                rec["search_outcome"] = b.outcome.value
                rec["search_is_cex"] = b.is_cex
                rec["search_malformed"] = b.is_malformed
                pinned = await _pin_oracle_witness(
                    chat, bk, args, entry, rec,
                    model=rec.get("model_dafny") or frozen, py_source=py_source)
                if pinned is not None:
                    b = pinned
                    rec["bounded_outcome"] = b.outcome.value
                    rec["bounded_exit"] = b.exit_code
                else:
                    # This arm reports a counterexample only when it is the
                    # dataset's own killing input. Falling back to the one CBMC
                    # found for itself would silently mix the two and let the
                    # ablation take credit for a search it was meant to remove.
                    rec["prediction"] = ABSTAIN
                    rec["reason"] = "oracle: " + (rec.get("oracle_reason")
                                                  or "witness not installed")
                    rec["attempts_used"] = attempts_used
                    rec["wall_s"] = time.time() - started
                    return _finish(rec)
            if b.is_cex and b.is_malformed:
                # A partiality/well-formedness error indicts the translation, not
                # the program. Reporting Incorrect here would turn translation
                # defects into bug findings.
                rec["reason"] = ("malformed model: "
                                 + ", ".join(b.wellformedness_issues))
                rec["cex_diagnostics"] = ((b.stdout or "") + (b.stderr or ""))[:4000]
                rec["attempts_used"] = attempts_used
                rec["wall_s"] = time.time() - started
                return _finish(rec)
            if b.is_cex:
                rec["prediction"] = INCORRECT
                rec["cex_has_witness"] = b.has_witness
                rec["reason"] = (
                    ("counterexample on the pinned killing input"
                     if rec.get("oracle_installed") else
                     "counterexample in bounded phase") if b.has_witness
                    else "bounded refutation WITHOUT concrete witness (triage)")
                rec["cex_diagnostics"] = ((b.stdout or "") + (b.stderr or ""))[:4000]
                rec["attempts_used"] = attempts_used
                rec["wall_s"] = time.time() - started
                return _finish(rec)
            if args.phase == "bounded":
                if getattr(bk, "two_sided", False):
                    if not rec.get("oracle_witness"):
                        head = ("no violating execution found within the "
                                "unwinding bound")
                    elif rec.get("oracle_installed"):
                        head = ("the pinned killing input did not violate the "
                                "translated postcondition")
                        if rec.get("oracle_reachable") is False:
                            head += ("; the pinned assumptions admit no "
                                     "execution (vacuous)")
                    else:
                        head = "oracle: " + (rec.get("oracle_reason")
                                             or "witness not installed")
                    rec["reason"] = (
                        head
                        + ("; the bound did not exhaust every loop"
                           if getattr(b, "bound_insufficient", False) else "")
                        + ("; " + ", ".join(b.wellformedness_issues)
                           if b.wellformedness_issues else ""))
                    rec["bound_insufficient"] = getattr(b, "bound_insufficient", False)
                    rec["attempts_used"] = attempts_used
                    rec["wall_s"] = time.time() - started
                    return _finish(rec)
                rec["reason"] = "no counterexample within bound (bounded-only run)"
                rec["attempts_used"] = attempts_used
                rec["wall_s"] = time.time() - started
                return _finish(rec)

        # ---- Stage 2b: unbounded proof repair (annotations only; program frozen)
        rec["phase_reached"] = "unbounded"
        try:
            pr = await invariant_agent.prove(
                chat, frozen, entry["entry_point"],
                max_rounds=args.max_proof_rounds, timeout=args.llm_timeout,
                verify_budget_s=args.verify_budget,
                precondition_rescue=args.precondition_rescue,
                docstring=entry.get("docstring", ""),
                python_source=source_path.read_text(encoding="utf-8"),
                reference_block=ref_block,
                theory=theory,
                backend=bk,
            )
        except Exception as exc:
            rec["reason"] = f"proof error: {type(exc).__name__}: {exc}"
            rec["attempts_used"] = attempts_used
            rec["wall_s"] = time.time() - started
            return _finish(rec)

        attempt_log.append({"attempt": attempt, "proved": pr.proved,
                            "rounds": pr.rounds, "outcome": pr.reason})
        if pr.proved:
            break

    rec["attempts_used"] = attempts_used
    rec["attempt_log"] = attempt_log
    if tr is None or not tr.ok or pr is None:
        rec["wall_s"] = time.time() - started
        return _finish(rec)

    rec["proof_rounds"] = pr.rounds
    rec["verify_seconds"] = round(pr.verify_seconds, 1)
    rec["rejected_rounds"] = pr.rejected_rounds
    rec["rescue_used"] = pr.rescue_used
    rec["rescue_preconditions"] = pr.rescue_preconditions
    rec["rescue_assumptions"] = pr.rescue_assumptions
    rec["src_annotations"] = pr.src_annotations
    rec["abduced_definedness"] = pr.abduced_definedness
    rec["abduced_library"] = pr.abduced_library
    rec["theory_amendments"] = pr.theory_amendments
    rec["final_dafny"] = pr.model
    # Stage 2 may introduce library axioms of its own, so the trusted base of the
    # artifact that was actually verified is NOT the one recorded at Stage 1.
    # Auditing the Stage-1 base alone under-reports what a proof rests on.
    try:
        final_integ = bk.integrity_check(pr.model, entry["entry_point"])
        rec["trusted_base_final"] = list(final_integ.trusted_base)
        rec["assumed_precondition_final"] = list(final_integ.assumed_precondition)
        rec["trusted_base_added_in_proof"] = [
            a for a in final_integ.trusted_base if a not in set(tr.trusted_base)]
        # Repair is the highest-pressure phase: it sees the failing verifier
        # output. Same asymmetry as above -- reject only when phase 0b
        # authorised nothing, where no re-encoding argument can apply.
        if getattr(bk, "phase0_precondition", False):
            extra = library_theory.precondition_violations(
                final_integ.assumed_precondition, precondition)
            if extra:
                rec["phase0_pre_violations_final"] = extra
                if pr.proved and not precondition:
                    pr.proved = False
                    rec["phase0_pre_downgraded"] = True
                    rec["reason"] = ("proof rested on a precondition phase 0b did "
                                     f"not authorise: {'; '.join(extra)}")
    except Exception:
        pass
    if pr.proved:
        rec["prediction"] = CORRECT
        rec["reason"] = ("verified (unbounded proof, precondition rescue)"
                         if pr.rescue_used else "verified (unbounded proof)")
    else:
        # Failure to find a proof is NOT evidence of a bug.
        if not rec.get("phase0_pre_downgraded"):
            rec["reason"] = "no proof found within round budget"
        if getattr(bk, "two_sided", False):
            # Unreachable: a two-sided backend runs bounded-only and has already
            # returned above. Asserted rather than assumed, because reaching an
            # unbounded call with a bounded model checker would silently report
            # a proof the tool never established.
            raise AssertionError(
                f"{bk.name} is a bounded model checker and must be run with "
                f"--phase bounded")
    rec["wall_s"] = time.time() - started
    return _finish(rec)


async def run_entry_refute(
    chat: CopilotChat,
    entry: dict,
    args: argparse.Namespace,
) -> dict:
    """Formal refutation: prove the program WRONG, unboundedly.

    Phases 0 and 0b are the ones `run_entry` uses, unchanged, and translation
    runs with the same prompt. Only afterwards does this diverge: the entry's
    postcondition is mechanically negated, an agent proposes an input on which
    it believes the program fails, that input is installed as a precondition,
    and the ordinary proof-repair loop is pointed at the negated goal.

    A proof of `!P` under witness `W` says every input satisfying `W` violates
    the postcondition. That is only a counterexample if some such input exists
    AND reaches a return, so the proof is gated on `nfv.agent.refute.reachable`
    -- see the module docstring for why the probe keeps the method body.
    """
    entry_id = entry["entry_id"]
    bk = _resolve_backend(args)
    # No abductions, per the design: on this goal a goal-visible addition to the
    # trusted base does not lose a proof, it invents a bug report.
    bk = replace(bk, abduce_definedness=False)
    started = time.time()
    _usage = new_usage_scope()
    rec: dict = {
        "entry_id": entry_id,
        "task_id": entry.get("task_id"),
        "entry_point": entry.get("entry_point"),
        "label": entry.get("label"),
        "backend": args.backend,
        "model": args.model,
        "mode": "refute",
        "prediction": ABSTAIN,
        "reason": "",
        "phase_reached": "translation",
    }
    source_path = WORK / entry_id / "source.py"

    def _finish(r: dict) -> dict:
        r["nano_aiu"] = _usage.nano_aiu
        r["api_calls"] = _usage.api_calls
        r["input_tokens"] = _usage.input_tokens
        r["output_tokens"] = _usage.output_tokens
        r["transport_retries"] = _usage.transport_retries
        r["wall_s"] = time.time() - started
        return r

    if not source_path.exists():
        rec["reason"] = "missing materialized source (run python -m paper.prepare)"
        return _finish(rec)

    gated = _vacuity_gate(entry, args, rec)
    if gated is not None:
        return _finish(gated)

    py_source = source_path.read_text(encoding="utf-8")
    entry_point = entry["entry_point"]

    # ---- Phases 0 / 0b: identical to the proof pipeline.
    ref_block = pydocs.reference_block(py_source) if bk.python_reference else ""
    theory = ""
    if bk.library_theory:
        th = await library_theory.synthesize(chat, py_source, bk,
                                             timeout=args.llm_timeout)
        rec["theory_ok"] = th.ok
        rec["theory_chars"] = len(th.text)
        if th.ok:
            theory = th.text
    precondition: list[str] | None = None
    if getattr(bk, "phase0_precondition", False):
        pre = await library_theory.synthesize_precondition(
            chat, py_source, entry.get("docstring", ""), bk,
            timeout=args.llm_timeout)
        rec["phase0_pre_ok"] = pre.ok
        rec["phase0_precondition"] = pre.items
        precondition = pre.items if pre.ok else []

    # ---- Phase 1: translate (unchanged prompt), then invert the goal.
    # Translation is retried because provability is a property of the sampled
    # encoding; the WITNESS loop below is separate and much cheaper.
    tr = None
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            tr = await translate.translate(
                chat, entry, source_path, backend=bk,
                max_rounds=args.max_syntax_rounds, timeout=args.llm_timeout,
                reference_block=ref_block, theory=theory,
                precondition=precondition,
            )
        except Exception as exc:
            rec["reason"] = f"translation error: {type(exc).__name__}: {exc}"
            return _finish(rec)
        rec["translation_rounds"] = tr.rounds
        rec["trusted_base"] = tr.trusted_base
        # Record the artifact even when translation FAILED. The verify path does
        # this (see run_entry), which is what made it possible to show that most
        # of its audit failures were the model declining to write Lean at all
        # rather than the audit being strict. Recording it only on success threw
        # that evidence away for every refuted entry, so a failure here could not
        # be told apart from a refusal after the fact.
        rec["model_dafny"] = tr.model
        if tr.ok:
            break
        rec["reason"] = f"translation: {tr.reason}"
    if tr is None or not tr.ok:
        return _finish(rec)

    rec["freeze_hash"] = tr.freeze_hash
    rec["model_dafny"] = tr.model
    inv = refute.invert_entry_spec(tr.model, entry_point, bk)
    if not inv.ok:
        rec["reason"] = f"could not invert the goal: {inv.reason}"
        return _finish(rec)
    rec["original_ensures"] = inv.original_ensures
    rec["inverted_goal"] = inv.inverted
    rec["phase_reached"] = "inverted"

    # ---- Phase 2: guess a violating input, prove `!P` on it, then gate.
    history: list[str] = []
    attempts_log: list[dict] = []
    oracle = getattr(args, "oracle_witness", False)
    kt = entry.get("killing_test")
    if oracle and kt is None:
        rec["reason"] = "oracle mode: entry has no killing test"
        return _finish(rec)
    # With an oracle there is exactly ONE witness, so the budget cannot be spent
    # on alternatives. Retries exist only to re-render a witness that failed to
    # type-check, which is a defect in the encoding rather than a new guess.
    n_att = max(1, args.max_refutation_attempts)
    rec["oracle_witness"] = bool(oracle)
    if oracle:
        rec["killing_test"] = kt
    for att in range(1, n_att + 1):
        if oracle:
            g = await oracle_witness.render(
                chat, killing_test=kt, python_source=py_source,
                postcondition=entry["postcondition"], entry_point=entry_point,
                model=inv.model, backend=bk, timeout=args.llm_timeout)
        else:
            g = await refute.guess_input(
                chat, python_source=py_source, docstring=entry.get("docstring", ""),
                postcondition=entry["postcondition"], entry_point=entry_point,
                model=inv.model, backend=bk, history=history,
                timeout=args.llm_timeout)
        if not g.ok:
            history.append(f"(no witness produced: {g.reason})")
            attempts_log.append({"attempt": att, "outcome": f"no witness: {g.reason}"})
            continue
        witness = g.clauses
        candidate = refute.install_precondition(inv.model, entry_point, witness, bk)
        tc = await asyncio.to_thread(bk.type_check, candidate)
        if tc.outcome is not dafny.Outcome.VERIFIED:
            why = "witness does not type-check"
            history.append(f"`{'; '.join(witness)}` -- {why}")
            attempts_log.append({"attempt": att, "witness": witness, "outcome": why})
            continue

        rec["phase_reached"] = "refuting"
        try:
            pr = await invariant_agent.prove(
                chat, candidate, entry_point,
                max_rounds=args.max_proof_rounds, timeout=args.llm_timeout,
                verify_budget_s=args.verify_budget, precondition_rescue=False,
                docstring=entry.get("docstring", ""), python_source=py_source,
                reference_block=ref_block, theory=theory, backend=bk,
            )
        except Exception as exc:
            why = f"proof error: {type(exc).__name__}: {exc}"
            history.append(f"`{'; '.join(witness)}` -- {why}")
            attempts_log.append({"attempt": att, "witness": witness, "outcome": why})
            continue

        if not pr.proved:
            why = ("the negated goal could not be proved on this domain, so the "
                   "postcondition may well hold there")
            history.append(f"`{'; '.join(witness)}` -- {why}")
            attempts_log.append({"attempt": att, "witness": witness,
                                 "rounds": pr.rounds, "outcome": "not proved"})
            if oracle:
                # There is no other witness to try: this input is known to break
                # the program, so failing here is a failure of translation or
                # proof, and retrying would only re-render the same input.
                rec["reason"] = ("oracle witness did not yield a proof of the "
                                 "negated goal")
                break
            continue

        # G1 reachability, G2 inversion, G3 integrity. All three must pass; the
        # verdict is never taken from the proof alone.
        reach, reach_why = await asyncio.to_thread(
            refute.reachable, pr.model, entry_point, bk, args.verify_timeout)
        ens = refute.entry_ensures(pr.model, entry_point, bk)
        g2 = (len(ens) == 1 and ens[0] == inv.inverted)
        integ = bk.integrity_check(pr.model, entry_point)
        gates = {"reachable": reach, "reachable_why": reach_why,
                 "inversion_intact": g2, "entry_ensures": ens,
                 "integrity_ok": integ.ok, "integrity": list(integ.violations)}
        attempts_log.append({"attempt": att, "witness": witness,
                             "rounds": pr.rounds, "outcome": "proved", "gates": gates})
        rec["gates"] = gates
        rec["witness"] = witness
        rec["witness_rationale"] = g.rationale
        # A witness that mentions anything but the input parameters is asserting
        # something about the ENVIRONMENT, not merely choosing an input, and a
        # false assertion there manufactures a bug report. Recorded, not
        # rejected, but it disqualifies the refutation from being called clean.
        foreign = refute.witness_foreign_symbols(
            witness, refute.entry_param_names(pr.model, entry_point, bk),
            refute.datatype_constructors(pr.model))
        rec["entry_params"] = refute.entry_param_names(pr.model, entry_point, bk)
        rec["witness_foreign_symbols"] = foreign
        rec["proof_rounds"] = pr.rounds
        rec["final_dafny"] = pr.model
        rec["trusted_base_final"] = list(integ.trusted_base)
        # F62: Dafny is the evaluated backend precisely because it proves
        # termination by default, so a proved negation describes an execution
        # that really returns. `decreases *` waives exactly that, reducing the
        # refutation to the partial-correctness claim we declined to evaluate
        # under Boogie. G1 does not catch it: it only rejects an infeasible
        # exit, and a semantically non-terminating loop with a satisfiable
        # abstracted exit passes the gate. Recorded and excluded from clean.
        nonterm = "decreases *" in (pr.model or "")
        rec["witness_nontermination_risk"] = nonterm

        if reach is not True:
            history.append(f"`{'; '.join(witness)}` -- the negated goal was PROVED "
                           f"but the domain is vacuous or unreachable, so it "
                           f"establishes nothing ({reach_why})")
            rec["reason"] = f"refutation rejected by reachability gate: {reach_why}"
            continue
        if not g2:
            rec["reason"] = ("refutation rejected: the goal is no longer the "
                             f"negation this pipeline wrote ({ens})")
            continue
        if not integ.ok:
            rec["reason"] = ("refutation rejected by the integrity audit: "
                             + "; ".join(integ.violations))
            continue

        rec["prediction"] = INCORRECT
        rec["refutation_clean"] = (
            not integ.trusted_base and not foreign and not nonterm)
        why_unclean = []
        if nonterm:
            why_unclean.append("`decreases *` waives the termination proof")
        if integ.trusted_base:
            why_unclean.append(f"{len(integ.trusted_base)} trusted decl(s)")
        if foreign:
            why_unclean.append("witness constrains " + ", ".join(foreign))
        rec["reason"] = ("verified counterexample (unbounded refutation"
                         + ("" if not why_unclean else "; " + "; ".join(why_unclean))
                         + ")")
        rec["attempts_used"] = att
        rec["attempt_log"] = attempts_log
        return _finish(rec)

    rec["attempts_used"] = n_att
    rec["attempt_log"] = attempts_log
    if not rec["reason"]:
        rec["reason"] = "no verified counterexample within the attempt budget"
    return _finish(rec)


def append_experiment(args: argparse.Namespace, m, log_path: Path, usage=None,
                      account=None) -> None:
    """Append a run entry to EXPERIMENTS.md.

    Append-only and automatic: a log that must be updated by hand silently stops
    matching reality. Entries are never rewritten, so a superseded result stays
    visible next to the one that replaced it.
    """
    import subprocess as sp

    try:
        commit = sp.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                        text=True, cwd=ROOT.parent).stdout.strip() or "unknown"
        dirty = bool(sp.run(["git", "status", "--porcelain", "--untracked-files=no"],
                            capture_output=True, text=True, cwd=ROOT.parent).stdout.strip())
    except Exception:
        commit, dirty = "unknown", False

    stamp = time.strftime("%Y-%m-%d %H:%M")
    # Reconstruct from every flag that differs from its parser default, rather
    # than from a hand-maintained list. The hand-maintained version silently
    # omitted --attempts and --max-proof-rounds, which are exactly the flags
    # that define the proof budget, so a logged command re-run verbatim would
    # have used the defaults (1 attempt, 5 rounds) and produced different
    # numbers under the same heading.
    defaults = getattr(args, "_defaults", None) or {}
    skip = {"_defaults", "_resolved_backend", "out", "backend", "model", "no_log", "report_only"}
    flags = ""
    for key in sorted(vars(args)):
        if key in skip:
            continue
        val = getattr(args, key)
        if key in defaults and val == defaults[key]:
            continue
        opt = "--" + key.replace("_", "-")
        if isinstance(val, bool):
            flags += f" {opt}" if val else ""
        elif val not in (None, ""):
            flags += f" {opt} {val}"
    cmd = (f"python -m nfv.run --backend {args.backend} --model {args.model}"
           f"{flags} --out {args.out}")

    block = [
        "",
        f"## Run {stamp} — {args.backend} / {args.model}"
        + ("  (DIRTY WORKING TREE)" if dirty else "")
        + ("  (EMU ACCOUNT)" if getattr(account, "is_emu", False) else ""),
        "",
        f"- commit: `{commit}`{' + uncommitted changes' if dirty else ''}, "
        f"backend: {args.backend}, model: {args.model}, "
        f"unroll: {args.unroll}, phase: {args.phase}",
        f"- account: {account.one_line() if account is not None else 'unknown'}",
        f"- command: `{cmd}`",
        f"- results: `{args.out}`",
        "",
        "```",
        render(m, title=Path(args.out).stem, phase=args.phase),
        "```",
        "",
    ]
    if usage is not None and usage.api_calls:
        block += [
            "LLM cost (read from the Copilot API at call time, so it is not subject",
            "to the billing page's reporting lag):",
            "",
            "```",
            usage.render(),
            "```",
            "",
        ]
    if m.soundness_violations:
        block += [
            f"Soundness violations: {m.soundness_violations} — REQUIRE TRIAGE "
            "(mistranslation | bad library spec | unsound bounded reasoning | wrong label)",
            "",
        ]
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(block))
_TYPE_ONLY = re.compile(r"assert\s+isinstance\s*\([^)]*\)\s*$")


def is_trivial(entry: dict) -> bool:
    """True if the postcondition is a pure type assertion.

    `assert isinstance(return_value, bool)` is guaranteed by Dafny's type system,
    so it becomes a tautology once translated and is satisfied identically by a
    correct program and its mutant. Such entries cannot discriminate no matter how
    good the verifier is, so including them only consumes proof budget. 26 of the
    232 entries (13 correct / 13 incorrect) are of this form.
    """
    body = "\n".join(l.strip() for l in entry["postcondition"].splitlines()
                     if l.strip().startswith("assert"))
    return bool(_TYPE_ONLY.fullmatch(body.strip()))


def _usage_from_records(recs: list[dict]):
    """Rebuild run-level usage totals from per-entry records.

    Lets an interrupted run still report what it spent, and lets any stored
    result file be re-costed without re-running it.
    """
    from nfv.shared.copilot_client import UsageTotals
    if not any(r.get("api_calls") for r in recs):
        return None
    u = UsageTotals()
    for r in recs:
        u.api_calls += r.get("api_calls", 0) or 0
        u.input_tokens += r.get("input_tokens", 0) or 0
        u.output_tokens += r.get("output_tokens", 0) or 0
        u.nano_aiu += r.get("nano_aiu", 0) or 0
        u.transport_retries += r.get("transport_retries", 0) or 0
    return u


def _apply_overrides(bk, args):
    """Apply the two provenance flags on top of the arm's defaults.

    The defaults are now `phase 0b on, abduction off`: the entry precondition is
    authored once, blind, from the source and docstring, and frozen. Both flags
    exist so the pre-change behaviour stays reachable -- `--abduce --no-phase0b`
    reproduces the old arm exactly, which is what makes the published a3 results
    re-runnable rather than merely archived.

    Returns the backend unchanged when neither flag is given, so a default run
    is byte-identical to selecting the arm directly.
    """
    import dataclasses

    changes = {}
    if args.abduce and not bk.abduce_definedness:
        changes["abduce_definedness"] = True
    if args.no_phase0b and bk.phase0_precondition:
        changes["phase0_precondition"] = False
    if getattr(args, "no_theory", False) and bk.library_theory:
        changes["library_theory"] = False
    if not changes:
        return bk
    # Both on at once is a real configuration, but not a coherent one: phase 0b
    # freezes the precondition precisely so that no goal-visible phase may
    # restrict the domain, and abduction is a goal-visible phase that adds
    # preconditions. Warn rather than refuse -- measuring the combination is a
    # legitimate thing to want.
    if changes.get("abduce_definedness") and bk.phase0_precondition:
        print("warning: --abduce with phase 0b still ON. The blind precondition is "
              "frozen, so abduction can only ADD to it; the arm no longer isolates "
              "who may restrict the domain. Add --no-phase0b for the old arm.")
    print("config override: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    return dataclasses.replace(bk, **changes)


def _resolve_backend(args: argparse.Namespace) -> "Backend":
    """The backend an entry actually runs under, overrides included.

    `main_async` used to apply the overrides to its own local copy while every
    entry re-fetched the pristine backend from the registry, so `--abduce`,
    `--no-phase0b` and `--no-theory` were accepted, echoed, logged -- and had no
    effect on a single entry. No published arm used those flags, so no recorded
    result is affected, but an ablation driven by them would silently have
    measured the baseline against itself.

    Resolving once and caching on `args` keeps the override in one place and
    guarantees every entry of a run sees the same configuration.
    """
    bk = getattr(args, "_resolved_backend", None)
    if bk is None:
        bk = _apply_overrides(get_backend(args.backend), args)
        args._resolved_backend = bk
    return bk


async def main_async(args: argparse.Namespace) -> int:
    bk = _resolve_backend(args)
    if bk.deprecated:
        print(f"warning: backend `{bk.name}` is DEPRECATED -- {bk.deprecated}.\n"
              f"         it still runs unchanged so published results stay reproducible.")
    if args.phase in ("bounded", "both") and not bk.supports_bounded:
        # Silently degrading to unbounded would mislabel the run in EXPERIMENTS.md
        # and invite a comparison against bounded arms that never happened.
        print(f"backend `{bk.name}` has no bounded mode (there is no program to "
              f"unroll); use --phase unbounded")
        return 2
    if getattr(bk, "two_sided", False) and args.phase != "bounded":
        # The mirror image of the check above. A bounded model checker has no
        # unbounded mode, and inventing one by raising the unwinding bound until
        # it stops objecting would report a proof the tool never established.
        print(f"backend `{bk.name}` is a bounded model checker: it refutes by "
              f"exhibiting an execution and proves nothing. Use --phase bounded")
        return 2
    if args.report_only:
        # An interrupted run never reaches the auto-log step, so its results sit
        # in the store unreported. This recovers the stats from the store alone.
        store = Store(args.out)
        recs = store.records()
        if not recs:
            print(f"no records in {args.out}")
            return 1
        m = compute(recs)
        print(render(m, title=Path(args.out).stem, phase=args.phase))
        usage = _usage_from_records(recs)
        if usage is not None:
            print("\n--- LLM usage ---")
            print(usage.render())
        if not args.no_log:
            log_path = ROOT / "EXPERIMENTS.md"
            # --report-only makes no LLM calls, so resolve the identity directly.
            append_experiment(args, m, log_path, usage=usage,
                              account=detect_account())
            print(f"\nlogged to {log_path}")
        return 0

    payload = json.loads(Path(args.data).read_text(encoding="utf-8"))
    entries = payload["entries"] if isinstance(payload, dict) else payload
    subset_meta = None
    if args.subset:
        from paper.data.build_subset import load_subset
        subset_meta = load_subset(args.subset)
        keep = set(subset_meta["task_ids"])
        before = len(entries)
        entries = [e for e in entries if e["task_id"] in keep]
        print(f"subset {args.subset!r}: kept {len(entries)}/{before} entries "
              f"({subset_meta['n_pairs_kept']} pairs, "
              f"dropped {subset_meta['n_pairs_dropped']})")
        print(f"  derived from {', '.join(subset_meta['derived_from'])}")
    if args.label:
        entries = [e for e in entries if e.get("label") == args.label]
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        entries = [e for e in entries
                   if any(k == e.get("task_id") or k in e["entry_id"] for k in keys)]
    if args.exclude_trivial:
        before = len(entries)
        entries = [e for e in entries if not is_trivial(e)]
        print(f"excluded {before - len(entries)} trivial (type-assertion) entries")
    if args.oracle_witness:
        # The oracle is only defined for mutants: a correct entry has no killing
        # test. Restricting here rather than asking the user to remember
        # --label keeps the arm impossible to misconfigure.
        before = len(entries)
        entries = [e for e in entries if e.get("label") == "incorrect"]
        from paper.data import killing_tests
        entries, missing = killing_tests.attach(entries)
        print(f"oracle witness: {len(entries)}/{before} mutants, "
              f"{len(missing)} without a killing test")
        if missing:
            print("  WARNING dropping: " + ", ".join(missing[:5]))
            entries = [e for e in entries if "killing_test" in e]
    if args.sample:
        import random
        rng = random.Random(args.seed)
        entries = sorted(entries, key=lambda e: e["entry_id"])
        entries = rng.sample(entries, min(args.sample, len(entries)))
    if args.limit:
        entries = entries[: args.limit]

    store = Store(args.out)
    done = store.done_ids() if args.resume else set()
    todo = [e for e in entries if e["entry_id"] not in done]

    print(f"backend={args.backend} model={args.model} phase={args.phase}")
    print(f"entries={len(entries)} todo={len(todo)} skipped={len(entries) - len(todo)}")
    print(f"out={args.out}\n")
    usage = None
    account = None
    if not todo:
        print("nothing to do")
        account = detect_account()
    else:
        async with CopilotChat(model=args.model, concurrency=args.concurrency,
                               allow_emu=args.allow_emu) as chat:
            completed = 0

            async def worker(entry: dict) -> None:
                nonlocal completed
                runner = run_entry_refute if args.phase == "refute" else run_entry
                rec = await runner(chat, entry, args)
                store.append(rec)
                completed += 1
                print(f"[{completed}/{len(todo)}] {rec['entry_id']:<28} "
                      f"label={rec['label']:<9} pred={rec['prediction']:<9} "
                      f"phase={rec['phase_reached']:<11} {rec['reason'][:60]}",
                      flush=True)

            await asyncio.gather(*(worker(e) for e in todo))
            usage = chat.usage
            account = chat.account

    print()
    m = compute(store.records())
    print(render(m, title=Path(args.out).stem, phase=args.phase))
    if usage is not None:
        print("\n--- LLM usage (read from the API, not the billing page) ---")
        print(usage.render())
    if not args.no_log:
        log_path = ROOT / "EXPERIMENTS.md"
        append_experiment(args, m, log_path, usage=usage, account=account)
        print(f"\nlogged to {log_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Neuro-formal verification pipeline")
    # Sourced from the registry rather than hardcoded: a hand-maintained list
    # here silently drifts from BACKENDS every time an arm is added.
    ap.add_argument("--backend", choices=sorted(_BACKENDS),
                    default="dafny")
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--label", choices=["correct", "incorrect"], default=None,
                    help="restrict to entries with this label")
    ap.add_argument("--exclude-trivial", action="store_true",
                    help="drop entries whose postcondition is only a type assertion")
    ap.add_argument("--sample", type=int, default=0,
                    help="randomly sample N entries (reproducible via --seed)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--only", default="",
                    help="comma-separated task_id or entry_id substrings to run")
    ap.add_argument("--subset", default="",
                    help="named subset from paper/data/subsets/ (e.g. `core63`). "
                         "Keeps only that subset's task_ids, always as complete "
                         "correct/incorrect pairs. The subset name and provenance "
                         "are printed and recorded in the EXPERIMENTS.md run log.")
    ap.add_argument("--phase",
                    choices=["bounded", "unbounded", "both", "refute"],
                    default="both",
                    help="`refute` runs the formal-refutation pipeline instead: "
                         "translate as usual, mechanically negate the entry's "
                         "postcondition, have an agent propose a violating input "
                         "as a precondition, and try to PROVE the negated goal. "
                         "A proof counts as a counterexample only after the "
                         "reachability, inversion and integrity gates pass.")
    ap.add_argument("--vacuity-gate", action="store_true",
                    help="abstain immediately when the Python postcondition has a "
                         "conjunct that is only a type test (isinstance and "
                         "friends). Such a conjunct is absorbed by the target's "
                         "type system rather than translated, so the surviving "
                         "specification is weaker than the one measured against. "
                         "Costs nothing: it reads the postcondition, not the model.")
    ap.add_argument("--oracle-witness", action="store_true",
                    help="refutation ablation: instead of having the model SEARCH "
                         "for a violating input, hand it the dataset's killing "
                         "test -- the input on which the mutant is known to fail "
                         "-- and ask only that it be encoded as a precondition. "
                         "Separates witness search from proof: a failure here is "
                         "attributable to translation or proof, not to search. "
                         "Restricted to the 103 mutants, since a correct entry "
                         "has no killing test. With --phase refute this pins the "
                         "input as `requires` clauses on the inverted goal; with "
                         "--phase bounded on a model checker it installs the "
                         "input as `__CPROVER_assume` above the goal assertion, "
                         "which replaces CBMC's search over the bounded input "
                         "domain with the one input that is known to break the "
                         "program.")
    ap.add_argument("--max-refutation-attempts", type=int, default=5,
                    help="with --phase refute: how many different witness inputs "
                         "to try. Each attempt is told what the previous ones "
                         "tried and why they failed.")
    ap.add_argument("--unroll", type=int, default=10)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-syntax-rounds", type=int, default=3)
    ap.add_argument("--max-proof-rounds", type=int, default=5)
    ap.add_argument("--attempts", type=int, default=1,
                    help="retry the WHOLE pipeline (translate -> verify -> repair -> "
                         "abduce) up to N times, stopping at the first proof. Provability "
                         "is a property of the sampled translation, so a retry redraws it. "
                         "N>1 makes the arm a pass@N estimator: report it as such, and note "
                         "that it resamples soundness violations too.")
    ap.add_argument("--precondition-rescue", action="store_true",
                    help="DEPRECATED -- do not use. Measured negative result: "
                         "after the proof rounds fail, allow ONE more round in which "
                         "the entry precondition may be strengthened if the agent "
                         "ties it to the docstring. Across the corpus this fired 5 "
                         "times: 3 in the dafny arm (all sound) and 2 in the "
                         "dafny-total arm (BOTH unsound -- mutants proved correct). "
                         "Same prompt, opposite sign, so the dafny arm's clean "
                         "result was luck at n=3. This is the F10 unsoundness "
                         "mechanism: the domain is chosen while looking at a "
                         "failing proof. Proofs obtained this way are flagged "
                         "`rescue_used` so they can be reverted. See F16.")
    ap.add_argument("--abduce", action="store_true",
                    help="re-enable the goal-visible definedness ABDUCTION round, "
                         "which is OFF by default. Default behaviour is that the "
                         "entry precondition is authored once in phase 0b -- blind, "
                         "from the source and docstring, before the postcondition "
                         "is visible -- and then frozen. Abduction runs after a "
                         "failed proof, so it chooses the input domain while "
                         "looking at the goal: on HumanEval/106 it added "
                         "`requires 1 <= n`, which is satisfiable (so the vacuity "
                         "gate passes it) and excludes the single input n=0 on "
                         "which that mutant is wrong. Use with --no-phase0b to "
                         "reproduce the pre-change arms.")
    ap.add_argument("--no-phase0b", action="store_true",
                    help="switch OFF the blind phase-0b precondition, which is ON "
                         "by default. With this flag the precondition is written "
                         "by translation instead, which sees the postcondition.")
    ap.add_argument("--no-theory", action="store_true",
                    help="switch OFF the blind phase-0 library theory, which is ON "
                         "by default in the -theory arms. With this flag the "
                         "library surface is not axiomatised before translation; "
                         "whatever the program needs about `sum`, `sorted` or "
                         "`len` must then be supplied by translation or, with "
                         "--abduce, inferred during proof repair with the goal "
                         "already visible. Together with --no-phase0b this is the "
                         "ablation of the two blind phases.")
    ap.add_argument("--verify-budget", type=float, default=60.0,
                    help="seconds of Dafny verification time per PROOF ATTEMPT in "
                         "Stage 2 (prove() runs once per translation attempt, so "
                         "the per-entry ceiling is this times --attempts)")
    ap.add_argument("--stop-after-repeats", type=int, default=0, metavar="N",
                    help="stop redrawing an entry once N consecutive attempts fail "
                         "with an identical outcome (0 = off, the default). F83: "
                         "39%% of multi-attempt entries fail identically every "
                         "time, so those redraws only cost wall time. Cannot "
                         "affect soundness -- it only skips attempts.")
    ap.add_argument("--entry-deadline", type=float, default=0.0, metavar="SECONDS",
                    help="stop starting new attempts for an entry after this much "
                         "wall time (0 = off, the default). Backstop for runs "
                         "whose tail is dominated by entries that never prove; "
                         "checked between attempts, so it never kills a verifier.")
    ap.add_argument("--llm-timeout", type=float, default=600.0)
    ap.add_argument("--verify-timeout", type=float, default=120.0)
    ap.add_argument("--no-log", action="store_true",
                    help="do not append this run to nfv/EXPERIMENTS.md")
    ap.add_argument("--allow-emu", action="store_true",
                    help="permit running on an Enterprise Managed User account "
                         "(refused by default; EMU policy and model availability "
                         "differ, so results are not comparable with non-EMU runs)")
    ap.add_argument("--report-only", action="store_true",
                    help="recompute stats from an existing --out file and log them, "
                         "without running anything (use after an interrupted run)")
    args = ap.parse_args()
    # Captured so the EXPERIMENTS.md command can be rebuilt from whatever
    # actually differs from a default, without a second list to keep in sync.
    args._defaults = {k: ap.get_default(k) for k in vars(args)}

    if args.out is None:
        args.out = str(ROOT / "results" / f"{args.backend}_{args.model}.jsonl")

    try:
        return asyncio.run(main_async(args))
    except AccountError as exc:
        print(f"\nERROR: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
