# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Maintainer-only, label-blinded evaluation of the shared PR audit engine."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import time
import uuid

from .cli import audit, audit_limits, load, save
from .followups import Options
from .general import Limits, select_properties, validate_finding, validate_verification
from .github import Abstain, GitHub, repository, sha
from .outcomes import EXHAUSTED, INVALID, REFUTED, VERIFIED
from .publish import text
from .source_context import function_catalog


TOTAL = 206
ENTRY_COUNTS = (5, TOTAL)
SHARD_SIZE = 8
LABELS = ("correct", "incorrect")
PREDICTIONS = (*LABELS, "abstain")
CASE_ID = re.compile(r"case-[a-f0-9]{32}")
USAGE_FIELDS = ("api_calls", "input_tokens", "output_tokens", "nano_aiu")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_case(entry, release, repo):
    """Capture source as text and select the supplied entry, not an easier claim."""
    source = entry["code"]
    definitions = [node for node in ast.parse(source).body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == entry["entry_point"]]
    if len(definitions) != 1:
        raise Abstain("Benchmark entry must identify one top-level function")
    postcondition = entry["postcondition"]
    assertion = ast.parse(postcondition).body
    if (not any(isinstance(node, ast.Assert) for node in assertion)
            or any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assert)) for node in assertion)):
        raise Abstain("Benchmark property must contain the complete supplied assertions and import context")
    path = "subject.py"
    raw = source.encode("utf-8")
    quote = entry["docstring"] + "\n\nComplete supplied postcondition:\n" + postcondition
    bundle = {
        "identity": {"repository": repo, "number": 1, "source_kind": "benchmark",
                     "source_sha": release, "head_sha": release, "base_sha": release},
        "sources": {path: {"text": source, "blob_sha": hashlib.sha1(
            b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}},
        "changed": {path: [definitions[0].lineno]},
        "signals": [{"id": "benchmark-specification", "text": quote}],
    }
    catalog = function_catalog(bundle)["functions"]
    if len(catalog) != 1:
        raise Abstain("Could not bind the benchmark entry to a captured function")
    proposal = {"function_id": catalog[0]["id"], "signal_id": "benchmark-specification",
                "quote": quote, "property": postcondition, "language": "python"}
    if len(select_properties([proposal], bundle)) != 1:
        raise Abstain("Benchmark specification did not select exactly one whole property")
    return {"id": "case-" + uuid.uuid4().hex, "bundle": bundle, "properties": [proposal]}


def prepare(dataset, output, release, model, repo, run_id, attempt, limits=None, *, count=TOTAL):
    release, repo = sha(release), repository(repo)
    if type(count) is not int or count not in ENTRY_COUNTS:
        raise Abstain("Choose a five-entry pilot or the complete 206-entry benchmark")
    if (not isinstance(model, str) or not model.strip() or model != model.strip()
            or len(model) > 200 or any(ord(char) < 32 for char in model)):
        raise Abstain("Use the explicitly configured PR-review NFV_MODEL")
    if not all(re.fullmatch(r"[1-9]\d*", str(value)) for value in (run_id, attempt)):
        raise Abstain("Expected positive Actions run and attempt IDs")
    limits = replace(limits or audit_limits(), minimize_assumptions=False)
    if SHARD_SIZE * (limits.entry_seconds + 3 * limits.verifier_timeout) > 330 * 60:
        raise Abstain("PR-review time limits exceed the benchmark worker's six-hour job budget")
    data = load(dataset)
    entries = data["entries"]
    if (data.get("total") != TOTAL or len(entries) != TOTAL
            or len({entry["entry_id"] for entry in entries}) != TOTAL
            or Counter(entry["label"] for entry in entries) != {"correct": 103, "incorrect": 103}):
        raise Abstain("Expected the frozen 206-entry, 103/103 nfv-micro-bench dataset")
    shards = math.ceil(count / SHARD_SIZE)
    run = {"repository": repo, "run_id": str(run_id), "sweep_id": f"{run_id}-{attempt}",
           "release_sha": release, "model": model, "limits": asdict(limits),
           "dataset_sha256": hashlib.sha256(Path(dataset).read_bytes()).hexdigest(),
           "started_at": now(), "total": count, "shards": shards, "billing": "personal-pat"}
    # Frozen dataset order alternates reference/mutant; do not expose that order.
    entries = list(entries)
    random.SystemRandom().shuffle(entries)
    entries = entries[:count]
    output = Path(output)
    if output.exists():
        raise Abstain("Use a fresh benchmark preparation directory")
    manifest = {"schema": 1, "run": run, "cases": []}
    for shard in range(shards):
        cases = []
        for entry in entries[shard * SHARD_SIZE:(shard + 1) * SHARD_SIZE]:
            case = make_case(entry, release, repo)
            cases.append(case)
            manifest["cases"].append({"id": case["id"], "entry_id": entry["entry_id"],
                                      "label": entry["label"], "shard": shard,
                                      "input_sha256": digest(case)})
        save(output / "shards" / f"{shard}.json",
             {"schema": 1, "run": run, "shard": shard, "cases": cases})
    save(output / "manifest.json", manifest)
    return run


def prepared_cases(directory):
    directory = Path(directory)
    manifest = load(directory / "manifest.json")
    run = manifest["run"]
    sha(run["release_sha"])
    repository(run["repository"])
    limits = Limits(**run["limits"])
    cases = manifest["cases"]
    total = run["total"]
    if type(total) is not int or total not in ENTRY_COUNTS:
        raise Abstain("Invalid benchmark entry count")
    shards = math.ceil(total / SHARD_SIZE)
    if (manifest.get("schema") != 1 or run["shards"] != shards
            or limits.minimize_assumptions is not False or len(cases) != total
            or len({case["id"] for case in cases}) != total
            or len({case["entry_id"] for case in cases}) != total
            or any(case["label"] not in LABELS for case in cases)
            or (total == TOTAL
                and Counter(case["label"] for case in cases) != {"correct": 103, "incorrect": 103})):
        raise Abstain("Invalid benchmark manifest")
    expected = {case["id"]: case for case in cases}
    inputs = {}
    for shard in range(shards):
        payload = load(directory / "shards" / f"{shard}.json")
        if (payload.get("schema") != 1 or payload["run"] != run or payload["shard"] != shard
                or len(payload["cases"]) != min(SHARD_SIZE, total - shard * SHARD_SIZE)):
            raise Abstain("Benchmark shard disagrees with its frozen run")
        for case in payload["cases"]:
            key = case["id"]
            if not isinstance(key, str) or not CASE_ID.fullmatch(key) or key not in expected or key in inputs:
                raise Abstain("Unknown or repeated benchmark case")
            if expected[key]["shard"] != shard or expected[key]["input_sha256"] != digest(case):
                raise Abstain("Benchmark input hash or shard changed")
            inputs[key] = case
    if set(inputs) != set(expected):
        raise Abstain("Prepared benchmark cases are incomplete")
    return manifest, inputs


def select(directory, shard, output, release, model, sweep):
    manifest, _ = prepared_cases(directory)
    run = manifest["run"]
    if (run["release_sha"], run["model"], run["sweep_id"]) != (release, model, sweep):
        raise Abstain("Benchmark source/model/sweep changed after preparation")
    if type(shard) is not int or not 0 <= shard < run["shards"]:
        raise Abstain("Invalid benchmark shard")
    save(output, load(Path(directory) / "shards" / f"{shard}.json"))


def prediction(report, case):
    """Revalidate native acceptance; a failed verifier is never an incorrect label."""
    if report.get("completed") is not True or report["errors"]:
        raise Abstain("Benchmark audit did not complete")
    bundle = case["bundle"]
    claims = select_properties(case["properties"], bundle)
    if (len(claims) != 1 or report["identity"] != bundle["identity"]
            or len(report["properties"]) != 1 or report["properties"][0]["id"] != claims[0]["id"]):
        raise Abstain("Benchmark audit changed its whole supplied property")
    status = report["properties"][0]["status"]
    findings, proofs = report["findings"], report["verified"]
    if status == VERIFIED:
        if len(proofs) != 1 or findings or proofs[0]["candidate"] != claims[0]:
            raise Abstain("Verification does not match the benchmark property")
        validate_verification(proofs[0], bundle)
        return "correct"
    if status == REFUTED:
        if len(findings) != 1 or proofs or findings[0]["candidate"] != claims[0]:
            raise Abstain("Refutation does not match the benchmark property")
        finding = findings[0]
        if (finding["retained_assumptions"] != list(range(len(finding["translation"]["assumptions"])))
                or finding["minimization"]):
            raise Abstain("Benchmark refutation unexpectedly minimized its assumptions")
        validate_finding(finding, bundle)
        return "incorrect"
    if status not in (EXHAUSTED, INVALID) or findings or proofs:
        raise Abstain("Unexpected benchmark audit outcome")
    return "abstain"


def worker(payload_path, output):
    env = os.environ
    if (env.get("GITHUB_AW") != "true" or env.get("AWF_REFLECT_ENABLED") != "1"
            or not env.get("COPILOT_SDK_URI") or not env.get("COPILOT_CONNECTION_TOKEN")
            or not env.get("COPILOT_PROVIDER_BASE_URL") or not env.get("COPILOT_PROVIDER_TYPE")):
        raise Abstain("Benchmark inference requires the gh-aw AWF SDK runtime")
    if any(env.get(key) for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "NFV_RELEASE_TOKEN")):
        raise Abstain("Do not expose GitHub credentials to the benchmark driver")
    payload = load(payload_path)
    run = payload["run"]
    limits = Limits(**run["limits"])
    if limits.minimize_assumptions is not False or env.get("NFV_MODEL") != run["model"]:
        raise Abstain("Benchmark model or no-minimization policy changed")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / "results.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump({"schema": 1, "run": run, "shard": payload["shard"],
                   "completed": False, "rows": [], "errors": []}, stream)
    result = load(target)
    for case in payload["cases"]:
        if not CASE_ID.fullmatch(case["id"]):
            raise Abstain("Invalid benchmark case ID")
        directory = output / case["id"]
        save(directory / "input.json", case)
        save(directory / "bundle.json", case["bundle"])
        save(directory / "proposals.json", case["properties"])
        env.update(NFV_BUNDLE=str(directory / "bundle.json"), NFV_MODEL=run["model"], NFV_OUT=str(directory))
        started = time.monotonic()
        try:
            audit(directory / "proposals.json", limits, Options(False, False), render_report=False)
            report = load(directory / "report.json")
            if report["limits"] != run["limits"]:
                raise Abstain("Benchmark audit did not use the frozen PR-review budgets")
            label = prediction(report, case)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            result["errors"].append(f"{case['id']}: {exc}")
            save(target, result)
            raise
        result["rows"].append({"id": case["id"], "prediction": label,
                               "status": report["properties"][0]["status"],
                               "elapsed_seconds": time.monotonic() - started})
        save(target, result)
        print(json.dumps({"event": "benchmark_case_completed", "id": case["id"],
                          "status": report["properties"][0]["status"]}), flush=True)
    result["completed"] = True
    save(target, result)
    from .sandbox import submit
    submit(output, "benchmark_checkpoint", {})
    return result


