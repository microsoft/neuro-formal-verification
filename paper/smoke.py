# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Replay four fixed retained formal artifacts, without translation or inference."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import ntpath

from nfv.backends import cbmc, dafny
from nfv.shared import config
from paper.redaction import public_metadata

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Case:
    name: str
    result_file: str
    entry_id: str
    backend: str
    artifact_field: str
    expected_outcome: str
    expected_prediction: str


CASES = (
    Case("dafny-reference-proof", "lemma4_verify.jsonl",
         "HumanEval_157__r1__reference", "dafny", "final_dafny", "verified", "correct"),
    Case("dafny-mutant-negation", "lemma4_refute.jsonl",
         "HumanEval_97__r0__mutant", "dafny", "final_dafny", "verified", "incorrect"),
    Case("cbmc-reference-bounded-clean", "cbmc_theory.jsonl",
         "HumanEval_53__r0__reference", "cbmc", "model_dafny", "verified", "abstain"),
    Case("cbmc-mutant-counterexample", "cbmc_theory.jsonl",
         "HumanEval_53__r0__mutant", "cbmc", "model_dafny", "failed", "incorrect"),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_case(case: Case) -> tuple[dict, str, dict]:
    """Validate the release file hash and extract the exact stored code string."""
    provenance = json.loads((ROOT / "paper/provenance/local-results.json").read_text(encoding="utf-8"))
    relative = "paper/results/" + case.result_file
    pin = next(r for r in provenance["files"] if r["destination"] == relative)
    raw = (ROOT / relative).read_bytes()
    if digest(raw) != pin["sha256"]:
        raise ValueError(f"{relative}: release raw SHA-256 mismatch")
    matches = []
    for number, line in enumerate(raw.splitlines(keepends=True), 1):
        if line.strip():
            row = json.loads(line)
            if row.get("entry_id") == case.entry_id:
                matches.append((number, line, row))
    if len(matches) != 1:
        raise ValueError(f"{relative}: expected exactly one {case.entry_id}")
    number, line, row = matches[0]
    if row.get("prediction") != case.expected_prediction:
        raise ValueError("fixed selection no longer has its original prediction")
    code = row.get(case.artifact_field)
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"missing retained {case.artifact_field}")
    if case.backend == "cbmc":
        if row.get("bounded_outcome") != case.expected_outcome:
            raise ValueError("recorded CBMC verifier outcome differs from selection")
        evidence = "explicit bounded_outcome and bounded_exit fields"
    elif case.expected_prediction == "correct":
        if not any(a.get("proved") is True for a in row.get("attempt_log", [])):
            raise ValueError("original record does not report a successful proof")
        evidence = "attempt_log.proved=true; raw record does not contain the Dafny process exit code"
    else:
        if not any(a.get("outcome") == "proved" for a in row.get("attempt_log", [])):
            raise ValueError("original record does not report a proof of the negation")
        gates = row.get("gates", {})
        if not all(gates.get(k) is True for k in ("reachable", "inversion_intact", "integrity_ok")):
            raise ValueError("original refutation record did not pass its gates")
        evidence = "attempt_log.outcome=proved and accepted refutation gates; no raw Dafny process exit code"
    identity = {
        "result_file": relative,
        "result_file_sha256": digest(raw),
        "original_result_file_sha256": pin.get("original_sha256", pin["sha256"]),
        "result_line": number,
        "result_line_sha256": digest(line),
        "artifact_field": case.artifact_field,
        "artifact_utf8_sha256": digest(code.encode("utf-8")),
        "artifact_utf8_bytes": len(code.encode("utf-8")),
        "expected_outcome_evidence": evidence,
    }
    return row, code, identity


def toolchain() -> dict:
    return {
        name: {"executable": ntpath.basename(executable) if executable else None,
               "version": config._version([executable, "--version"]) if executable else "not found"}
        for name, executable in (
            ("dafny", dafny.DAFNY_PATH), ("z3", dafny.Z3_PATH), ("cbmc", cbmc.CBMC_PATH))
    }


