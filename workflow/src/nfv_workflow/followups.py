# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Checked, optional enrichments; never change whole-property acceptance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import time

from nfv.agent import refute

from . import read_prompt
from .check import Dafny, accepted
from .general import json_answer, reachable, require_tool, save, validate_finding
from .general_gates import (
    BACKEND, audit, clauses, remove_requirement, signature_parameters,
    with_postconditions, witness_bridge,
)
from .github import Abstain
from .outcomes import ResourcesExhausted
from .presentation import digest, validate_report
from .source_context import check_request, scoped_context


@dataclass(frozen=True)
class Options:
    simplify: bool = True
    fixes: bool = True

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        values = {}
        for name, field in (("NFV_SIMPLIFY_REFUTATIONS", "simplify"), ("NFV_CHECK_FIXES", "fixes")):
            raw = env.get(name, "").strip().lower()
            if raw not in ("", "true", "false"):
                raise Abstain(f"{name} must be true or false")
            values[field] = raw != "false"
        return cls(**values)


def sha256(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def positive_model(finding):
    translation = finding["translation"]
    model = translation["model"]
    for index, condition in enumerate(translation["assumptions"]):
        if index not in finding["retained_assumptions"]:
            model = remove_requirement(model, translation["entry_point"], condition["clause"])
    return model


def body_span(model, entry):
    # Keep offsets while masking quoted source braces and string literals.
    code = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])'|//[^\n]*''',
                  lambda match: "".join("\n" if char == "\n" else " " for char in match[0]), model)
    declaration = re.search(rf"(?m)^method\s+{re.escape(entry)}\s*\(", code)
    if declaration is None:
        raise Abstain("Missing repair entry declaration")
    opening = re.search(r"(?m)^[ \t]*\{[ \t]*$", code[declaration.end():])
    if opening is None:
        raise Abstain("Missing canonical entry body")
    start = code.index("{", declaration.end() + opening.start())
    depth = 1
    for index in range(start + 1, len(code)):
        depth += (code[index] == "{") - (code[index] == "}")
        if depth == 0:
            return start + 1, index
    raise Abstain("Unbalanced repair entry body")


def repaired_model(finding, body):
    if not isinstance(body, str) or not body.strip():
        raise Abstain("Repair needs a complete nonempty entry body")
    original = positive_model(finding)
    entry = finding["translation"]["entry_point"]
    first, last = body_span(original, entry)
    model = original[:first] + body + original[last:]
    new_first, new_last = body_span(model, entry)
    if original[:first] != model[:new_first] or original[last:] != model[new_last:]:
        raise Abstain("Repair changed a signature, goal, input domain or helper")
    if any(clauses(model, entry, key) != clauses(original, entry, key)
           for key in ("requires", "ensures")):
        raise Abstain("Repair changed the complete property or input domain")
    audit(model, entry)
    return model


def expression(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4000
            or value.strip() in ("true", "false")
            or any(char in value for char in "\r\n;{}") or "//" in value or "/*" in value
            or re.search(r"\b(?:requires|ensures|method|lemma|assume|axiom|decreases|Nfv\w*)\b", value)):
        raise Abstain("Projection must be a bounded Boolean expression, not declarations or a constant")
    return value


def bounded_text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Abstain("Follow-up requires nonempty bounded source-oriented text")
    return value


def projection(finding, value):
    goal = expression(value["goal"])
    label = bounded_text(value["property"], 1000)
    if len(label) >= len(finding["candidate"]["property"]):
        raise Abstain("Proposed property is not shorter")
    translation = finding["translation"]
    entry = translation["entry_point"]
    base = positive_model(finding)
    inputs, outputs = signature_parameters(base, entry)
    parameters = ", ".join(f"{name}: {typ}" for name, typ in inputs + outputs)
    conditions = "".join(f"  requires {condition}\n" for condition in clauses(base, entry, "requires"))
    full = " && ".join(f"({condition})" for condition in translation["original_ensures"])
    lemma = (f"lemma NfvProjectionCheck({parameters})\n{conditions}"
             f"  ensures ({full}) ==> ({goal})\n{{}}\n")
    negative = with_postconditions(finding["checked_model"], entry, ["!(" + goal + ")"])
    audit(negative, entry)
    driver = witness_bridge(translation["model"], entry, translation["witness"])[1]
    return label, goal, base + "\n" + lemma, negative, negative + "\n" + driver