def credit_usage(directory, shards):
    """Sum per-response proxy credits, never cumulative counters or cache snapshots."""
    artifacts = sorted(path for path in Path(directory).glob("*-usage") if path.is_dir())
    if len(artifacts) > shards:
        raise Abstain("Too many benchmark credit-accounting artifacts")
    values = {"inference": [], "threat_detection": []}
    complete = dict.fromkeys(values, len(artifacts) == shards)
    seen = set()
    for artifact in artifacts:
        for kind, stage in (("inference", "agent"), ("threat_detection", "detection")):
            path = artifact / stage / "token_usage.jsonl"
            if not path.is_file():
                complete[kind] = False
                continue
            if path.stat().st_size > 12_000_000:
                raise Abstain("Credit-accounting ledger exceeds 12 MB")
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                record = json.loads(line)
                key = record.get("request_id")
                if record.get("event") != "token_usage" or not isinstance(key, str) or not key or key in seen:
                    raise Abstain("Unknown or duplicate credit-accounting request")
                seen.add(key)
                value = record.get("ai_credits_this_response")
                if value is None:
                    complete[kind] = False
                elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise Abstain("Invalid recorded AI-credit value")
                else:
                    values[kind].append(value)
    subtotals = {kind: math.fsum(amounts) for kind, amounts in values.items()}
    return {"source": "gh-aw per-response proxy usage ledgers",
            "shards_observed": len(artifacts), "shards_expected": shards,
            "complete": all(complete.values()),
            **{kind: subtotals[kind] if complete[kind] else None for kind in values},
            "total": math.fsum(subtotals.values()) if all(complete.values()) else None,
            "recorded_subtotal": math.fsum(subtotals.values())}


