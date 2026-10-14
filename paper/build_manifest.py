"""Record extraction provenance; requires the original pinned checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from paper.extract_source import COMMIT


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise ValueError(f"Expected source {COMMIT}, got {actual}")
    text = (source / "nfv/EXPERIMENTS.md").read_text(encoding="utf-8")
    names = sorted(p.name for p in (root / "paper/results").glob("*.jsonl"))
    runs = {name: {"source_path": "nfv/results/" + name,
                   "status": "missing", "logged_runs": []} for name in names}
    recovery_path = root / "paper/provenance/local-results.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8")) if recovery_path.exists() else None
    if recovery:
        for item in recovery["files"]:
            name = Path(item["destination"]).name
            if name in runs:
                runs[name].update(status="retained", local_provenance=item)
    for match in re.finditer(r"(?m)^## (.*?)(?=^## |\Z)", text, re.S):
        block = match.group(0)
        result = re.search(r"(?m)^- results: `([^`]+)`", block)
        if not result:
            continue
        name = result.group(1).replace("\\", "/").split("/")[-1]
        if name not in runs:
            continue
        command = re.search(r"(?m)^- command: `(.*)`$", block)
        commit = re.search(r"(?m)^- commit: `(.*?)`(.*)$", block)
        cli = command.group(1) if command else None
        if cli:
            cli = re.sub(r" ---resolved-backend Backend\(.*?\) --", " --", cli)
        runs[name]["logged_runs"].append({
            "run_id": name.removesuffix(".jsonl") + "-" + str(len(runs[name]["logged_runs"]) + 1),
            "source_snapshot": commit.group(1) if commit else None,
            "command": cli,
            "command_note": "The logger's non-CLI ---resolved-backend repr is omitted; all real flags are retained.",
        })
    tree = subprocess.check_output(
        ["git", "-C", str(source), "ls-tree", "-r", "HEAD"], text=True)
    blobs = {line.split("\t", 1)[1]: line.split()[2] for line in tree.splitlines()}
    extraction = json.loads((root / "paper/provenance/extraction.json").read_text())
    provenance = {r["destination"]: r for r in extraction["files"]}
    additions = {}
    for path in (root / "paper/data/upstream").rglob("*"):
        if (path.is_file() and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}):
            additions[path.relative_to(root).as_posix()] = path.relative_to(root / "paper/data/upstream").as_posix()
    for dest, src in additions.items():
        provenance.setdefault(dest, {"source": src, "destination": dest})
    files = []
    for record in provenance.values():
        path = root / record["destination"]
        if not path.exists():
            continue
        files.append({**record, "source_git_blob": blobs.get(record["source"]),
                      "release_sha256": digest(path),
                      "modified_from_materialized_source": (
                          digest(path) != record["source_sha256"]
                          if "source_sha256" in record else None)})
    return {
        "paper": {"url": "https://arxiv.org/abs/2608.21516",
                  "verified_html": "https://arxiv.org/html/2608.21516",
                  "note": "Manuscript sources and table generators remain in the private research repository; reported claims below can be recounted from paper/results with python -m nfv.metrics."},
        "source": {"snapshot_id": COMMIT},
        "result_integrity": recovery,
        "metadata_redactions": json.loads(
            (root / "paper/provenance/redactions.json").read_text(encoding="utf-8")),
        "runtime": {"python": "3.10+; release validation 3.14",
                    "sdk": "github-copilot-sdk==1.0.4 (source client docstring)",
                    "dafny": "4.11.0", "z3_for_dafny": "4.12.1",
                    "cbmc": "6.9.0 (local validation; historical paper version not recorded)",
                    "model": "gpt-5.6-sol", "comparison_models": [
                        "claude-haiku-4.5", "gpt-5-mini", "claude-sonnet-4.6"],
                    "dataset_validation_model": "claude-sonnet-4.5 (100 tests/task)",
                    "availability": "Historical model IDs and account entitlements are external; no inference calls made during extraction."},
        "host_portability": {
            "historical_host": "Windows with MSVC; historical metrics are not Ubuntu measurements.",
            "supported_new_experiment_host": "Ubuntu 22.04",
            "dafny_version": "4.11.0",
            "ubuntu_archive_sha256": "a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179",
            "archive_provenance": "Official Dafny 4.11.0 Ubuntu 22.04 archive.",
            "validation_scope": "Platform-discovery regression tests and real verifier smoke are provided.",
        },
        "budgets": {
            "main_and_weak_models": {"attempts": 5, "proof_rounds": 10},
            "oracle": {"translation_attempts": 1, "proof_rounds": 3, "mutants_only": 103},
            "unstaged_baseline": {"attempts": 1, "proof_rounds": 3},
            "warning": "Use each logged command, not current CLI defaults. Refutation separately defaults to 5 witness attempts. A5 is not 5 full refutation restarts.",
        },
        "reported_claims": {
            "dafny_verify": {"correct_claims": 72, "wrong_claims": 5, "class_population": 103},
            "dafny_refute": {"correct_claims": 47, "wrong_claims": 6, "class_population": 103},
            "combined": {"claims": 128, "correct_claims": 118, "population": 206,
                         "rule": "offline join of two independent arms (refutation first); not run.py's newer combined mode"},
            "cbmc": {"recall_percent_rounded": 63, "precision_percent_rounded": 90},
            "judge": {"precision_percent_rounded": 72},
            "unstaged": {"proved_percent_each_class_rounded": 98, "precision_percent": 50},
        },
        "result_files": runs,
        "smoke_replay": {
            "command": "python -m paper.smoke",
            "report": "paper/replayed/smoke.json",
            "sha256": digest(root / "paper/replayed/smoke.json"),
            "scope": "Four fixed retained model artifacts, two per backend; no inference or model repair.",
        },
        "files": sorted(files, key=lambda r: r["destination"]),
        "release_authored_files": [
            {"path": name, "sha256": digest(root / name),
             "provenance": "New extraction, portability, validation or workflow integration code"}
            for name in (
                "paper/prepare.py", "paper/smoke.py", "paper/redaction.py",
                "paper/extract_source.py", "paper/build_manifest.py",
                "tests/test_paper_core.py", "tests/test_paper_smoke.py")
        ],
        "gaps": [
            "Complete per-run implementation and model-service snapshots are not preserved; bit-identical new inference results are not guaranteed.",
            "Historical invocation metadata is unavailable for Sonnet refutation and judge. The Sonnet refutation result contains 203 entries, not 206.",
            "S1 upstream archives and historical EvalPlus cache/package version are not bundled; retained S1 output permits deterministic downstream checking.",
            "S2 validate_specs.py is archival and references legacy py2c.call_llm_async, not included; original generated tests are not retained in its summary JSON. Do not regenerate S2 silently with a new model.",
            "Nagini results mentioned in v3 are intentionally outside this Dafny/CBMC release scope.",
            "The retained source does not explicitly pin the historical CBMC version. Version 6.9.0 is the executable discovered and self-tested locally, not independently established as the paper's exact binary.",
        ],
        "ports": [
            "Restricted backend registry and removed unsupported backend imports/branches; retained exact relevant prompt literals.",
            "Removed the newer online combined mode, which was not the paper's offline join of two completed arms.",
            "Relocated dataset predecessors under paper/data/upstream; original frozen dataset bytes retained.",
            "LaTeX table generators, the manuscript snapshot and the table reproduction script are not released.",
            "Verifier scratch directories are repository-local instead of system temporary paths.",
            "CBMC inherits the normal compiler environment on non-Windows hosts instead of requiring MSVC there; Windows discovery is unchanged.",
            "Non-Windows CBMC lookup uses the explicit override or PATH and does not select Windows .exe candidates.",
            "Removed automatic git staging/commit of experiment logs; logging remains available without modifying Git history.",
            "Future command logs omit the internal _resolved_backend object because it is not a CLI argument.",
            "An explicit identity pin fails closed if account identity is unavailable.",
            "Workflow reuses the existing stdlib-only run_dafny backend; no additional general-purpose refutation adapter is included.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    result = build(args.source)
    Path(__file__).with_name("manifest.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