def source_patch(finding, bundle, value):
    path = finding["candidate"]["path"]
    record = bundle["sources"][path]
    if record.get("line_start", 1) != 1:
        raise Abstain("Source fix requires a complete captured entry file")
    old, new = value["old"], value["new"]
    source = record["text"]
    low, high = finding["translation"]["source_span"]
    entry = "\n".join(source.splitlines()[low - 1:high])
    if (not isinstance(old, str) or not isinstance(new, str) or not old or not new
            or old == new or len(old) + len(new) > 32000
            or source.count(old) != 1 or old not in entry):
        raise Abstain("Source repair must replace one unique captured entry span")
    patched = source.replace(old, new, 1)
    rows = difflib.unified_diff(source.splitlines(keepends=True), patched.splitlines(keepends=True),
                                fromfile="a/" + path, tofile="b/" + path)
    patch = "".join(row if row.endswith("\n") else row + "\n\\ No newline at end of file\n"
                    for row in rows)
    return patch, sha256(source), sha256(patched)


def feasible(result):
    return (result.get("outcome") == "failed" and result.get("exit_code") == 4
            and result.get("mode") == "unbounded")


def check_simplification(finding, value, directory, verifier):
    label, goal, lemma, negative, source = projection(finding, value)
    entry = finding["translation"]["entry_point"]
    entailment = verifier.run(lemma, directory / "entailment", filter_symbol="NfvProjectionCheck")
    require_tool(entailment)
    refutation = verifier(source, directory / "refutation")
    require_tool(refutation)
    domain_ok, domain = reachable(verifier, negative, entry, directory / "input-domain")
    checked = accepted(entailment) and accepted(refutation) and domain_ok is True
    return {"status": "checked" if checked else "not-established",
            "property": label, "goal": goal, "model_sha256": sha256(source),
            "entailment_sha256": sha256(lemma),
            "checks": {"entailment": entailment, "refutation": refutation, "input_domain": domain}}


def check_fix(finding, bundle, value, directory, verifier):
    model = repaired_model(finding, value["body"])
    description = bounded_text(value["description"])
    caveat = bounded_text(value["caveat"])
    patch, original_hash, patched_hash = source_patch(finding, bundle, value)
    entry = finding["translation"]["entry_point"]
    full = verifier(model, directory / "full-property")
    require_tool(full)
    domain_ok, domain = reachable(verifier, model, entry, directory / "input-domain")
    pins, driver = witness_bridge(model, entry, finding["translation"]["witness"])
    pinned = refute.install_precondition(model, entry, pins, BACKEND)
    witness = verifier(pinned + "\n" + driver, directory / "witness")
    require_tool(witness)
    witness_ok, witness_domain = reachable(verifier, pinned, entry, directory / "witness-input-domain")
    checked = accepted(full) and domain_ok is True and accepted(witness) and witness_ok is True
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "candidate.patch").write_text(patch, encoding="utf-8", newline="\n")
    return {"status": "checked" if checked else "not-established", "description": description,
            "caveat": caveat, "body": value["body"], "old": value["old"], "new": value["new"],
            "patch": patch, "patch_sha256": sha256(patch), "model_sha256": sha256(model),
            "source_sha256": original_hash, "patched_source_sha256": patched_hash,
            "checks": {"full_property": full, "input_domain": domain,
                       "witness": witness, "witness_input_domain": witness_domain}}