def account(summary_path, usage, output):
    summary = load(summary_path)
    updated = {**summary, "ai_credits": credit_usage(usage, summary["run"]["shards"])}
    save(Path(output) / "summary.json", updated)
    save(Path(output) / "accounting-source.json",
         {"run": summary["run"], "summary_sha256": hashlib.sha256(Path(summary_path).read_bytes()).hexdigest()})
    (Path(output) / "report.md").write_text(render(updated), encoding="utf-8", newline="\n")
    return updated


def summarize(prepared, evidence, output, workers_succeeded=True, usage_directory=None, *, fallback=None):
    manifest, inputs = prepared_cases(prepared)
    run = manifest["run"]
    total, shards = run["total"], run["shards"]
    expected = {case["id"]: case for case in manifest["cases"]}
    rows, seen_shards, errors = {}, set(), []
    if not workers_succeeded:
        errors.append("A worker workflow failed analysis, detection or its checkpoint handoff")
    usage = {field: 0 for field in USAGE_FIELDS}
    observed_usage = Counter()
    responses = 0
    paths = sorted(Path(evidence).rglob("results.json"))
    primary_count = len(paths)
    if fallback is not None:
        paths.extend(sorted(Path(fallback).glob("*-agent/sandbox/agent/logs/nfv-bench/results.json")))
    checkpoints, seen_backups, recovered = {}, set(), []
    for index, path in enumerate(paths):
        result = load(path)
        shard = result["shard"]
        if (result.get("schema") != 1 or result["run"] != run
                or type(shard) is not int or not 0 <= shard < shards):
            raise Abstain("Foreign or duplicate benchmark shard evidence")
        backup = index >= primary_count
        if backup:
            if shard in seen_backups:
                raise Abstain("Duplicate backup benchmark shard evidence")
            seen_backups.add(shard)
        if shard in seen_shards:
            if not backup:
                raise Abstain("Foreign or duplicate benchmark shard evidence")
            original_path, original = checkpoints[shard]
            if result != original:
                raise Abstain("Conflicting primary and backup benchmark checkpoints")
            for row in result["rows"]:
                for name in ("input.json", "report.json"):
                    if load(path.parent / row["id"] / name) != load(original_path.parent / row["id"] / name):
                        raise Abstain("Conflicting primary and backup benchmark audit evidence")
            continue
        seen_shards.add(shard)
        checkpoints[shard] = (path, result)
        if backup:
            recovered.append(shard)
        errors.extend(str(error)[:1000] for error in result["errors"])
        shard_ids = set()
        for row in result["rows"]:
            key = row["id"]
            if key not in expected or key in rows or expected[key]["shard"] != shard:
                raise Abstain("Unknown, duplicated or misplaced benchmark result")
            shard_ids.add(key)
            directory = path.parent / key
            if load(directory / "input.json") != inputs[key]:
                raise Abstain("Captured benchmark input changed")
            report = load(directory / "report.json")
            if report["limits"] != run["limits"]:
                raise Abstain("Recorded audit budgets disagree with the frozen run")
            label = prediction(report, inputs[key])
            if row["prediction"] != label or row["status"] != report["properties"][0]["status"]:
                raise Abstain("Recorded prediction disagrees with native proof evidence")
            rows[key] = {**expected[key], **row}
        assigned = {key for key, case in expected.items() if case["shard"] == shard}
        if result.get("completed") is not True or shard_ids != assigned:
            errors.append(f"Shard {shard} is incomplete")
        for transcript in path.parent.rglob("model-transcript.json"):
            for response in load(transcript):
                responses += 1
                for field in USAGE_FIELDS:
                    value = response.get(field)
                    if type(value) is int and value >= 0:
                        usage[field] += value
                        observed_usage[field] += 1
    for key, case in expected.items():
        if key not in rows:
            rows[key] = {**case, "prediction": "abstain", "status": "not evaluated"}
    for shard in sorted(set(range(shards)) - seen_shards):
        errors.append(f"Shard {shard} has no result checkpoint")
    matrix = {label: dict.fromkeys(PREDICTIONS, 0) for label in LABELS}
    for row in rows.values():
        matrix[row["label"]][row["prediction"]] += 1
    decisive = sum(row["prediction"] != "abstain" for row in rows.values())
    matched = sum(row["prediction"] == row["label"] for row in rows.values())
    evaluated = sum(row["status"] != "not evaluated" for row in rows.values())
    completed = evaluated == total and len(seen_shards) == shards and not errors
    finished = now()
    elapsed = (datetime.fromisoformat(finished) - datetime.fromisoformat(run["started_at"])).total_seconds()
    summary = {"schema": 1, "run": run, "completed": completed, "finished_at": finished,
               "elapsed_seconds": elapsed,
               "evaluated": evaluated, "not_evaluated": total - evaluated,
               "predictions": {label: sum(row["prediction"] == label for row in rows.values())
                               for label in PREDICTIONS},
               "matrix": matrix, "matched": matched, "decisive": decisive,
               "accuracy": matched / total, "coverage": decisive / total,
               "answered_accuracy": matched / decisive if decisive else None,
               "abstention_reasons": dict(Counter(row["status"] for row in rows.values()
                                                   if row["prediction"] == "abstain")),
               "recorded_usage": {field: value if observed_usage[field] == responses else None
                                  for field, value in usage.items()} if responses else None,
               "recorded_responses": responses,
               "errors": errors, "rows": list(rows.values())}
    if recovered:
        summary["recovered_shards"] = sorted(recovered)
    if usage_directory is not None:
        summary["ai_credits"] = credit_usage(usage_directory, run["shards"])
    output = Path(output)
    save(output / "summary.json", summary)
    (output / "report.md").write_text(render(summary), encoding="utf-8", newline="\n")
    return summary


