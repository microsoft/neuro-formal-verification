#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Metrics for the neuro-formal pipeline.

Two rules drive the reporting, both from the plan:

1. ABSTAIN IS A FIRST-CLASS OUTCOME. It gets its own column, and accuracy is
   reported twice -- coverage-conditioned (abstains excluded) and overall
   (abstains counted as errors). Reporting only the former would let a pipeline
   look excellent by answering three questions.

2. SOUNDNESS VIOLATIONS ARE THE SCIENTIFIC CORE, so they are counted separately
   rather than folded into FP/FN. A Cex on a `correct` entry may be a genuine
   refutation of a wrong label (labels are test-validated, not proved), so these
   demand human triage.

Positive class = Incorrect (bug detection).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

INCORRECT = "incorrect"
CORRECT = "correct"
ABSTAIN = "abstain"


@dataclass
class Metrics:
    total: int = 0
    tp: int = 0            # predicted Incorrect, labelled incorrect
    fp: int = 0            # predicted Incorrect, labelled correct   (soundness violation)
    tn: int = 0            # predicted Correct,   labelled correct
    fn: int = 0            # predicted Correct,   labelled incorrect (soundness violation)
    abstain_incorrect: int = 0
    abstain_correct: int = 0
    abstain_reasons: dict[str, int] = field(default_factory=dict)
    # Pair test. Each task appears as a reference and a mutant differing only in
    # the bug, so a Cex that fires on BOTH is explained by something they share
    # (spec or translation), not by the mutation. Uses no labels beyond the
    # pairing, and is the cheapest available check on translation fidelity.
    pair_discriminating: int = 0   # mutant refuted, reference not -> genuine
    pair_both_refuted: int = 0     # shared cause; the bug is incidental
    pair_inverted: int = 0         # reference refuted, mutant not
    pair_missed: int = 0           # neither refuted
    # Precondition rescue (--precondition-rescue). A rescued proof rests on a
    # domain restriction the agent chose while looking at a failing proof, so it
    # is counted here as well as in tp/fp/tn/fn and never reported as a clean
    # proof. `rescue_unsound` is the number that proved a program labelled
    # incorrect: since a reference and its mutant share a docstring by
    # construction, any precondition that saves a mutant is not a specification
    # of the function -- it is the bug being defined away.
    rescue_proved: int = 0
    rescue_sound: int = 0
    rescue_unsound: int = 0

    @property
    def pairs(self) -> int:
        return (self.pair_discriminating + self.pair_both_refuted
                + self.pair_inverted + self.pair_missed)

    @property
    def abstains(self) -> int:
        return self.abstain_incorrect + self.abstain_correct

    @property
    def covered(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def coverage(self) -> float:
        return self.covered / self.total if self.total else 0.0

    @property
    def accuracy_covered(self) -> float:
        return (self.tp + self.tn) / self.covered if self.covered else 0.0

    @property
    def accuracy_overall(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn + self.abstain_incorrect
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def soundness_violations(self) -> int:
        return self.fp + self.fn


def compute(records: list[dict]) -> Metrics:
    m = Metrics(total=len(records))
    for rec in records:
        label = (rec.get("label") or "").lower()
        pred = (rec.get("prediction") or ABSTAIN).lower()
        if pred == ABSTAIN:
            reason = rec.get("reason") or "unknown"
            m.abstain_reasons[reason] = m.abstain_reasons.get(reason, 0) + 1
            if label == INCORRECT:
                m.abstain_incorrect += 1
            else:
                m.abstain_correct += 1
        elif pred == INCORRECT:
            if label == INCORRECT:
                m.tp += 1
            else:
                m.fp += 1
        elif pred == CORRECT:
            if label == CORRECT:
                m.tn += 1
            else:
                m.fn += 1
            if rec.get("rescue_used"):
                m.rescue_proved += 1
                if label == CORRECT:
                    m.rescue_sound += 1
                else:
                    m.rescue_unsound += 1

    by_task: dict[str, dict[str, str]] = {}
    for rec in records:
        tid = rec.get("task_id")
        label = (rec.get("label") or "").lower()
        if tid and label:
            by_task.setdefault(tid, {})[label] = (rec.get("prediction") or ABSTAIN).lower()
    for preds in by_task.values():
        if CORRECT not in preds or INCORRECT not in preds:
            continue  # incomplete pair: the test needs both halves
        ref_refuted = preds[CORRECT] == INCORRECT
        mut_refuted = preds[INCORRECT] == INCORRECT
        if mut_refuted and not ref_refuted:
            m.pair_discriminating += 1
        elif mut_refuted and ref_refuted:
            m.pair_both_refuted += 1
        elif ref_refuted:
            m.pair_inverted += 1
        else:
            m.pair_missed += 1
    return m


def render(m: Metrics, title: str = "Results", phase: str = "both") -> str:
    lines = [
        f"== {title} ==",
        f"  entries              : {m.total}",
        "",
        "  confusion matrix (positive class = Incorrect)",
        f"    {'':<22}{'pred Incorrect':>16}{'pred Correct':>14}{'abstain':>10}",
        f"    {'label incorrect':<22}{m.tp:>16}{m.fn:>14}{m.abstain_incorrect:>10}",
        f"    {'label correct':<22}{m.fp:>16}{m.tn:>14}{m.abstain_correct:>10}",
        "",
        f"  coverage             : {m.coverage:6.1%}  ({m.covered}/{m.total})",
        f"  accuracy (covered)   : {m.accuracy_covered:6.1%}",
        f"  accuracy (overall)   : {m.accuracy_overall:6.1%}  (abstain = error)",
    ]

    if phase == "unbounded":
        # Refutation is disabled, so `Incorrect` is unreachable by construction and
        # precision/recall/F1 for it would report 0 as if the pipeline had failed
        # at a task it was never asked to do. The meaningful questions in this mode
        # are how often a proof is found, and whether a mutant ever gets proved.
        n_correct = m.tn + m.fp + m.abstain_correct
        n_incorrect = m.tp + m.fn + m.abstain_incorrect
        proof_rate = m.tn / n_correct if n_correct else 0.0
        lines += [
            "",
            "  UNBOUNDED-ONLY MODE - refutation disabled, `Incorrect` unreachable.",
            "  This is a soundness test: proving a mutant is a violation.",
            f"    proof rate on correct entries : {proof_rate:6.1%}  ({m.tn}/{n_correct})",
            f"    proved despite incorrect label: {m.fn}  of {n_incorrect}   <- must be 0",
        ]
    else:
        lines += [
            f"  precision (Incorrect): {m.precision:6.1%}",
            f"  recall    (Incorrect): {m.recall:6.1%}",
            f"  F1        (Incorrect): {m.f1:6.1%}",
        ]

    lines += [
        "",
        f"  SOUNDNESS VIOLATIONS : {m.soundness_violations}   <- require human triage",
        f"    Cex on correct entry     (FP): {m.fp}",
        f"    Verified on incorrect    (FN): {m.fn}",
    ]
    if m.pairs and (m.pair_discriminating or m.pair_both_refuted or m.pair_inverted):
        # A Cex firing on both halves of a pair is explained by what they share
        # (spec or translation), not by the bug, yet the entry-level metric
        # credits it as a true positive. This is the honest detection rate.
        lines += [
            "",
            f"  PAIR TEST ({m.pairs} complete pairs) - does the Cex track the BUG?",
            f"    discriminating (mutant refuted, reference NOT): "
            f"{m.pair_discriminating:>4}  ({m.pair_discriminating / m.pairs:.1%})",
            f"    non-discriminating (BOTH refuted)             : "
            f"{m.pair_both_refuted:>4}  <- shared cause, not the bug",
            f"    inverted (reference refuted, mutant not)      : {m.pair_inverted:>4}",
            f"    missed (neither refuted)                      : {m.pair_missed:>4}",
        ]
    if m.rescue_proved:
        lines += [
            "",
            f"  PRECONDITION RESCUE - proofs that needed a strengthened domain",
            f"    rescued proofs total                : {m.rescue_proved:>4}"
            f"  ({m.rescue_proved / m.tn if m.tn else 0:.1%} of proofs on correct entries)",
            f"    on correct entries   (plausible)    : {m.rescue_sound:>4}",
            f"    on INCORRECT entries (UNSOUND)      : {m.rescue_unsound:>4}"
            f"  <- bug defined away by the precondition",
            f"    clean proofs (no rescue)            : {m.tn + m.fn - m.rescue_proved:>4}",
        ]
    if m.abstain_reasons:
        lines.append("")
        lines.append("  abstain reasons")
        for reason, n in sorted(m.abstain_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {n:>4}  {reason}")
    return "\n".join(lines)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Report metrics from an nfv results JSONL")
    ap.add_argument("results", help="path to results .jsonl")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    from nfv.shared.store import Store

    recs = Store(args.results).records()
    m = compute(recs)
    if args.json:
        print(json.dumps({
            "total": m.total, "tp": m.tp, "fp": m.fp, "tn": m.tn, "fn": m.fn,
            "abstain_incorrect": m.abstain_incorrect, "abstain_correct": m.abstain_correct,
            "coverage": m.coverage, "accuracy_covered": m.accuracy_covered,
            "accuracy_overall": m.accuracy_overall, "precision": m.precision,
            "recall": m.recall, "f1": m.f1,
            "soundness_violations": m.soundness_violations,
            "abstain_reasons": m.abstain_reasons,
        }, indent=2))
    else:
        print(render(m, title=Path(args.results).stem))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
