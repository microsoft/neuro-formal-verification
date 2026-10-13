from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .github import Abstain
from .model import render_model, select_candidates
from .source_binding import bind_source


class Dafny:
    """Use the released NFV backend, but strip credentials from its process."""

    def __init__(self, timeout=60):
        self.timeout = timeout

    def __call__(self, source, directory):
        return self.run(source, directory)

    def resolve(self, source, directory):
        return self.run(source, directory, operation="resolve")

    def run(self, source, directory, operation="verify", filter_symbol=""):
        directory.mkdir(parents=True, exist_ok=True)
        path, result_path = directory / "model.dfy", directory / "verifier.json"
        path.write_text(source, encoding="utf-8")
        keep = {"PATH", "SYSTEMROOT", "WINDIR", "DOTNET_ROOT", "DOTNET_ROOT_X64",
                "HOME", "USERPROFILE", "LANG", "LC_ALL", "PATHEXT",
                "NFV_Z3_PATH", "PYTHONPATH"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "nfv_workflow.worker", str(path.resolve()),
                 str(result_path.resolve()), str(self.timeout), operation, filter_symbol],
                capture_output=True, text=True, env=env, timeout=self.timeout + 20)
            if proc.returncode != 0 or not result_path.is_file():
                return {"outcome": "tool_error", "exit_code": proc.returncode,
                        "stdout": "", "stderr": proc.stderr[-3000:]}
            result = json.loads(result_path.read_text(encoding="utf-8"))
            for field in ("stdout", "stderr"):
                result[field] = result.get(field, "").replace(str(directory.resolve()), ".")
            result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        except subprocess.TimeoutExpired:
            return {"outcome": "timeout", "exit_code": None, "stdout": "", "stderr": ""}


def accepted(result):
    # A zero process status without an actual positive verification summary is
    # not sufficient. In particular, parse errors and timeouts never refute.
    return (result.get("outcome") == "verified" and result.get("exit_code") == 0
            and result.get("mode") == "unbounded"
            and re.search(r"\b[1-9]\d* verified, 0 errors\b", result.get("stdout", "")) is not None)


def check(bundle, proposals, output, verifier=None, budget_seconds=900):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    verifier = verifier or Dafny()
    started = time.monotonic()
    report = {"schema": 1, "identity": bundle["identity"], "findings": [],
              "abstentions": [], "errors": []}
    try:
        candidates = select_candidates(proposals, bundle)
    except (Abstain, KeyError, TypeError, ValueError) as exc:
        report["errors"].append(f"Proposal validation: {exc}")
        candidates = []
    for candidate in candidates:
        try:
            if time.monotonic() - started >= budget_seconds:
                raise Abstain("Run verification budget exhausted")
            directory = output / candidate["id"]
            retained = list(range(len(candidate["model"].get("assumptions", []))))
            source = render_model(candidate, retained)
            result = verifier(source, directory / "initial")
            if not accepted(result):
                report["abstentions"].append({"id": candidate["id"],
                                              "reason": result.get("outcome", "tool_error")})
                continue
            minimization = []
            # Greedy subset minimization, not a globally minimum assumption set.
            for index in retained[:]:
                if time.monotonic() - started >= budget_seconds:
                    raise Abstain("Run verification budget exhausted during minimization")
                trial = [i for i in retained if i != index]
                trial_source = render_model(candidate, trial)
                trial_result = verifier(trial_source, directory / f"remove-{index}")
                removed = accepted(trial_result)
                minimization.append({"index": index, "removed": removed,
                                     "outcome": trial_result.get("outcome")})
                if removed:
                    retained = trial
            source = render_model(candidate, retained)
            final = verifier(source, directory / "final")
            if not accepted(final):
                report["abstentions"].append({"id": candidate["id"], "reason": "Final recheck failed"})
                continue
            binding = bind_source(candidate, bundle)
            evidence_path = directory / "final" / "model.dfy"
            # Fake verifiers in unit tests need not materialize evidence.
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(source, encoding="utf-8")
            (evidence_path.parent / "verifier.json").write_text(
                json.dumps(final, indent=2), encoding="utf-8")
            report["findings"].append({
                "candidate": candidate, "retained_assumptions": retained,
                "source_binding": binding,
                "minimization": minimization, "result": final,
                "dafny_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "evidence": f"{candidate['id']}/final/model.dfy",
                "dafny": source})
        except (Abstain, KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
            report["abstentions"].append({"id": candidate["id"], "reason": str(exc)})
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
