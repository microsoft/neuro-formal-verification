"""Multi-language, conditional MODEL refutation using the retained NFV backend."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .check import Dafny, accepted
from .general_gates import (BACKEND, annotate, clean_code, final_gate,
                            remove_requirement, validate_translation, witness_bridge)
from .github import Abstain
from .model import normalize, validate_claim

LANGUAGES = {
    ".c": "c", ".h": "cpp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".java": "java", ".cs": "csharp", ".go": "go",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".py": "python",
}
ALIASES = {"c++": "cpp", "c#": "csharp", "js": "javascript", "ts": "typescript", "py": "python"}
LANGUAGE_GUIDES = {
    "c": "Preserve integer widths/promotions, signed-overflow undefined behavior versus unsigned wrapping, "
         "division truncation toward zero, pointer validity/lifetimes, aliasing, and evaluation order. "
         "State concrete defined-behavior conditions; do not turn undefined behavior into a confirmed failure.",
    "cpp": "Preserve C++ integer widths/promotions, signed-overflow undefined behavior, unsigned wrapping, "
           "truncating division, object/reference identity and lifetimes, RAII, exceptions, virtual dispatch, "
           "and library preconditions. Unknown overloads/templates/callees need explicit context or abstention.",
    "java": "Preserve Java int/long wrapping and shifts, truncating division, null and bounds exceptions, "
            "reference identity, and evaluation order. Do not silently replace JVM arithmetic by "
            "unbounded integers or exceptional execution by an ordinary return.",
    "csharp": "Preserve C# checked versus unchecked arithmetic, fixed integer widths, truncating division, "
              "null/bounds exceptions, value versus reference types, and evaluation order. Type/nullability "
              "annotations are not runtime assumptions unless justified by the modeled input domain.",
    "go": "Record the assumed Go int width; preserve fixed-width wrapping, truncating division, "
          "slice backing-array aliasing, nil/bounds panics, and multiple results. Do not model concurrent "
          "goroutine behavior as sequential without an explicit justified environment restriction.",
    "javascript": "JavaScript Number is IEEE-754, not mathematical int: preserve NaN/infinity/-0, coercion, "
                  "== versus ===, evaluation order, undefined, sparse arrays, and object identity. An "
                  "integer-only abstraction needs explicit input/operation restrictions. Unknown dynamic "
                  "effects and external APIs require disclosed models or abstention.",
    "typescript": "TypeScript types are erased; preserve JavaScript runtime Number/NaN/coercion/undefined "
                  "and object/array semantics. Do not treat casts, interfaces, readonly, or static "
                  "nullability as runtime guarantees. State and check any integer-only abstraction domain.",
    "python": "Preserve Python arbitrary-precision integers, floor // and modulo versus floating /, "
              "truthiness, mutable object aliasing, negative indexing/slices, exceptions, and dynamic "
              "types. Do not replace a dynamic input type case or library behavior by an unrecorded assumption.",
}


class ToolFailure(RuntimeError):
    """Infrastructure failure, not a refutation or a syntax-repair opportunity."""


def require_tool(result):
    if result.get("outcome") == "tool_error":
        raise ToolFailure("Dafny execution failed: " + str(result.get("stderr", ""))[-2000:])


@dataclass(frozen=True)
class Limits:
    syntax_rounds: int = 6
    proof_rounds: int = 10
    verifier_timeout: int = 60
    model_timeout: int = 240
    entry_seconds: int = 1800

    def __post_init__(self):
        if not 0 <= self.syntax_rounds <= 20 or not 0 <= self.proof_rounds <= 30:
            raise ValueError("Repair limits must be syntax 0..20 and proof 0..30")
        if min(self.verifier_timeout, self.model_timeout, self.entry_seconds) <= 0:
            raise ValueError("Timeouts must be positive")


def select_properties(raw, bundle):
    if not isinstance(raw, list) or len(raw) > 100:
        raise Abstain("Expected at most 100 extracted intended properties")
    selected, seen = [], set()
    for value in raw:
        path = validate_claim(value, bundle)
        language = str(value.get("language") or LANGUAGES.get(Path(path).suffix.lower(), "")).lower()
        language = ALIASES.get(language, language)
        expected = LANGUAGES.get(Path(path).suffix.lower())
        if language not in set(LANGUAGES.values()) or (expected != language and not (
                Path(path).suffix.lower() == ".h" and language in ("c", "cpp"))):
            raise Abstain("Unsupported or mismatched source language")
        key = (path, normalize(value["property"]))
        if key in seen:
            continue
        seen.add(key)
        claim = {key: value[key] for key in ("path", "lines", "signal_id", "quote", "property")}
        claim["language"] = language
        claim["id"] = hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()[:20]
        selected.append(claim)
        if len(selected) == 10:
            break
    return selected


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def json_answer(text):
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    if len(text) > 300000:
        raise Abstain("Model response exceeds the artifact budget")
    result = json.loads(text)
    if isinstance(result, dict) and result.get("abstain"):
        raise Abstain(str(result["abstain"]))
    return result


def reachable(verifier, model, entry, directory):
    from nfv.agent import refute
    # The retained probe builder counts braces textually; source excerpts in
    # provenance comments (e.g. `if (...) {`) must not unbalance its body scan.
    probe = refute._probe_source(clean_code(model), entry, BACKEND)
    if not probe:
        raise Abstain("Could not construct retained NFV reachability probe")
    result = verifier.run(model + probe, directory, filter_symbol=refute.REACH_PROBE)
    require_tool(result)
    return (result.get("outcome") == "failed" and result.get("exit_code") == 4
            and result.get("mode") == "unbounded"), result


async def run_property(claim, bundle, chat, directory, limits, verifier):
    from nfv.agent import refute
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    transcript = []
    counters = {"translation_calls": 0, "syntax_repairs": 0, "proof_repairs": 0}
    system = importlib.resources.files("nfv_workflow").joinpath("general_prompt.txt").read_text()

    def budget():
        remaining = limits.entry_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise Abstain("Per-property wall-time budget exhausted")
        return remaining

    async def ask(stage, prompt):
        remaining = budget()
        response = await chat.ask(system, prompt, timeout=min(limits.model_timeout, remaining))
        record = {"stage": stage, "response": response.text}
        for field in ("model", "input_tokens", "output_tokens", "api_calls", "nano_aiu"):
            if hasattr(response, field):
                record[field] = getattr(response, field)
        transcript.append(record)
        save(directory / "model-transcript.json", transcript)
        return json_answer(response.text)

    context = {
        "identity": bundle["identity"], "claim": claim, "sources": {},
        "language_semantics": LANGUAGE_GUIDES[claim["language"]],
        "declarations_and_preconditions": bundle.get("declarations_and_preconditions", []),
        "callee_names": bundle.get("callee_names", []),
        "context_omissions": list(bundle.get("context_omissions", [])),
    }
    # The claim file always comes first; other sources keep the collector's nearest-first order
    # and are dropped (and disclosed) once the per-property budget would be exceeded.
    size = len(json.dumps(context))
    for path in [claim["path"]] + [p for p in bundle["sources"] if p != claim["path"]]:
        record = bundle["sources"][path]
        cost = len(json.dumps({path: record})) + 2
        if path != claim["path"] and size + cost > 560000:
            context["context_omissions"].append(f"{path}: omitted by per-property context budget")
            continue
        context["sources"][path] = record
        size += cost
    encoded_context = json.dumps(context)
    if len(encoded_context) > 600000:
        raise Abstain("Captured context exceeds the per-property model context budget")
    counters["translation_calls"] += 1
    translation = await ask("translation", "Translate this intended property. No triage or original-property proof.\n" + encoded_context)
    if not isinstance(translation, dict):
        raise Abstain("Translation must be a structured artifact")
    current = translation["model"]
    type_log = []
    for attempt in range(limits.syntax_rounds + 1):
        budget()
        mapped = validate_translation(translation, claim, bundle, current)
        result = verifier.resolve(current, directory / f"syntax-{attempt}")
        require_tool(result)
        type_log.append(result)
        if result.get("outcome") == "verified" and result.get("exit_code") == 0 and result.get("mode") == "typecheck":
            break
        if attempt == limits.syntax_rounds:
            raise Abstain(f"Type checking failed after {limits.syntax_rounds} syntax repairs")
        counters["syntax_repairs"] += 1
        repair = await ask("syntax-repair", json.dumps({
            "instruction": "Fix only Dafny syntax/type errors. Return ONLY {\"model\":\"full tagged Dafny\"}. "
                           "Entry, original ensures, witness, and all metadata/contracts are immutable.",
            "source_context": context, "metadata": {k: v for k, v in translation.items() if k != "model"},
            "current_model": current, "typecheck": result,
        }))
        if not isinstance(repair, dict) or set(repair) != {"model"}:
            raise Abstain("Syntax repair attempted to replace immutable translation metadata")
        current = repair["model"]
    original = current
    translation = {**translation, "model": original}
    save(directory / "translation.json", translation)
    inverse = refute.invert_entry_spec(original, translation["entry_point"], BACKEND)
    if not inverse.ok:
        raise Abstain("Cannot mechanically negate original property: " + inverse.reason)
    pins, driver = witness_bridge(original, translation["entry_point"], translation["witness"])
    frozen = refute.install_precondition(inverse.model, translation["entry_point"], pins, BACKEND)
    retained = list(range(len(translation["assumptions"])))
    plan, proof_log = [], []
    for attempt in range(limits.proof_rounds + 1):
        budget()
        current = annotate(frozen, plan)
        gates = final_gate(original, current, translation, retained, pins)
        result = verifier(current + "\n" + driver, directory / f"proof-{attempt}")
        require_tool(result)
        proof_log.append(result)
        if accepted(result):
            break
        if attempt == limits.proof_rounds:
            raise Abstain(f"Negative check failed after {limits.proof_rounds} proof repairs")
        counters["proof_repairs"] += 1
        repair = await ask("proof-repair", json.dumps({
            "instruction": "Return ONLY {\"annotations\":[{\"after\":FROZEN_LINE_NUMBER,\"text\":\"invariant ...\"}]}. "
                           "The list REPLACES the previous annotation plan. Only single-line assert (ending ;), "
                           "invariant, and decreases annotations are permitted. No computation, contracts, "
                           "goal, witness, declarations, or original source changes. No full-model response.",
            "source_context": context, "frozen_model_lines": [
                f"{i}: {line}" for i, line in enumerate(frozen.splitlines(), 1)],
            "previous_plan": plan, "negative_check": result,
        }))
        if not isinstance(repair, dict) or set(repair) != {"annotations"}:
            raise Abstain("Proof repair attempted to replace frozen computation or metadata")
        plan = repair["annotations"]
    budget()
    reach_ok, reach_result = reachable(verifier, current, translation["entry_point"], directory / "reach-initial")
    if not reach_ok:
        raise Abstain("Initial reachability gate did not establish a nonvacuous returning case")
    minimization = []
    for index, condition in enumerate(translation["assumptions"]):
        if not condition["optional"]:
            continue
        budget()
        trial = remove_requirement(current, translation["entry_point"], condition["clause"])
        trial_retained = [x for x in retained if x != index]
        final_gate(original, trial, translation, trial_retained, pins)
        result = verifier(trial + "\n" + driver, directory / f"remove-{index}")
        require_tool(result)
        removed = False
        trial_reach = None
        if accepted(result):
            removed, trial_reach = reachable(verifier, trial, translation["entry_point"],
                                             directory / f"remove-{index}-reach")
        minimization.append({"index": index, "removed": removed, "result": result, "reachability": trial_reach})
        if removed:
            current, retained = trial, trial_retained
    budget()
    gates = final_gate(original, current, translation, retained, pins)
    final = verifier(current + "\n" + driver, directory / "final")
    require_tool(final)
    if not accepted(final):
        raise Abstain("Final negative-check/witness recheck failed")
    reach_ok, reach_result = reachable(verifier, current, translation["entry_point"], directory / "reach-final")
    if not reach_ok:
        raise Abstain("Final reachability recheck failed")
    gates.update({"witness_consistency": True, "reachability": True,
                  "source_correspondence": "not established"})
    final_source = current + "\n" + driver
    finding = {
        "kind": "conditional-model-refutation", "candidate": claim, "translation": translation,
        "frozen_negative_model": frozen, "checked_model": current, "annotation_plan": plan,
        "retained_assumptions": retained, "minimization": minimization,
        "dafny": final_source, "dafny_sha256": hashlib.sha256(final_source.encode()).hexdigest(),
        "evidence": f"{claim['id']}/final/model.dfy", "result": final,
        "reachability_result": reach_result, "gates": gates, "source_map": mapped,
        "counters": counters, "limits": asdict(limits),
        "typecheck_log": type_log, "proof_log": proof_log,
    }
    save(directory / "finding.json", finding)
    return finding


async def run(bundle, properties, output, chat, limits=None, verifier=None):
    limits = limits or Limits()
    verifier = verifier or Dafny(limits.verifier_timeout)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": 2, "mode": "general-model-refutation", "identity": bundle["identity"],
              "limits": asdict(limits), "findings": [], "abstentions": [], "errors": []}
    account = getattr(chat, "account", None)
    if getattr(account, "login", None):
        report["authenticated_account"] = account.login
    try:
        claims = select_properties(properties, bundle)
    except (ValueError, KeyError, TypeError) as exc:
        claims = []
        report["errors"].append(str(exc))
    for claim in claims:
        try:
            finding = await run_property(claim, bundle, chat, output / claim["id"], limits, verifier)
            # Identical formalized properties on the same slice are one finding.
            duplicate = any(
                x["candidate"]["path"] == claim["path"] and
                x["translation"]["original_ensures"] == finding["translation"]["original_ensures"] and
                x["translation"]["source_span"] == finding["translation"]["source_span"]
                for x in report["findings"])
            if not duplicate:
                report["findings"].append(finding)
        except ToolFailure as exc:
            report["errors"].append(str(exc))
            break
        except Exception as exc:
            report["abstentions"].append({"id": claim["id"], "reason": f"{type(exc).__name__}: {exc}"[:3000]})
    save(output / "report.json", report)
    return report


def validate_finding(finding, bundle):
    """Static replay at the trusted publisher boundary; no inference or source execution."""
    from nfv.agent import refute
    claim = finding["candidate"]
    if select_properties([claim], bundle)[0]["id"] != claim["id"]:
        raise Abstain("General finding claim identity changed")
    translation = finding["translation"]
    mapped = validate_translation(translation, claim, bundle)
    original = translation["model"]
    inverse = refute.invert_entry_spec(original, translation["entry_point"], BACKEND)
    if not inverse.ok:
        raise Abstain("Cannot reproduce recorded mechanical goal inversion")
    pins, driver = witness_bridge(original, translation["entry_point"], translation["witness"])
    frozen = refute.install_precondition(inverse.model, translation["entry_point"], pins, BACKEND)
    if frozen != finding["frozen_negative_model"]:
        raise Abstain("Frozen model does not match original goal/witness")
    current = annotate(frozen, finding["annotation_plan"])
    retained = finding["retained_assumptions"]
    for index, condition in enumerate(translation["assumptions"]):
        if index not in retained:
            if not condition["optional"]:
                raise Abstain("Required source applicability condition was removed")
            current = remove_requirement(current, translation["entry_point"], condition["clause"])
    final_gate(original, current, translation, retained, pins)
    source = current + "\n" + driver
    if (current != finding["checked_model"] or source != finding["dafny"]
            or hashlib.sha256(source.encode()).hexdigest() != finding["dafny_sha256"]
            or mapped != finding["source_map"]):
        raise Abstain("General evidence computation, provenance, or hash changed")
    if not accepted(finding["result"]):
        raise Abstain("Missing accepted Dafny negative/witness evidence")
    reach = finding["reachability_result"]
    if reach.get("outcome") != "failed" or reach.get("exit_code") != 4 or reach.get("mode") != "unbounded":
        raise Abstain("Missing reachability evidence")
    return mapped
