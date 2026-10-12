"""Record extraction provenance; requires the original pinned checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from paper.extract_source import COMMIT
from paper.reproduce import RUN_GENERATORS


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise ValueError(f"Expected source {COMMIT}, got {actual}")
    text = (source / "nfv/EXPERIMENTS.md").read_text(encoding="utf-8")
    names = sorted({n for values in RUN_GENERATORS.values() for n in values})
    runs = {name: {"source_path": "nfv/results/" + name, "tracked_at_source_commit": False,
                   "status": "missing", "logged_runs": []} for name in names}
    recovery_path = root / "paper/provenance/local-results.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8")) if recovery_path.exists() else None
    if recovery:
        for item in recovery["files"]:
            name = Path(item["destination"]).name
            if name in runs:
                runs[name].update(status="recovered_local", local_provenance=item)
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
            "heading": block.splitlines()[0],
            "experiment_log_line": text[:match.start()].count("\n") + 1,
            "commit": commit.group(1) if commit else None,
            "dirty": "uncommitted changes" in (commit.group(2) if commit else ""),
            "command": cli,
            "command_note": "The logger's non-CLI ---resolved-backend repr is omitted; all real flags are retained.",
        })
    tree = subprocess.check_output(
        ["git", "-C", str(source), "ls-tree", "-r", "HEAD"], text=True)
    blobs = {line.split("\t", 1)[1]: line.split()[2] for line in tree.splitlines()}
    extraction = json.loads((root / "paper/provenance/extraction.json").read_text())
    provenance = {r["destination"]: r for r in extraction["files"]}
    additions = {
        "nfv/paper/make_dataset_tables.py": "nfv/paper/make_tables.py",
        "nfv/paper/make_naive_table.py": "nfv/paper/make_naive_table.py",
    }
    for path in (root / "paper/data/upstream").rglob("*"):
        if path.is_file():
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
        "paper": {"url": "https://arxiv.org/abs/2608.21516v3",
                  "verified_html": "https://arxiv.org/html/2608.21516v3",
                  "retained_snapshot": "nfv/paper/nfv_arxiv",
                  "note": "Source snapshot retained without editing. Claims checked against official v3 HTML; not a claim of identical arXiv archive bytes."},
        "source": {"repository": "https://github.com/shuvendu-lahiri/quasi-formal-verification",
                   "ref": "neuro-formal-v1", "commit": COMMIT,
                   "results_in_head": 0, "results_path_history": "git log --all -- nfv/results returned no commits",
                   "github_results_probe": "contents/nfv/results/lemma4_verify.jsonl?ref=neuro-formal-v1 returned HTTP 404"},
        "remote_artifact_search": json.loads(
            (root / "paper/provenance/remote-artifacts.json").read_text(encoding="utf-8")),
        "local_result_recovery": recovery,
        "runtime": {"python": "3.10+; release validation 3.14",
                    "sdk": "github-copilot-sdk==1.0.4 (source client docstring)",
                    "dafny": "4.11.0", "z3_for_dafny": "4.12.1",
                    "cbmc": "6.9.0 (local validation; historical paper version not recorded)",
                    "model": "gpt-5.6-sol", "comparison_models": [
                        "claude-haiku-4.5", "gpt-5-mini", "claude-sonnet-4.6"],
                    "dataset_validation_model": "claude-sonnet-4.5 (100 tests/task)",
                    "availability": "Historical model IDs and account entitlements are external; no inference calls made during extraction."},
        "host_portability": {
            "historical_host_evidence": "Windows paths and MSVC in EXPERIMENTS.md; recovered metrics are not Ubuntu measurements.",
            "supported_new_experiment_host": "Ubuntu 22.04",
            "dafny_version": "4.11.0",
            "ubuntu_archive_sha256": "a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179",
            "archive_provenance": "Parent-owned Ubuntu Actions tool setup; do not substitute z3-bench's older Dafny 4.9.1 Ubuntu 20.04 package.",
            "validation_scope": "Non-Windows discovery regression tests here; real Ubuntu binary execution belongs to parent Actions validation.",
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
                         "rule": "make_tasks_table.py joins two independent arms; not run.py's newer combined mode"},
            "cbmc": {"recall_percent_rounded": 63, "precision_percent_rounded": 90},
            "judge": {"precision_percent_rounded": 72},
            "unstaged": {"proved_percent_each_class_rounded": 98, "precision_percent": 50},
        },
        "result_files": runs,
        "recomputation": {
            "command": "python -m paper.reproduce --require-complete",
            "report": "paper/recomputed/metrics.json",
            "note": "Recovered per-entry results, not substituted historical plain-arm results, drive the final tables.",
        },
        "generators": {name: list(paths) for name, paths in RUN_GENERATORS.items()},
        "files": sorted(files, key=lambda r: r["destination"]),
        "release_authored_files": [
            {"path": name, "sha256": digest(root / name),
             "provenance": "New extraction, portability, validation or workflow integration code"}
            for name in (
                "paper/reproduce.py", "paper/prepare.py",
                "paper/extract_source.py", "paper/build_manifest.py", "tests/test_paper_core.py")
        ],
        "gaps": [
            "Resumed files span multiple commits; dirty run deltas were not captured. Source HEAD is not an exact snapshot of every reported run.",
            "No run entry for lemma4_sonnet46_refute.jsonl or judge.jsonl was found in EXPERIMENTS.md; comparison script records Sonnet refutation abandoned at 203 entries.",
            "S1 upstream archives and historical EvalPlus cache/package version are not bundled; retained S1 output permits deterministic downstream checking.",
            "S2 validate_specs.py is archival and references legacy py2c.call_llm_async, not included; original generated tests are not retained in its summary JSON. Do not regenerate S2 silently with a new model.",
            "Nagini results mentioned in v3 are intentionally outside this Dafny/CBMC release scope.",
            "The retained source does not explicitly pin the historical CBMC version. Version 6.9.0 is the executable discovered and self-tested locally, not independently established as the paper's exact binary.",
        ],
        "ports": [
            "Restricted backend registry and removed unsupported backend imports/branches; retained exact relevant prompt literals.",
            "Removed the newer online combined mode, which was not the paper's offline join of two completed arms.",
            "Relocated dataset predecessors under paper/data/upstream; original frozen dataset bytes retained.",
            "Source-comparison generator narrowed to final model/analysis tables; dataset-only generators extracted from historical multipurpose script.",
            "Verifier scratch directories are repository-local instead of system temporary paths.",
            "CBMC inherits the normal compiler environment on non-Windows hosts instead of requiring MSVC there; Windows discovery is unchanged.",
            "Non-Windows CBMC lookup uses the explicit override or PATH and does not select Windows .exe candidates.",
            "Removed automatic git staging/commit of experiment logs; logging remains available without modifying Git history.",
            "Future command logs omit the internal _resolved_backend object (a non-CLI argument); original historical log provenance is unchanged.",
            "An explicit NFV_EXPECTED_LOGIN pin now fails closed if account identity is unavailable; no author-account default is set and historical EMU opt-in policy is retained.",
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
