"""Verification-first conditional model analysis using the retained NFV backend."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .check import Dafny, accepted
from .general_gates import (BACKEND, annotate, clauses, clean_code, final_gate,
                            remove_requirement, validate_translation, with_postconditions, witness_bridge)
from .github import Abstain
from .model import normalize, validate_claim
from .outcomes import EXHAUSTED, INVALID, REFUTED, VERIFIED, InferenceFailure, ResourcesExhausted
from .source_context import (LANGUAGES, caller_first, check_request, diagnostics,
                             function_catalog, quoted_context, scoped_context)
ALIASES = {"c++": "cpp", "c#": "csharp", "js": "javascript", "ts": "typescript", "py": "python",
           "rs": "rust"}


def numbered(record):
    """Model view of a captured source: each line prefixed by its repository line number."""
    first = record.get("line_start", 1)
    return {"blob_sha": record.get("blob_sha"), "numbered_lines": "\n".join(
        f"{first + index}| {line}" for index, line in enumerate(record["text"].splitlines()))}


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
    "rust": "Preserve Rust fixed-width integers: plain arithmetic overflow panics in debug builds and wraps "
            "in release builds, so record the modeled build mode; wrapping_/checked_/saturating_/overflowing_ "
            "methods have their own semantics. Preserve truncating division, panics (unwrap/expect, indexing, "
            "division by zero, explicit panic!) as exceptional outcomes, Option/Result variants, ownership "
            "moves and &mut borrows as explicit state updates, and slice/Vec bounds. unsafe code, unknown trait "
            "impls or generic instantiations, macros, and closures without captured definitions require "
            "explicit context or abstention.",
}


class ToolFailure(RuntimeError):
    """Infrastructure failure, not a refutation or a syntax-repair opportunity."""


def require_tool(result):
    if result.get("outcome") == "tool_error":
        raise ToolFailure("Dafny execution failed: " + str(result.get("stderr", ""))[-2000:])


@dataclass(frozen=True)
class Limits:
    syntax_rounds: int = 3
    proof_rounds: int = 10
    verifier_timeout: int = 60
    model_timeout: int = 240
    entry_seconds: int = 1800

    def __post_init__(self):
        if not 0 <= self.syntax_rounds <= 20 or not 0 <= self.proof_rounds <= 30:
            raise ValueError("Repair limits must be syntax 0..20 and proof 0..30")
        if min(self.verifier_timeout, self.model_timeout, self.entry_seconds) <= 0:
            raise ValueError("Timeouts must be positive")


def select_properties(raw, bundle, rejected=None):
    """Validate extracted claims. With `rejected`, invalid claims are recorded there
    and skipped instead of failing the whole batch."""
    if not isinstance(raw, list) or len(raw) > 100:
        raise Abstain("Expected at most 100 extracted intended properties")
    selected, seen, catalog = [], set(), None
    for value in raw:
        try:
            source_span = None
            if isinstance(value, dict) and "function_id" in value:
                if catalog is None:
                    catalog = {fn["id"]: fn for fn in function_catalog(bundle)["functions"]}
                fn = catalog.get(value["function_id"])
                if fn is None:
                    raise Abstain("Property selects an unknown captured function")
                if value.get("path", fn["path"]) != fn["path"]:
                    raise Abstain("Property path disagrees with its captured function")
                value = {**value, "path": fn["path"], "lines": fn["lines"]}
                source_span = (fn["first"], fn["last"])
            path = validate_claim(value, bundle, allow_inferred=True, source_span=source_span)
            language = str(value.get("language") or LANGUAGES.get(Path(path).suffix.lower(), "")).lower()
            language = ALIASES.get(language, language)
            expected = LANGUAGES.get(Path(path).suffix.lower())
            if language not in set(LANGUAGES.values()) or (expected != language and not (
                    Path(path).suffix.lower() == ".h" and language in ("c", "cpp"))):
                raise Abstain("Unsupported or mismatched source language")
        except (Abstain, KeyError, TypeError, AttributeError) as exc:
            if rejected is None:
                raise
            identity = json.dumps(value, sort_keys=True, default=str) if isinstance(value, dict) else repr(value)
            rejected.append({"id": "claim-" + hashlib.sha256(identity.encode()).hexdigest()[:14],
                             "reason": INVALID, "diagnostic": str(exc)})
            continue
        key = (path, normalize(value["property"]))
        if key in seen:
            continue
        seen.add(key)
        claim = {key: value[key] for key in ("path", "lines", "signal_id", "quote", "property")}
        for field in ("origin", "function_id"):
            if field in value:
                claim[field] = value[field]
        claim["language"] = language
        claim["id"] = hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()[:20]
        selected.append(claim)
        if len(selected) == 10:
            break
    return caller_first(bundle, selected)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def json_answer(text):
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    if len(text) > 300000:
        raise ResourcesExhausted("Model response exceeds the artifact budget")
    result = json.loads(text)
    if isinstance(result, dict) and isinstance(result.get("failure"), str):
        if result["failure"] not in (EXHAUSTED, INVALID):
            raise Abstain("Unknown model failure category")
        message = str(result.get("diagnostic", "No usable model was produced"))
        if result["failure"] == EXHAUSTED:
            raise ResourcesExhausted(message)
        raise Abstain(message)
    if isinstance(result, dict) and result.get("abstain"):
        raise Abstain(str(result["abstain"]))
    return result


def reachable(verifier, model, entry, directory):
    from nfv.agent import refute
    # The retained probe builder counts braces textually; source excerpts in
    # provenance comments (e.g. `if (...) {`) must not unbalance its body scan.
    probe = refute._probe_source(clean_code(model), entry, BACKEND)
    if not probe:
        raise Abstain("Could not construct the feasible-input probe")
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
            raise ResourcesExhausted("Per-property wall-time budget exhausted")
        return remaining

    async def ask(stage, prompt):
        remaining = budget()
        compacted = False
        try:
            check_request(system, prompt, stage)
        except ResourcesExhausted:
            if stage not in ("syntax-repair", "proof-repair", "witness"):
                raise
            request = json.loads(prompt)
            model = request.get("current_model") or request.get("translation", {}).get("model")
            if model is None:
                model = "\n".join(line.split(": ", 1)[1] for line in request["frozen_model_lines"])
            request["source_context"] = quoted_context(request["source_context"], model)
            if "metadata" in request:
                request["metadata"] = {key: value for key, value in request["metadata"].items()
                                       if key != "original_ensures"}
            prompt = json.dumps(request, ensure_ascii=False)
            check_request(system, prompt, stage)
            compacted = True
        response = await chat.ask(system, prompt, timeout=min(limits.model_timeout, remaining))
        record = {"stage": stage, "response": response.text,
                  "request_bytes": len(system.encode("utf-8")) + len(prompt.encode("utf-8")),
                  "source_context_deduplicated": compacted}
        for field in ("model", "input_tokens", "output_tokens", "api_calls", "nano_aiu"):
            if hasattr(response, field):
                record[field] = getattr(response, field)
        transcript.append(record)
        save(directory / "model-transcript.json", transcript)
        return json_answer(response.text)

    context = {
        "identity": bundle["identity"], "claim": claim,
        "language_semantics": LANGUAGE_GUIDES[claim["language"]],
        **scoped_context(bundle, claim),
    }
    save(directory / "source-context.json", context)
    encoded_context = json.dumps(context, ensure_ascii=False)
    counters["translation_calls"] += 1
    translation = await ask("translation", "Translate this whole conjecture for verification first. "
                            "Preserve every part in the entry postconditions; do not add or split properties. "
                            "A failing witness is not required.\n" + encoded_context)
    if not isinstance(translation, dict):
        raise Abstain("Translation must be a structured artifact")
    current = translation["model"]
    type_log = []
    positive = None
    for attempt in range(limits.syntax_rounds + 1):
        budget()
        try:
            translation = {**translation,
                           "original_ensures": clauses(current, translation["entry_point"], "ensures")}
            mapped = validate_translation(translation, claim, bundle, current)
            scope = context["source_scope"]
            if not scope["first"] <= translation["source_span"][0] <= translation["source_span"][1] <= scope["last"]:
                raise Abstain("Translation source span escapes the scoped changed function")
            if any(path not in context["sources"] for path in translation["required_context"]):
                raise Abstain("Translation requires source outside the supplied scoped context")
            for row in mapped:
                ranges = context["sources"].get(row["path"], {}).get("ranges", [])
                if not any(low <= row["line"] <= high for low, high in ranges):
                    raise Abstain("Translation cites computation outside the supplied scoped context")
            for item in translation.get("environment", []):
                ranges = context["sources"].get(item["source"]["path"], {}).get("ranges", [])
                if not all(any(low <= line <= high for low, high in ranges)
                           for line in item["source"]["lines"]):
                    raise Abstain("Environment contract cites source outside the supplied scoped context")
            result = verifier.resolve(current, directory / f"syntax-{attempt}")
            require_tool(result)
            if (result.get("outcome") == "verified" and result.get("exit_code") == 0
                    and result.get("mode") == "typecheck"):
                positive = verifier(current, directory / "verification")
                require_tool(positive)
                save(directory / "drafts" / f"{attempt}-verification.json", positive)
                if re.search(r"(?m)^.*\.dfy\(\d+,\d+\): Error: [^\n]*(?:index|bound)[^\n]*out of range",
                             positive.get("stdout", "") + positive.get("stderr", "")):
                    result = {**positive, "outcome": "audit_failure", "mode": "well-definedness",
                              "stderr": "Draft array/sequence accesses are not established as "
                                        "well-defined. Repair justified domains or proof annotations "
                                        "before freezing the translation.\n" + positive.get("stderr", "")}
        except (Abstain, KeyError, TypeError, ValueError) as exc:
            result = {"outcome": "audit_failure", "stderr": str(exc), "stdout": ""}
        type_log.append(result)
        save(directory / "drafts" / f"{attempt}.json",
             {"translation": {**translation, "model": current}, "typecheck": result})
        if result.get("outcome") == "verified" and result.get("exit_code") == 0 and result.get("mode") == "typecheck":
            break
        if result.get("outcome") == "timeout":
            raise ResourcesExhausted("Type checking exceeded its time budget")
        if attempt == limits.syntax_rounds:
            detail = result.get("stderr") or result.get("stdout", "")
            raise Abstain(f"Type checking failed after {limits.syntax_rounds} syntax repairs: {detail[:1000]}")
        counters["syntax_repairs"] += 1
        old_goals = translation.get("original_ensures", [])
        from nfv.agent import refute
        span = refute._split_signature(current, translation["entry_point"], "dafny")
        contract_lines = (current.count("\n", 0, span[0]) + 1,
                          current.count("\n", 0, span[1]) + 1) if span else (0, 0)
        error_lines = [int(line) for line in re.findall(
            r"\.dfy\((\d+),\d+\):\s*(?:Parse )?Error\b",
            result.get("stdout", "") + result.get("stderr", ""), re.IGNORECASE)]
        contract_error = any(contract_lines[0] <= line <= contract_lines[1] for line in error_lines)
        repair = await ask("syntax-repair", json.dumps({
            "instruction": "Correct this draft's Dafny syntax, types, contracts and source mapping. "
                           "The source property and captured source are immutable; never weaken or "
                           "replace the intended property to obtain a proof. Return a JSON object "
                           "with model (full tagged Dafny) and any corrected translation metadata. "
                           "If a contract changes, update original_ensures, assumptions or environment "
                           "to match. Preserve every part of the whole conjecture, including all "
                           "conjuncts and entry postconditions. Never drop an obligation or decompose "
                           "the property into child checks. This is draft formalization, not proof repair. The first "
                           "fully audited, type-checked translation will be frozen before verification.",
            "source_context": context, "metadata": {k: v for k, v in translation.items() if k != "model"},
            "current_model": current, "typecheck": diagnostics(result),
        }, ensure_ascii=False))
        fields = {"model", "entry_point", "original_ensures", "assumptions", "environment",
                  "source_span", "required_context", "source_notes", "correspondence", "explanation"}
        if not isinstance(repair, dict) or "model" not in repair or set(repair) - fields:
            raise Abstain("Draft repair returned unsupported translation metadata")
        current = repair["model"]
        entry = repair.get("entry_point", translation["entry_point"])
        if old_goals and not contract_error and clauses(current, entry, "ensures") != old_goals:
            current = with_postconditions(current, entry, old_goals)
            repair = {**repair, "model": current, "original_ensures": old_goals}
            save(directory / "drafts" / f"{attempt}-goal-restoration.json",
                 {"reason": "Diagnostics did not identify an entry-contract error",
                  "restored_ensures": old_goals})
        translation = {**translation, **repair}
    original = current
    translation = {**translation, "model": original}
    save(directory / "translation.json", translation)
    budget()
    if positive is None:
        raise Abstain("Original property lacks its recorded verification attempt")
    if accepted(positive):
        feasible, domain = reachable(verifier, original, translation["entry_point"], directory / "input-domain")
        if not feasible:
            if domain.get("outcome") == "timeout":
                raise ResourcesExhausted("Input-domain check exceeded its time budget")
            raise Abstain("No feasible returning input was established for the recorded assumptions")
        verified = {
            "kind": "conditional-model-verification", "candidate": claim,
            "translation": translation, "source_map": mapped, "result": positive,
            "input_domain_result": domain, "dafny": original,
            "dafny_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "evidence": f"{claim['id']}/verification/model.dfy", "counters": counters,
            "limits": asdict(limits),
        }
        save(directory / "verified.json", verified)
        return verified
    if "witness" not in translation:
        witness = await ask("witness", json.dumps({
            "instruction": "Verification did not succeed. Propose a concrete counterexample for "
                           "the frozen model. Return witness, explanation, and optional presentation. "
                           "Never change the model, property, assumptions or environment contracts.",
            "source_context": context, "translation": translation,
            "verification": diagnostics(positive),
        }, ensure_ascii=False))
        if (not isinstance(witness, dict) or not {"witness", "explanation"} <= set(witness)
                or set(witness) - {"witness", "explanation", "presentation"}):
            raise Abstain("Counterexample response attempted to replace frozen translation metadata")
        translation = {**translation, **witness}
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
            if result.get("outcome") == "type_error":
                raise Abstain("Refutation model did not type-check within its repair budget")
            raise ResourcesExhausted(f"Neither verification nor refutation succeeded after {limits.proof_rounds} proof repairs")
        counters["proof_repairs"] += 1
        repair = await ask("proof-repair", json.dumps({
            "instruction": "Return ONLY {\"annotations\":[{\"after\":FROZEN_LINE_NUMBER,\"text\":\"invariant ...\"}]}. "
                           "The list REPLACES the previous annotation plan. Only single-line assert (ending ;), "
                           "invariant, and decreases annotations are permitted. No computation, contracts, "
                           "goal, witness, declarations, or original source changes. No full-model response.",
            "source_context": context, "frozen_model_lines": [
                f"{i}: {line}" for i, line in enumerate(frozen.splitlines(), 1)],
            "previous_plan": plan, "negative_check": diagnostics(result),
        }, ensure_ascii=False))
        if not isinstance(repair, dict) or set(repair) != {"annotations"}:
            raise Abstain("Proof repair attempted to replace frozen computation or metadata")
        plan = repair["annotations"]
    budget()
    reach_ok, reach_result = reachable(verifier, current, translation["entry_point"], directory / "reach-initial")
    if not reach_ok:
        if reach_result.get("outcome") == "timeout":
            raise ResourcesExhausted("Counterexample input-domain check exceeded its time budget")
        raise Abstain("No feasible returning input was established for the counterexample")
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
        if final.get("outcome") == "timeout":
            raise ResourcesExhausted("Final refutation recheck exceeded its time budget")
        raise Abstain("Final negative-check/witness recheck failed")
    reach_ok, reach_result = reachable(verifier, current, translation["entry_point"], directory / "reach-final")
    if not reach_ok:
        if reach_result.get("outcome") == "timeout":
            raise ResourcesExhausted("Final input-domain recheck exceeded its time budget")
        raise Abstain("Final input-domain recheck failed")
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
    report = {"schema": 3, "mode": "general-model-analysis", "completed": False, "identity": bundle["identity"],
              "limits": asdict(limits), "findings": [], "verified": [], "failures": [],
              "errors": [], "properties": []}
    account = getattr(chat, "account", None)
    if getattr(account, "login", None):
        report["authenticated_account"] = account.login
    try:
        claims = select_properties(properties, bundle, rejected=report["failures"])
    except (Abstain, ValueError, KeyError, TypeError) as exc:
        claims = []
        report["errors"].append(str(exc))
        report["failures"].append({"id": "proposals", "reason": INVALID, "diagnostic": str(exc)})
    for failure in report["failures"]:
        report["properties"].append({
            "id": failure["id"], "property": "Invalid proposed property",
            "status": failure["reason"], "diagnostic": failure["diagnostic"], "formalized": None,
        })
    save(output / "report.json", report)
    completed = len(report["properties"])
    total = completed + len(claims)
    for checked, claim in enumerate(claims, completed + 1):
        entry = {k: claim[k] for k in ("id", "path", "lines", "quote", "property")}
        if "origin" in claim:
            entry["origin"] = claim["origin"]
        entry.update(formalized=None, status=INVALID)
        report["properties"].append(entry)
        try:
            finding = await run_property(claim, bundle, chat, output / claim["id"], limits, verifier)
            if finding["kind"] == "conditional-model-verification":
                entry["status"] = VERIFIED
                report["verified"].append(finding)
            else:
                entry["status"] = REFUTED
                report["findings"].append(finding)
        except Exception as exc:
            entry["status"] = EXHAUSTED if isinstance(exc, (ResourcesExhausted, TimeoutError)) else INVALID
            entry["diagnostic"] = str(exc)[:3000]
            if isinstance(exc, (ToolFailure, InferenceFailure)):
                report["errors"].append(str(exc))
            failure = {"id": claim["id"], "reason": entry["status"], "diagnostic": entry["diagnostic"]}
            report["failures"].append(failure)
            save(output / claim["id"] / "failure.json", failure)
        finally:
            translation = output / claim["id"] / "translation.json"
            if translation.exists():
                entry["formalized"] = json.loads(translation.read_text(encoding="utf-8")).get("original_ensures")
            save(output / "report.json", report)
        if report["errors"]:
            return report
        print(json.dumps({"event": "property_completed", "property": claim["id"], "status": entry["status"],
                          "properties_completed": checked, "properties_total": total,
                          "properties_remaining": total - checked}), flush=True)
    report["completed"] = not report["errors"]
    save(output / "report.json", report)
    return report


def validate_verification(evidence, bundle):
    if evidence.get("kind") != "conditional-model-verification":
        raise Abstain("Unexpected verification evidence kind")
    claim = evidence["candidate"]
    if select_properties([claim], bundle)[0]["id"] != claim["id"]:
        raise Abstain("Verified property identity changed")
    translation = evidence["translation"]
    source = translation["model"]
    mapped = validate_translation(translation, claim, bundle)
    if (source != evidence["dafny"] or mapped != evidence["source_map"]
            or hashlib.sha256(source.encode()).hexdigest() != evidence["dafny_sha256"]
            or not accepted(evidence["result"])):
        raise Abstain("Verified model, provenance or positive proof evidence changed")
    domain = evidence["input_domain_result"]
    if domain.get("outcome") != "failed" or domain.get("exit_code") != 4 or domain.get("mode") != "unbounded":
        raise Abstain("Missing feasible-input evidence for the verified model")
    return mapped


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
        raise Abstain("Missing feasible-input evidence")
    return mapped