def render(summary):
    run = summary["run"]
    total = run["total"]
    kind = "five-entry pilot" if total == 5 else "sweep"
    url = f"https://github.com/{run['repository']}/actions/runs/{run['run_id']}"
    answered = summary["answered_accuracy"]
    lines = [f"<!-- nfv-micro-bench:{run['repository']}:{run['run_id']} -->",
             "# nfv-micro-bench " + ("five-entry pilot" if total == 5 else "nightly") + " statistics", "",
             f"**Complete {kind}.**" if summary["completed"] else f"**PARTIAL {kind}; operational failures or unevaluated entries remain.**",
             f"Evaluated **{summary['evaluated']}/{total}** entries. [Run and evidence artifacts]({url}).", "",
             f"NFV `{text(run['release_sha'])}`; model `{text(run['model'])}`; personal PAT billing.",
             f"Dataset SHA-256: `{text(run['dataset_sha256'])}`.",
             f"Started: {text(run['started_at'])}; finished: {text(summary['finished_at'])}.", "",
             f"Elapsed wall time, including worker queueing: **{summary['elapsed_seconds']:.0f} seconds**.", "",
             "| Ground-truth label | Predicted correct | Predicted incorrect | Abstain / not evaluated |",
             "|---|---:|---:|---:|"]
    for label in LABELS:
        cells = summary["matrix"][label]
        lines.append(f"| {label} | {cells['correct']} | {cells['incorrect']} | {cells['abstain']} |")
    lines += ["", f"**Overall accuracy:** {summary['accuracy']:.2%} ({summary['matched']}/{total}).",
              f"**Coverage:** {summary['coverage']:.2%} ({summary['decisive']}/{total}).",
              "**Accuracy among decisive results:** " + ("N/A." if answered is None else f"{answered:.2%}."),
              "", "Verification proof -> correct; otherwise witnessed refutation proof -> incorrect; otherwise abstain.",
              "Repository-context collection, assumption/failure minimization, shorter-property generation and candidate-fix generation are disabled.",
              "This evaluates the shared PR classifier with supplied specifications, not PR intent extraction or the paper pipeline.",
              "Results concern translated models under stated assumptions, not confirmed source bugs or source-correctness proofs. "
              "Ground-truth labels are the original benchmark labels; disagreements require triage.", ""]
    if total == 5:
        lines += ["Five-entry pilot only; this is not a full-dataset accuracy estimate.", ""]
    if summary.get("recovered_shards"):
        lines += [f"Recovered **{len(summary['recovered_shards'])} shard checkpoints** from backup agent artifacts "
                  "without rerunning inference; the same native evidence checks apply.", ""]
    credits = summary.get("ai_credits")
    if credits is not None:
        def amount(value):
            return "unavailable" if value is None else f"{value:.5f}"

        lines += ["**Recorded AI credits (AIC; not an invoice):** "
                  f"total={amount(credits['total'])}; "
                  f"benchmark inference={amount(credits['inference'])}; "
                  f"threat detection={amount(credits['threat_detection'])}.",
                  "Source: gh-aw per-response proxy usage ledgers; cumulative counters and usage-cache snapshots are not summed.", ""]
        if not credits["complete"]:
            lines += [f"**Incomplete credit accounting:** {credits['shards_observed']}/{credits['shards_expected']} "
                      f"shard artifacts; recorded subtotal={amount(credits['recorded_subtotal'])} AIC. "
                      "Missing credits are unavailable, not zero.", ""]
    if summary["abstention_reasons"]:
        lines += ["| Abstention reason | Entries |", "|---|---:|"]
        lines += [f"| {text(reason)} | {count} |" for reason, count in sorted(summary["abstention_reasons"].items())]
    lines += ["", "**Recorded inference usage (not an invoice):** " + (
        ", ".join(f"{key}={value if value is not None else 'unavailable'}"
                  for key, value in summary["recorded_usage"].items())
        if summary["recorded_usage"] is not None else "unavailable."),
        f"Missing entries remain in the denominator of {total}. Runtime and usage may be incomplete after interruption."]
    return "\n".join(lines) + "\n"