def replay_case(case: Case, *, timeout: float) -> dict:
    result = {**asdict(case), "matched": False, "discrepancies": []}
    try:
        row, code, identity = load_case(case)
        result.update(identity)
        result["recorded"] = {
            "prediction": row["prediction"], "label": row["label"],
            "phase": row.get("phase_reached"), "reason": row.get("reason"),
            "bounded_outcome": row.get("bounded_outcome"),
            "bounded_exit": row.get("bounded_exit"),
        }
        mode = "bounded" if case.backend == "cbmc" else "unbounded"
        result["invocation"] = {"mode": mode, "timeout_seconds": timeout}
        if case.backend == "cbmc":
            # These are the original arm parameters, not enlarged smoke bounds.
            result["invocation"]["unroll"] = 10
            result["prelude_sha256"] = digest(cbmc.PRELUDE.encode("utf-8"))
            result["submitted_source_sha256"] = digest((cbmc.PRELUDE + code).encode("utf-8"))
            observed = cbmc.run_cbmc(code, mode=mode, unroll=10, timeout=timeout)
            prediction = "incorrect" if observed.is_cex and not observed.is_malformed else "abstain"
        else:
            result["submitted_source_sha256"] = identity["artifact_utf8_sha256"]
            observed = dafny.run_dafny(code, mode=mode, timeout=timeout)
            # For refutation this code ALREADY carries the proved negated goal.
            # This is artifact replay, not another translation/gate/search run.
            prediction = case.expected_prediction if observed.outcome is dafny.Outcome.VERIFIED else "abstain"
        result["observed"] = public_metadata(observed.to_dict())
        result["artifact_prediction"] = prediction
        if observed.outcome.value != case.expected_outcome:
            result["discrepancies"].append("verifier outcome differs from original")
        if prediction != row["prediction"]:
            result["discrepancies"].append("artifact verdict differs from recorded pipeline verdict")
        if case.backend == "cbmc" and observed.exit_code != row.get("bounded_exit"):
            result["discrepancies"].append("CBMC exit code differs from recorded bounded_exit")
        if case.backend == "dafny" and observed.exit_code != 0:
            result["discrepancies"].append("Dafny did not return successful proof exit code 0")
        result["matched"] = not result["discrepancies"]
    except Exception as exc:
        result["discrepancies"].append(public_metadata(f"{type(exc).__name__}: {exc}"))
    return result


def run_smoke(*, backend: str = "all", timeout: float = 60.0) -> dict:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if backend not in {"all", "dafny", "cbmc"}:
        raise ValueError("backend must be all, dafny, or cbmc")
    cases = [case for case in CASES if backend == "all" or case.backend == backend]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "python": platform.python_version()},
        "scope": "Exact retained formal artifacts only: no translation, repair, witness search, or inference.",
        "limitation": (
            "Dafny refutation replays its already-negated final model; original admission gates are "
            "validated from the immutable record, not re-executed. This does not re-attest source/model "
            "correspondence. CBMC bounded success remains abstain. Historical CBMC version is not pinned."),
        "selection": backend,
        "metadata_redaction": "Host-specific diagnostic/executable paths are anonymized; model code and verifier verdicts are unchanged.",
        "toolchain": toolchain(),
        "backend_source_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): digest(path.read_bytes())
            for path in (ROOT / "nfv/backends/dafny.py", ROOT / "nfv/backends/cbmc.py",
                         ROOT / "nfv/backends/_scratch.py", ROOT / "nfv/shared/config.py")
        },
        "cases": [replay_case(case, timeout=timeout) for case in cases],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["all", "dafny", "cbmc"], default="all")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per verifier invocation")
    parser.add_argument("--output", type=Path, default=ROOT / ".nfv-work/paper-smoke.json")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    protected = (ROOT / "nfv", ROOT / "paper/data", ROOT / "paper/results",
                 ROOT / "paper/provenance", ROOT / "paper/licenses")
    if (not output.is_relative_to(ROOT) or output == ROOT / "paper/manifest.json"
            or any(output.is_relative_to(path) for path in protected)):
        parser.error("--output must stay in the checkout without replacing inputs or provenance")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    report = run_smoke(backend=args.backend, timeout=args.timeout)
    report["all_matched"] = all(case["matched"] for case in report["cases"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    for case in report["cases"]:
        print(f"{'MATCH' if case['matched'] else 'MISMATCH'} {case['name']}: {case['entry_id']}")
        for message in case["discrepancies"]:
            print(f"  {message}")
    print(f"Evidence: {output}")
    return 0 if report["all_matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