def validate_artifact(artifact, report, bundle):
    validate_report(report, bundle)
    if (not isinstance(artifact, dict)
            or artifact.get("schema") != 1 or artifact.get("kind") != "refutation-followups"
            or artifact.get("completed") is not True
            or artifact.get("report_sha256") != digest(report)
            or artifact.get("bundle_sha256") != digest(bundle)):
        raise Abstain("Follow-up evidence is incomplete or bound to a different report/source")
    findings = {item["candidate"]["id"]: item for item in report["findings"]}
    if not isinstance(artifact.get("findings"), dict) or set(artifact["findings"]) != set(findings):
        raise Abstain("Follow-up evidence must cover the accepted findings exactly")
    options = artifact.get("options")
    if (not isinstance(options, dict) or set(options) != {"simplify", "fixes"}
            or any(type(value) is not bool for value in options.values())):
        raise Abstain("Invalid follow-up feature flags")
    calls = artifact.get("calls")
    if (not isinstance(calls, list) or len(calls) > 2 * len(findings)
            or any(not isinstance(call, dict) or call.get("finding_id") not in findings
                   or call.get("attempt") not in (1, 2)
                   or ("proposals" in call and not isinstance(call["proposals"], dict))
                   or re.fullmatch(r"[0-9a-f]{64}", str(call.get("response_sha256", ""))) is None
                   for call in calls)):
        raise Abstain("Invalid follow-up proposal provenance")
    if len({(call["finding_id"], call["attempt"]) for call in calls}) != len(calls):
        raise Abstain("Repeated follow-up proposal provenance")
    for identity, result in artifact["findings"].items():
        finding = findings[identity]
        if not isinstance(result, dict):
            raise Abstain("Invalid per-finding follow-up evidence")
        for key in ("simplification", "fix"):
            if (not isinstance(result.get(key), dict)
                    or result[key].get("status") not in ("disabled", "not-established", "checked")):
                raise Abstain("Unknown follow-up status")
            enabled = options["simplify" if key == "simplification" else "fixes"]
            if (result[key]["status"] == "disabled") == enabled:
                raise Abstain("Follow-up status disagrees with its feature flag")
        simple, fix = result["simplification"], result["fix"]
        for key, value in (("simplification", simple), ("fix", fix)):
            if value["status"] != "checked":
                continue
            names = (("property", "goal") if key == "simplification" else
                     ("body", "old", "new", "description", "caveat"))
            if any(name not in value for name in names):
                raise Abstain("Checked follow-up is missing its proposal")
            proposal_hash = digest({name: value[name] for name in names})
            if not any(call.get("finding_id") == identity and call.get("attempt") == value.get("attempt")
                       and call.get("proposals", {}).get(key) == proposal_hash for call in calls):
                raise Abstain("Checked follow-up differs from its captured proposal")
            check_names = ({"entailment", "refutation", "input_domain"} if key == "simplification" else
                           {"full_property", "input_domain", "witness", "witness_input_domain"})
            if (not isinstance(value.get("checks"), dict) or set(value["checks"]) != check_names
                    or any(not isinstance(item, dict) for item in value["checks"].values())):
                raise Abstain("Checked follow-up is missing verifier evidence")
        if simple["status"] == "checked":
            _, _, lemma, _, source = projection(finding, simple)
            checks = simple["checks"]
            if (sha256(source) != simple.get("model_sha256") or sha256(lemma) != simple.get("entailment_sha256")
                    or not accepted(checks["entailment"])
                    or not accepted(checks["refutation"]) or not feasible(checks["input_domain"])):
                raise Abstain("Shorter property lacks bound entailment/refutation/input-domain evidence")
        if fix["status"] == "checked":
            model = repaired_model(finding, fix["body"])
            patch, old_hash, new_hash = source_patch(finding, bundle, fix)
            bounded_text(fix["description"])
            bounded_text(fix["caveat"])
            checks = fix["checks"]
            if (sha256(model) != fix.get("model_sha256") or patch != fix.get("patch")
                    or sha256(patch) != fix.get("patch_sha256") or old_hash != fix.get("source_sha256")
                    or new_hash != fix.get("patched_source_sha256")
                    or not accepted(checks["full_property"]) or not accepted(checks["witness"])
                    or not feasible(checks["input_domain"]) or not feasible(checks["witness_input_domain"])):
                raise Abstain("Fix lacks unchanged whole-property and non-vacuous witness evidence")
    return artifact["findings"]