def publication_target(api, repo, category):
    repo = repository(repo)
    if api.request(f"repos/{repo}").get("has_discussions") is not True:
        raise Abstain("Enable Discussions in the benchmark host before starting paid inference")
    owner, name = repo.split("/")
    data = api.graphql("""
      query($owner:String!,$name:String!){
        repository(owner:$owner,name:$name){
          id discussionCategories(first:100){nodes{id name}}
        }
      }""", {"owner": owner, "name": name})["repository"]
    matches = [item for item in data["discussionCategories"]["nodes"] if item["name"] == category]
    if len(matches) != 1:
        raise Abstain("Configure an existing NFV_DISCUSSION_CATEGORY before starting paid inference")
    return data["id"], matches[0]["id"]


def publish(summary, api, repo, category):
    if summary["run"]["repository"] != repository(repo):
        raise Abstain("Benchmark publication repository changed")
    repo_id, category_id = publication_target(api, repo, category)
    body = render(summary)
    marker = body.splitlines()[0]
    owner, name = repo.split("/")
    cursor, existing = None, None
    for _ in range(100):
        page = api.graphql("""
          query($owner:String!,$name:String!,$cursor:String){
            repository(owner:$owner,name:$name){
              discussions(first:100,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}){
                nodes{id body author{login __typename} }
                pageInfo{hasNextPage endCursor}
              }
            }
          }""", {"owner": owner, "name": name, "cursor": cursor})["repository"]["discussions"]
        existing = next((item for item in page["nodes"] if marker in item["body"]), None)
        if existing or not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    else:
        raise Abstain("Discussion pagination exhausted; refusing duplicate publication")
    kind = "five-entry pilot" if summary["run"]["total"] == 5 else "nightly benchmark"
    title = ("[PARTIAL] " if not summary["completed"] else "") + (
        f"NFV {kind}: {summary['run']['started_at'][:10]} (run {summary['run']['run_id']})")
    if existing:
        author = existing.get("author") or {}
        if (author.get("__typename") != "Bot"
                or author.get("login") not in ("github-actions", "github-actions[bot]")):
            raise Abstain("Benchmark marker belongs to a non-Actions author; refusing to overwrite")
        return api.graphql("""
          mutation($id:ID!,$title:String!,$body:String!){
            updateDiscussion(input:{discussionId:$id,title:$title,body:$body}){discussion{url}}
          }""", {"id": existing["id"], "title": title, "body": body})["updateDiscussion"]["discussion"]["url"]
    return api.graphql("""
      mutation($repo:ID!,$category:ID!,$title:String!,$body:String!){
        createDiscussion(input:{repositoryId:$repo,categoryId:$category,title:$title,body:$body}){discussion{url}}
      }""", {"repo": repo_id, "category": category_id, "title": title, "body": body})["createDiscussion"]["discussion"]["url"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    for flag in ("dataset", "out", "release-sha", "model", "repository", "run-id", "attempt"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--entries", type=int, choices=ENTRY_COUNTS, default=TOTAL)
    p = commands.add_parser("select")
    for flag in ("prepared", "out", "release-sha", "model", "sweep"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--shard", type=int, required=True)
    p = commands.add_parser("worker")
    p.add_argument("--payload", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("summarize")
    for flag in ("prepared", "evidence", "out"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--workers-succeeded", choices=("true", "false"), default="true")
    p.add_argument("--usage")
    p.add_argument("--fallback", help="Directory containing backup gh-aw agent artifacts")
    p = commands.add_parser("account")
    for flag in ("summary", "usage", "out"):
        p.add_argument("--" + flag, required=True)
    for name in ("check-publication", "publish"):
        p = commands.add_parser(name)
        p.add_argument("--repository", required=True)
        p.add_argument("--category", default="General")
        if name == "publish":
            p.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            run = prepare(args.dataset, args.out, args.release_sha, args.model,
                          args.repository, args.run_id, args.attempt, count=args.entries)
            if os.environ.get("GITHUB_OUTPUT"):
                with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as stream:
                    for field in ("release_sha", "model", "sweep_id"):
                        stream.write(f"{field}={run[field]}\n")
                    stream.write("matrix=" + json.dumps({"shard": list(range(run["shards"]))}) + "\n")
            print(json.dumps(run))
        elif args.command == "select":
            select(args.prepared, args.shard, args.out, args.release_sha, args.model, args.sweep)
        elif args.command == "worker":
            worker(args.payload, args.out)
        elif args.command == "summarize":
            summary = summarize(args.prepared, args.evidence, args.out,
                                args.workers_succeeded == "true", args.usage, fallback=args.fallback)
            print(json.dumps({key: value for key, value in summary.items() if key != "rows"}))
        elif args.command == "account":
            summary = account(args.summary, args.usage, args.out)
            print(json.dumps(summary["ai_credits"]))
        elif args.command == "check-publication":
            publication_target(GitHub(), args.repository, args.category)
        elif args.command == "publish":
            url = publish(load(args.summary), GitHub(), args.repository, args.category)
            save(Path(args.summary).with_name("publication.json"), {"url": url})
            print(url)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"NFV benchmark {args.command} failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
