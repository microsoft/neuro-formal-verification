"""Recompute available paper tables without making model or verifier calls."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import importlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DATA_GENERATORS = (
    ("make_dataset_tables", ("t_dataset", "t_specs")),
    ("dataset_size", ("main",)),
    ("make_dataset_provenance", ("main",)),
    ("make_tier_examples", ("main",)),
)
RUN_GENERATORS = {
    "make_tasks_table": ("lemma4_verify.jsonl", "lemma4_refute.jsonl", "lemma4_oracle.jsonl"),
    "make_tier_table": ("lemma4_verify.jsonl", "lemma4_refute.jsonl", "lemma4_oracle.jsonl"),
    "make_compare_tables": (
        "lemma4_verify.jsonl", "lemma4_refute.jsonl", "lemma4_haiku_verify.jsonl",
        "lemma4_haiku_refute.jsonl", "lemma4_mini_verify.jsonl", "lemma4_mini_refute.jsonl",
        "lemma4_sonnet46_verify.jsonl", "lemma4_sonnet46_refute.jsonl",
    ),
    "check_causes": ("lemma4_verify.jsonl", "lemma4_refute.jsonl"),
    "make_cbmc_clean": ("cbmc_theory.jsonl",),
    "make_cbmc_falsecex": ("cbmc_theory.jsonl",),
    "make_judge_numbers": ("judge.jsonl",),
    "make_naive_table": ("naive_nogates.jsonl",),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_result(path: Path, entries: list[dict]) -> str | None:
    """Reject partial/duplicate/mislabeled inputs rather than fill missing rows."""
    expected = 103 if path.name == "lemma4_oracle.jsonl" else (
        203 if path.name == "lemma4_sonnet46_refute.jsonl" else 206)
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        return f"unreadable result: {exc}"
    if len(rows) != expected:
        return f"expected {expected} original records, found {len(rows)}"
    if any(not isinstance(r, dict) or not isinstance(r.get("entry_id"), str) for r in rows):
        return "each result must be an object with a string entry_id"
    ids = [r.get("entry_id") for r in rows]
    if len(set(ids)) != len(ids):
        return "duplicate entry IDs"
    labels = {e["entry_id"]: e["label"] for e in entries}
    for row in rows:
        if row.get("entry_id") not in labels or row.get("label") != labels[row["entry_id"]]:
            return "entry IDs/labels do not match the frozen dataset"
        if row.get("prediction") not in {"correct", "incorrect", "abstain"}:
            return "invalid prediction"
        if path.name == "lemma4_oracle.jsonl" and row["label"] != "incorrect":
            return "oracle run must contain mutants only"
    return None


def reproduce(output: Path) -> dict:
    output = output.resolve()
    reported = ROOT / "paper/reported"
    if output == reported or reported in output.parents:
        raise ValueError("Reported manuscript/tables are immutable; choose another output directory")
    output.mkdir(parents=True, exist_ok=True)
    generated = output / "tables"
    generated.mkdir(exist_ok=True)
    report = {"scope": "Dafny/CBMC, LLM-judge and unstaged-Dafny results; Nagini intentionally excluded",
              "dataset": {}, "generated": {}, "blocked": {}, "run_metrics": {},
              "historical_metrics": {}}
    entries = json.loads((ROOT / "nfv/data/nl2post_safe.json").read_text())["entries"]
    report["dataset"] = {
        "entries": len(entries), "tasks": len({e["task_id"] for e in entries}),
        "labels": dict(Counter(e["label"] for e in entries)),
    }
    jobs = list(DATA_GENERATORS)
    from nfv.metrics import compute
    loaded = {}
    for name in sorted({f for required in RUN_GENERATORS.values() for f in required}):
        path = ROOT / "nfv/results" / name
        if not path.is_file() or validate_result(path, entries):
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        loaded[name] = rows
        metrics = compute(rows)
        claims = [r for r in rows if r["prediction"] != "abstain"]
        right = sum(r["prediction"] == r["label"] for r in claims)
        report["run_metrics"][name] = {
            "sha256": sha256(path), "records": len(rows), "claims": len(claims),
            "correct_claims": right, "wrong_claims": len(claims) - right,
            "claim_precision": right / len(claims) if claims else None,
            "metrics": asdict(metrics),
        }
    for name, required in RUN_GENERATORS.items():
        missing = [f"nfv/results/{f}" for f in required if not (ROOT / "nfv/results" / f).is_file()]
        invalid = {f: error for f in required if (ROOT / "nfv/results" / f).is_file()
                   and (error := validate_result(ROOT / "nfv/results" / f, entries))}
        if missing:
            report["blocked"][name] = {"missing": missing}
        elif invalid:
            report["blocked"][name] = {"invalid": invalid}
        else:
            jobs.append((name, ("main",)))
    for name, functions in jobs:
        module = importlib.reload(importlib.import_module("nfv.paper." + name))
        # Original scripts used multiple output conventions. Redirect outputs
        # only; input selection and scoring formulas remain the originals.
        module.TABLES = generated
        if hasattr(module, "OUT"):
            original = module.OUT
            module.OUT = generated / original.name if original.suffix else generated
        if hasattr(module, "MACROS"):
            module.MACROS = generated / module.MACROS.name
        for function in functions:
            result = getattr(module, function)()
            if isinstance(result, int) and result != 0:
                raise RuntimeError(f"{name}.{function} returned {result}")
    for table in sorted(generated.glob("*.tex")):
        retained = reported / "tables" / table.name
        report["generated"][table.name] = {
            "sha256": sha256(table),
            "matches_reported_text": (
                table.read_text(encoding="utf-8") == retained.read_text(encoding="utf-8")
                if retained.exists() else None
            ),
            "matches_reported_numeric_tokens": (
                re.findall(r"\d+(?:\.\d+)?", table.read_text(encoding="utf-8")) ==
                re.findall(r"\d+(?:\.\d+)?", retained.read_text(encoding="utf-8"))
                if retained.exists() else None
            ),
        }
    if {"lemma4_verify.jsonl", "lemma4_refute.jsonl"} <= loaded.keys():
        verify = {r["entry_id"]: r for r in loaded["lemma4_verify.jsonl"]}
        refute = {r["entry_id"]: r for r in loaded["lemma4_refute.jsonl"]}
        combined = []
        for key, row in verify.items():
            prediction = ("incorrect" if refute[key]["prediction"] == "incorrect"
                          else "correct" if row["prediction"] == "correct" else "abstain")
            combined.append({**row, "prediction": prediction})
        metrics = compute(combined)
        report["combined_metrics"] = {
            "rule": "make_tasks_table.py: refutation first, then verification; offline join, not a new run",
            "claims": metrics.covered, "correct_claims": metrics.tp + metrics.tn,
            "claim_precision": metrics.accuracy_covered,
            "overall_correct_fraction": metrics.accuracy_overall,
            "metrics": asdict(metrics),
        }
    historical = ROOT / "paper/data/upstream/a5r10_refute.jsonl"
    if historical.exists():
        rows = [json.loads(line) for line in historical.read_text(encoding="utf-8").splitlines() if line.strip()]
        metrics = compute(rows)
        report["historical_metrics"] = {
            "source": "paper/data/upstream/a5r10_refute.jsonl",
            "warning": "Historical plain-arm run, NOT the paper's final lemma4 refutation results.",
            "metrics": asdict(metrics),
            "rates": {name: getattr(metrics, name) for name in (
                "coverage", "accuracy_covered", "accuracy_overall", "precision", "recall", "f1")},
        }
    report["all_required_inputs_available"] = not report["blocked"]
    report["full_result_reproduction"] = (
        not report["blocked"] and
        all(item["matches_reported_numeric_tokens"] is not False
            for item in report["generated"].values()))
    (output / "metrics.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "paper/recomputed")
    parser.add_argument("--require-complete", action="store_true",
                        help="fail when any final-paper raw result is unavailable")
    args = parser.parse_args()
    report = reproduce(args.output)
    print(f"\nGenerated {len(report['generated'])} tables; "
          f"{len(report['blocked'])} result-dependent generators blocked.")
    print(f"Report: {args.output / 'metrics.json'}")
    return int(args.require_complete and not report["full_result_reproduction"])


if __name__ == "__main__":
    raise SystemExit(main())
