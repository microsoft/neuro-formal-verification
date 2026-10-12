"""Extract the paper's dependency closure from a pinned source snapshot.

This is a maintainer utility, not a benchmark prerequisite. It copies original
source slices without rewriting prompts or executable statements.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil

COMMIT = "650a2ce0cffff9847ea4f4674e8a6e1168d172a1"
CORE = [
    "__init__.py", "run.py", "run_judge.py", "metrics.py",
    "backends/__init__.py", "backends/_scratch.py", "backends/dafny.py",
    "backends/cbmc.py", "shared/__init__.py", "shared/account.py",
    "shared/config.py", "shared/copilot_client.py", "shared/store.py",
    "agent/__init__.py", "agent/c_integrity.py", "agent/c_witness.py",
    "agent/integrity.py", "agent/invariant_agent.py", "agent/library_theory.py",
    "agent/oracle_witness.py", "agent/provenance.py", "agent/pydocs.py",
    "agent/refute.py",
    "agent/tautology.py", "agent/translate.py", "agent/ub_repair.py",
    "data/__init__.py", "data/build_safe.py", "data/build_subset.py",
    "data/killing_tests.py", "data/rebuild_dataset.py", "data/verify_no_leak.py",
    "data/nl2post_safe.json", "data/MANIFEST.md", "data/input_assumptions.json",
]
ANALYSIS = [
    "check_causes.py", "dataset_size.py", "make_cbmc_clean.py",
    "make_cbmc_falsecex.py", "make_compare_tables.py",
    "make_dataset_provenance.py", "make_judge_numbers.py",
    "make_tasks_table.py", "make_tier_examples.py", "make_tier_table.py", "make_naive_table.py",
]


def extract_dataset_tables(source: Path, destination: Path) -> None:
    original = source / "nfv/paper/make_tables.py"
    lines = original.read_text(encoding="utf-8").splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    wanted = {"esc", "write", "t_dataset", "t_specs"}
    parts = ['"""Dataset-only generators extracted from make_tables.py."""\n']
    for node in tree.body:
        keep = isinstance(node, (ast.Import, ast.ImportFrom))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            keep = ast.unparse(target) in {"TIERS", "ROOT", "OUT"}
        if isinstance(node, ast.FunctionDef):
            keep = node.name in wanted
        if keep:
            parts.extend(lines[node.lineno - 1:node.end_lineno])
            parts.append("\n")
    parts.append('if __name__ == "__main__":\n    t_dataset()\n    t_specs()\n')
    (destination / "nfv/paper/make_dataset_tables.py").write_text(
        "".join(parts), encoding="utf-8", newline="\n")


def extract_comparisons(source: Path, destination: Path) -> None:
    original = source / "nfv/paper/make_compare_tables.py"
    lines = original.read_text(encoding="utf-8").splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    excluded = {"cbmc_exhausted", "cbmc_row", "model_macros", "ms_files",
                "resolve_modelscale", "opus_macros", "main"}
    parts = ['"""Final-paper model comparison and attribution generators."""\n']
    for node in tree.body:
        if isinstance(node, ast.Expr) or isinstance(node, ast.If):
            continue
        if isinstance(node, ast.FunctionDef) and node.name in excluded:
            continue
        if (isinstance(node, ast.Assign)
                and ast.unparse(node.targets[0]) == "COMPARISONS"):
            parts.append("COMPARISONS = {\n")
            for key, value in zip(node.value.keys, node.value.values):
                if key.value == "weakmodel":
                    parts.extend(lines[key.lineno - 1:value.end_lineno])
            parts.append("}\n")
            continue
        parts.extend(lines[node.lineno - 1:node.end_lineno])
        parts.append("\n")
    parts.append('''def main():
    TABLES.mkdir(parents=True, exist_ok=True)
    files, stale = resolve_weakmodel()
    (TABLES / "weakmodel.tex").write_text(
        build("weakmodel", COMPARISONS["weakmodel"]), encoding="utf-8")
    weak_macros(files, stale)
    analysis_macros()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
''')
    (destination / "nfv/paper/make_compare_tables.py").write_text(
        "".join(parts), encoding="utf-8", newline="\n")


def extract(source: Path, destination: Path) -> None:
    records = []

    def copy(src: str, dst: str):
        original = source / src
        target = destination / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        records.append({
            "source": src, "destination": dst,
            "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        })

    for name in CORE:
        copy("nfv/" + name, "nfv/" + name)
    # Remove unreachable implementation branches for excluded languages, keeping
    # every retained statement/prompt byte-for-byte rather than AST-unparsing it.
    for name in ("agent/refute.py", "agent/invariant_agent.py", "run.py"):
        target = destination / "nfv" / name
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        tree = ast.parse(text)
        drop = set()
        for node in ast.walk(tree):
            remove = False
            if name == "agent/refute.py":
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    remove = node.name.startswith(("_smt", "_lean", "_invert_smt",
                                                   "_invert_lean", "_reachable_smt",
                                                   "_reachable_lean")) or node.name == "_selftest"
                elif isinstance(node, ast.Assign):
                    remove = any(ast.unparse(t).startswith(("SMT_", "LEAN_", "_LEAN_"))
                                 for t in node.targets)
                elif isinstance(node, ast.ImportFrom):
                    remove = any(a.name == "lean" for a in node.names)
                elif isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
                    remove = True
            if isinstance(node, ast.If):
                test = node.test
                if (isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.Eq)
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value in {"smt", "lean", "boogie"}
                        and ast.unparse(test.left) in {"lang", "backend.lang", "bk.lang"}
                        and not node.orelse):
                    remove = True
            if remove:
                drop.update(range(node.lineno - 1, node.end_lineno))
        target.write_text("".join(line for i, line in enumerate(lines) if i not in drop),
                          encoding="utf-8", newline="\n")
    for name in ANALYSIS:
        copy("nfv/paper/" + name, "nfv/paper/" + name)
    extract_dataset_tables(source, destination)
    extract_comparisons(source, destination)
    for path in (source / "nfv/paper/nfv_arxiv").rglob("*"):
        if path.is_file() and path.suffix == ".tex":
            copy(path.relative_to(source).as_posix(),
                 "paper/reported/" + path.relative_to(source / "nfv/paper/nfv_arxiv").as_posix())
    copy("nfv/paper/nfv_arxiv/figs/northstar.pdf", "paper/reported/figs/northstar.pdf")
    for path in (source / "nfv/data/subsets").glob("pilot10.json"):
        copy(path.relative_to(source).as_posix(), path.relative_to(source).as_posix())
    # Keep only the registered variants needed by the final paper and their bases.
    original = source / "nfv/backends/registry.py"
    text = original.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    parts = ['"""Dafny and CBMC registry, extracted from the pinned paper source."""\n']
    prefixes = ("_DAFNY_", "_C_")
    unused = {"_DAFNY_TOTAL_SYNTAX", "_DAFNY_LAMBDA_PERMISSION",
              "_DAFNY_TOTALITY_PROOF", "_ORDERED_PROOF"}
    imports = {"__future__", "dataclasses", "typing"}
    for node in tree.body:
        keep = False
        if isinstance(node, ast.ImportFrom):
            keep = (node.module in imports or
                    any(a.name in {"c_integrity", "integrity", "dafny", "cbmc"}
                        for a in node.names))
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            keep = node.name in {"Backend", "_dafny_integrity", "get"}
        elif isinstance(node, ast.Assign):
            target = ast.unparse(node.targets[0])
            keep = ((target.startswith(prefixes) and target not in unused) or
                    target == "BACKENDS['dafny-theory-lemma4']")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Dict):
            parts.append("BACKENDS: dict[str, Backend] = {\n")
            for key, value in zip(node.value.keys, node.value.values):
                if key.value in {"dafny", "dafny-naive", "dafny-theory", "cbmc", "cbmc-theory"}:
                    parts.extend(lines[key.lineno - 1:value.end_lineno])
            parts.append("}\n")
        elif isinstance(node, ast.For):
            keep = ast.unparse(node.target) == "(_name, _bk)"
        if keep:
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            parts.extend(lines[start - 1:node.end_lineno])
            parts.append("\n")
    out = destination / "nfv/backends/registry.py"
    out.write_text("".join(parts), encoding="utf-8", newline="\n")
    records.append({"source": "nfv/backends/registry.py",
                    "destination": "nfv/backends/registry.py",
                    "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                    "transformation": "AST-selected source slices; retained variants and dependencies only"})
    (destination / "paper/provenance/extraction.json").write_text(
        json.dumps({"source_commit": COMMIT, "files": records}, indent=2) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path, default=Path.cwd())
    args = parser.parse_args()
    extract(args.source, args.destination)
