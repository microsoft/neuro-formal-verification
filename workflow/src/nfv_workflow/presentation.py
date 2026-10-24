"""Agent-authored, source-anchored report metadata; never verification evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.resources
import json
import re

from .general_gates import anchor, signature_parameters
from .github import Abstain, sha


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bounded_text(value, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Abstain("Presentation requires nonempty bounded text")
    return value


def anchors(value, bundle):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise Abstain("Presentation requires 1..12 captured source anchors")
    excerpts = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "line", "excerpt"}:
            raise Abstain("Invalid presentation source anchor")
        if type(item["line"]) is not int:
            raise Abstain("Invalid presentation source line")
        excerpt = anchor(bundle, item["path"], item["line"])
        if item["excerpt"] != excerpt:
            raise Abstain("Presentation source excerpt differs from captured snapshot")
        excerpts.append(excerpt)
    return excerpts


def validate(value, translation, bundle):
    """Check coverage and exact source expressions, not semantic equivalence."""
    if (not isinstance(value, dict) or set(value) != {"inputs", "failure", "explanation"}
            or len(json.dumps(value)) > 40000):
        raise Abstain("Invalid or excessive source presentation")
    inputs = value["inputs"]
    if not isinstance(inputs, dict) or set(inputs) != set(translation["witness"]):
        raise Abstain("Presentation must cover every witness key exactly once")
    for key, item in inputs.items():
        if not isinstance(item, dict):
            raise Abstain("Invalid presentation input")
        if item.get("kind") in ("expression", "per-element") and set(item) == {"kind", "expression", "anchors"}:
            if item["kind"] == "per-element" and not isinstance(translation["witness"][key], list):
                raise Abstain("Per-element presentation requires a sequence witness")
            expression = bounded_text(item["expression"], 500)
            if not any(expression in line for line in anchors(item["anchors"], bundle)):
                raise Abstain("Presentation expression is not in its captured source anchors")
        elif item.get("kind") == "abstract" and set(item) == {"kind", "label", "reason"}:
            bounded_text(item["label"], 500)
            bounded_text(item["reason"], 1000)
        else:
            raise Abstain("Input needs an anchored expression/per-element mapping or explicit abstract-input reason")
    failure = value["failure"]
    if not isinstance(failure, dict) or set(failure) != {"expression", "anchors"}:
        raise Abstain("Presentation needs an anchored source-level property failure")
    expression = bounded_text(failure["expression"], 500)
    if not any(expression in line for line in anchors(failure["anchors"], bundle)):
        raise Abstain("Failure expression is not in its captured source anchors")
    explanation = bounded_text(value["explanation"])
    if expression not in explanation:
        raise Abstain("Explanation must refer to the mapped source failure expression")
    # Reject model-only witness, result and helper identifiers in visible prose.
    inputs, outputs = signature_parameters(translation["model"], translation["entry_point"])
    names = {name for name, _ in inputs + outputs} | {
        item["name"] for item in translation.get("environment", [])}
    source_names = set(re.findall(r"\b[A-Za-z_]\w*\b", "\n".join(
        record["text"] for record in bundle["sources"].values())))
    visible = explanation + " " + " ".join(
        item.get("expression", item.get("label", "")) + " " + item.get("reason", "")
        for item in value["inputs"].values())
    if (names - source_names) & set(re.findall(r"\b[A-Za-z_]\w*\b", visible)):
        raise Abstain("Presentation exposes model-only predicates or result names")
    return value


def validate_report(report, bundle):
    from .general import validate_finding, validate_verification

    if (report.get("completed") is False or report.get("identity") != bundle["identity"] or report.get("errors")
            or report.get("mode") not in ("general-model-refutation", "general-model-analysis")
            or not 1 <= len(report.get("findings", [])) <= 10):
        raise Abstain("Presentation refresh requires an accepted general report and matching bundle")
    sha(bundle["identity"]["source_sha"])
    for evidence in report.get("verified", []):
        validate_verification(evidence, bundle)
    ids = []
    for finding in report["findings"]:
        if finding.get("kind") != "conditional-model-refutation":
            raise Abstain("Presentation refresh supports general findings only")
        validate_finding(finding, bundle)
        ids.append(finding["candidate"]["id"])
    if len(set(ids)) != len(ids):
        raise Abstain("Duplicate presentation finding identities")


def validate_artifact(artifact, report, bundle):
    validate_report(report, bundle)
    if (not isinstance(artifact, dict)
            or set(artifact) != {"schema", "kind", "report_sha256", "bundle_sha256", "findings", "provenance"}
            or artifact["schema"] != 1 or artifact["kind"] != "source-presentation"
            or artifact["report_sha256"] != digest(report) or artifact["bundle_sha256"] != digest(bundle)):
        raise Abstain("Presentation artifact does not match the saved report and source bundle")
    expected = {finding["candidate"]["id"] for finding in report["findings"]}
    if not isinstance(artifact["findings"], dict) or set(artifact["findings"]) != expected:
        raise Abstain("Presentation artifact must cover every accepted finding")
    provenance = artifact["provenance"]
    if (not isinstance(provenance, dict) or set(provenance) != {"method", "calls"}
            or provenance["method"] != "tool-free-agent"
            or not isinstance(provenance["calls"], list) or len(provenance["calls"]) != len(expected)):
        raise Abstain("Invalid presentation agent provenance")
    calls = provenance["calls"]
    if any(not isinstance(call, dict) for call in calls):
        raise Abstain("Invalid presentation call provenance")
    if {call.get("finding_id") for call in calls} != expected:
        raise Abstain("Presentation call provenance differs from accepted findings")
    for call in calls:
        if call.get("presentation_sha256") != digest(artifact["findings"][call["finding_id"]]):
            raise Abstain("Presentation metadata differs from the recorded agent output")
    for finding in report["findings"]:
        validate(artifact["findings"][finding["candidate"]["id"]], finding["translation"], bundle)
    return artifact["findings"]


def source_windows(finding, bundle):
    from .general import numbered

    translation, claim = finding["translation"], finding["candidate"]
    selected = {claim["path"]: set(range(translation["source_span"][0],
                                        translation["source_span"][1] + 1))}
    for row in finding["source_map"]:
        selected.setdefault(row["path"], set()).add(row["line"])
    for helper in translation.get("environment", []):
        selected.setdefault(helper["source"]["path"], set()).update(helper["source"]["lines"])
    for note in translation.get("source_notes", []):
        selected.setdefault(note["path"], set()).add(note["line"])
    sources = {}
    for path, anchors in sorted(selected.items()):
        record = bundle["sources"][path]
        rows = numbered(record)["numbered_lines"].splitlines()
        first = record.get("line_start", 1)
        nearby = {line + offset for line in anchors for offset in range(-2, 3)}
        sources[path] = {"blob_sha": record.get("blob_sha"), "numbered_lines": "\n".join(
            rows[line - first] for line in sorted(nearby) if first <= line < first + len(rows))}
    return sources


async def refresh(report, bundle, chat, timeout=240):
    """One bounded tool-free call per accepted finding; no repairs or proof reruns."""
    from .general import json_answer
    from .source_context import check_request

    validate_report(report, bundle)
    if not 1 <= timeout <= 600:
        raise Abstain("Presentation timeout must be 1..600 seconds")
    system = importlib.resources.files("nfv_workflow").joinpath("presentation_prompt.txt").read_text()
    results, calls = {}, []
    for finding in report["findings"]:
        translation = finding["translation"]
        sources = source_windows(finding, bundle)
        context = {
            "identity": bundle["identity"], "claim": finding["candidate"],
            "translation": {key: value for key, value in translation.items() if key != "presentation"},
            "source_map": finding["source_map"],
            "retained_assumptions": finding["retained_assumptions"],
            "sources": sources,
            "source_scope": "Captured entry and accepted source anchors with nearby rows; unrelated file lines omitted.",
            "context_omissions": [f"{path}: not used by the checked source mapping"
                                 for path in translation["required_context"] if path not in sources],
        }
        prompt = json.dumps(context)
        check_request(system, prompt, "Source presentation")
        response = await chat.ask(system, prompt, timeout=timeout)
        value = json_answer(response.text)
        validate(value, translation, bundle)
        identity = finding["candidate"]["id"]
        results[identity] = copy.deepcopy(value)
        call = {"finding_id": identity, "presentation_sha256": digest(value)}
        for name in ("model", "input_tokens", "output_tokens", "api_calls", "nano_aiu"):
            if hasattr(response, name):
                call[name] = getattr(response, name)
        calls.append(call)
    artifact = {"schema": 1, "kind": "source-presentation",
                "report_sha256": digest(report), "bundle_sha256": digest(bundle),
                "findings": results, "provenance": {"method": "tool-free-agent", "calls": calls}}
    validate_artifact(artifact, report, bundle)
    return artifact