async def run(report, bundle, chat, output, limits, options=None, verifier=None):
    options = options or Options()
    validate_report(report, bundle)
    before = digest(report), digest(bundle)
    output = Path(output)
    artifact = {"schema": 1, "kind": "refutation-followups", "completed": False,
                "report_sha256": before[0], "bundle_sha256": before[1],
                "options": asdict(options), "findings": {}, "calls": []}
    target = output / "followups.json"
    system = read_prompt("followup_prompt.txt")
    for finding in report["findings"]:
        validate_finding(finding, bundle)
        identity = finding["candidate"]["id"]
        result = {
            "simplification": {"status": "not-established" if options.simplify else "disabled"},
            "fix": {"status": "not-established" if options.fixes else "disabled"},
            "attempts": [],
        }
        artifact["findings"][identity] = result
        save(target, artifact)
        if not (options.simplify or options.fixes):
            continue
        started = time.monotonic()
        context = {
            "claim": finding["candidate"], "model": positive_model(finding),
            "original_ensures": finding["translation"]["original_ensures"],
            "witness": finding["translation"]["witness"],
            "source_span": finding["translation"]["source_span"],
            "source_context": scoped_context(bundle, finding["candidate"]),
            "enabled": asdict(options),
        }
        for attempt in range(2):
            directory = output / "followups" / identity / str(attempt + 1)
            try:
                remaining = min(600, limits.entry_seconds) - (time.monotonic() - started)
                if remaining <= 0:
                    raise ResourcesExhausted("Follow-up wall-time budget exhausted")
                prompt = json.dumps(context, ensure_ascii=False)
                check_request(system, prompt, "refutation follow-up")
                response = await chat.ask(system, prompt, timeout=min(limits.model_timeout, remaining))
                call = {"finding_id": identity, "attempt": attempt + 1,
                        "response_sha256": sha256(response.text)}
                for field in ("model", "api_calls", "nano_aiu", "input_tokens", "output_tokens"):
                    if hasattr(response, field):
                        call[field] = getattr(response, field)
                artifact["calls"].append(call)
                candidate = json_answer(response.text)
                if not isinstance(candidate, dict):
                    raise Abstain("Follow-up proposal must be a JSON object")
                call["proposals"] = {
                    "simplification": digest({name: candidate["simplification"].get(name)
                                              for name in ("property", "goal")})
                    if isinstance(candidate.get("simplification"), dict) else None,
                    "fix": digest({name: candidate["fix"].get(name)
                                   for name in ("body", "old", "new", "description", "caveat")})
                    if isinstance(candidate.get("fix"), dict) else None,
                }
                save(directory / "proposal.json", candidate)
            except (Abstain, ValueError, KeyError, TypeError, TimeoutError, ResourcesExhausted) as exc:
                diagnostic = str(exc) or type(exc).__name__
                result["attempts"].append({"attempt": attempt + 1, "diagnostic": diagnostic})
                context["feedback"] = diagnostic[:2000]
                save(target, artifact)
                continue
            feedback = {}
            for key, enabled, check in (
                ("simplification", options.simplify, check_simplification),
                ("fix", options.fixes, check_fix),
            ):
                if not enabled or result[key]["status"] == "checked":
                    continue
                try:
                    remaining = min(600, limits.entry_seconds) - (time.monotonic() - started)
                    operations = 4 if key == "fix" else 3
                    if remaining <= operations * 21:
                        raise ResourcesExhausted("Follow-up wall-time budget exhausted")
                    # Each verifier worker has up to 20 seconds of process overhead.
                    active = verifier or Dafny(min(limits.verifier_timeout, int(remaining / operations) - 20))
                    value = candidate.get(key)
                    if not isinstance(value, dict):
                        raise Abstain(f"No {key} candidate was supplied")
                    if key == "fix":
                        checked = check(finding, bundle, value, directory / key, active)
                    else:
                        checked = check(finding, value, directory / key, active)
                    checked["attempt"] = attempt + 1
                    result[key] = checked
                    if checked["status"] != "checked":
                        feedback[key] = {name: str(check_result.get("stderr") or check_result.get("stdout")
                                                  or check_result.get("outcome"))[:1500]
                                         for name, check_result in checked["checks"].items()}
                except (Abstain, ValueError, KeyError, TypeError, ResourcesExhausted) as exc:
                    result[key] = {"status": "not-established", "diagnostic": str(exc)}
                    feedback[key] = str(exc)[:2000]
            result["attempts"].append({"attempt": attempt + 1, "diagnostics": feedback})
            context["feedback"] = feedback
            context["enabled"] = {"simplify": options.simplify and result["simplification"]["status"] != "checked",
                                  "fixes": options.fixes and result["fix"]["status"] != "checked"}
            save(target, artifact)
            if not feedback:
                break
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save(target, artifact)
    if before != (digest(report), digest(bundle)):
        raise Abstain("Follow-up changed accepted proof evidence or captured source")
    artifact["completed"] = True
    validate_artifact(artifact, report, bundle)
    save(target, artifact)
    return artifact
