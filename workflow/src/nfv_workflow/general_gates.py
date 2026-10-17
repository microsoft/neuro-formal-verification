"""Language-independent provenance and frozen-model gates for Dafny refutation."""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

from nfv.agent import integrity

from .github import Abstain, source_path
BACKEND = SimpleNamespace(lang="dafny", comment="//")
SRC = re.compile(r"^\s*//\s*src:\s*([^:]+):(\d+)\s*\|\s?(.*?)\s*$")
SYNTH = re.compile(r"^\s*//\s*synth:\s*(.+)$")
SYNTH_CODE = re.compile(r"^(?:[{};]+|(?:requires|ensures|invariant|assert|decreases|reads|modifies)\b.*)$")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def normalized(value):
    return " ".join(value.split())


def clean_code(model):
    code = re.sub(r"//[^\n]*", "", model)
    if "/*" in code or "*/" in code:
        raise Abstain("Canonical Dafny forbids block comments")
    return code


def anchor(bundle, path, line):
    path = source_path(path)
    record = bundle["sources"].get(path)
    if not record:
        raise Abstain(f"Missing captured context: {path}")
    index = line - record.get("line_start", 1)
    lines = record["text"].splitlines()
    if type(line) is not int or not 0 <= index < len(lines):
        raise Abstain("Source provenance line is outside the captured snapshot")
    return lines[index]


def clauses(model, entry, keyword):
    from nfv.agent import refute
    signature = integrity._entry_signature(model, entry)
    lines = signature.splitlines()
    result = []
    for first, last, key in refute._clause_lines(lines, "dafny"):
        if key == keyword:
            result.append(normalized(re.sub(rf"^{keyword}\s+", "", "\n".join(lines[first:last]).strip())))
    return result


def with_postconditions(model, entry, goals):
    from nfv.agent import refute
    replacement = "\n".join("// synth: original conjecture postcondition\n  ensures " + goal
                            for goal in goals)
    inverse = refute.invert_entry_spec(model, entry, BACKEND)
    if not inverse.ok:
        span = refute._split_signature(model, entry, "dafny")
        if span is not None and not clauses(model, entry, "ensures"):
            start, end = span
            header, _, tail = model[start:end].partition("\n")
            result = model[:start] + header + "\n" + replacement + "\n" + tail + model[end:]
            if clauses(result, entry, "ensures") == [normalized(goal) for goal in goals]:
                return result
        raise Abstain("Cannot isolate an original postcondition: " + inverse.reason)
    start, end = refute._split_signature(inverse.model, entry, "dafny")
    signature = inverse.model[start:end]
    frozen_goal = ("// synth: refutation goal: negation of the translated postcondition\n"
                   "  ensures " + inverse.inverted)
    if signature.count(frozen_goal) != 1:
        raise Abstain("Cannot uniquely isolate the entry's postconditions")
    signature = signature.replace(frozen_goal, replacement, 1)
    result = inverse.model[:start] + signature + inverse.model[end:]
    if clauses(result, entry, "ensures") != [normalized(goal) for goal in goals]:
        raise Abstain("Isolated entry postconditions differ from the selected goals")
    return result


def audit(model, entry):
    if not isinstance(model, str) or len(model) > 160000:
        raise Abstain("Dafny model exceeds size budget")
    if any(character in model for character in ("\0", "\v", "\f", "\x85", "\u2028", "\u2029")):
        raise Abstain("Noncanonical control/line-separator characters in Dafny model")
    if not NAME.fullmatch(entry) or entry.startswith("Nfv"):
        raise Abstain("Invalid or reserved entry name")
    # The retained signature-surgery helper searches raw text. A declaration
    # quoted in a provenance comment must not shadow the real entry declaration.
    mentions = re.findall(
        rf"\b(?:method|function|lemma|predicate)\s+(?:\{{:[^}}]*\}}\s*)*{re.escape(entry)}\b", model)
    if len(mentions) != 1:
        raise Abstain("Entry declaration is duplicated or shadowed by quoted source/model text")
    code = clean_code(model)
    if re.search(r"\b(assume|expect|include|import|export|module|extern|abstract|opaque|iterator|yield|const|type|newtype|trait|codatatype)\b", code):
        raise Abstain("Dafny model uses an unsafe or unsupported verification construct")
    if "{:" in code or re.search(r"\bdecreases\s+\*", code):
        raise Abstain("Attributes or nontermination escapes are prohibited")
    if re.search(r"\bNfv[A-Za-z0-9_]*", code):
        raise Abstain("Model attempts to use reserved trusted probe names")
    report = integrity.check(model, entry)
    if not report.ok or report.trusted_base:
        raise Abstain("Core integrity gate: " + "; ".join(report.violations + report.trusted_base))
    declaration = re.search(rf"(?m)^method\s+{re.escape(entry)}\s*\(", code)
    if not declaration or code[:declaration.start()].count("{") != code[:declaration.start()].count("}"):
        raise Abstain("Entry must be a non-ghost top-level method")
    lines = code.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:ghost\s+)?(?:method|function|predicate|lemma)\b", line):
            if not re.match(r"^\s*(?:ghost\s+)?(?:method|function|predicate|lemma)\s+[A-Za-z_]\w*\s*\(", line):
                raise Abstain("Unsupported callable declaration")
            found_body = False
            for tail in lines[index + 1:]:
                if tail.strip() == "{":
                    found_body = True
                    break
                if re.match(r"^\s*(?:ghost\s+)?(?:method|function|predicate|lemma|class|datatype|type|newtype|trait)\b", tail):
                    break
                if tail.strip():
                    if (not re.match(r"^\s*(requires|ensures|reads|modifies|decreases)\s+", tail)
                            or any(c in tail for c in "{};")
                            or re.search(r"(?:\bin|&&|\|\||==|=>|::|[+*/%,:<>=-])\s*$", tail)):
                        raise Abstain("Callable signatures/contracts must be complete canonical lines")
            if not found_body:
                raise Abstain("Every callable needs a canonical body; no bodyless contracts")
    return report


def validate_translation(translation, claim, bundle, model=None):
    model = translation["model"] if model is None else model
    entry = translation["entry_point"]
    audit(model, entry)
    expected = [normalized(x) for x in translation["original_ensures"]]
    if not expected or clauses(model, entry, "ensures") != expected:
        raise Abstain("Original property goal was altered")
    assumptions = translation.get("assumptions", [])
    if len(assumptions) > 20:
        raise Abstain("Too many model conditions")
    declared = []
    for assumption in assumptions:
        if assumption.get("origin") != "inferred" or type(assumption.get("optional")) is not bool:
            raise Abstain("Conditions must identify inferred origin and optionality")
        if not assumption.get("description"):
            raise Abstain("Conditions require a developer-language explanation")
        declared.append(normalized(assumption["clause"]))
    if len(set(declared)) != len(declared) or clauses(model, entry, "requires") != declared:
        raise Abstain("Entry conditions differ from recorded condition metadata")
    first, last = translation["source_span"]
    if type(first) is not int or type(last) is not int or first > last or last - first > 1000:
        raise Abstain("Invalid source span")
    if not all(first <= line <= last for line in claim["lines"]):
        raise Abstain("Source span omits changed anchors")
    for path in translation["required_context"]:
        if path not in bundle["sources"]:
            raise Abstain(f"Missing required source context: {path}")
    if claim["path"] not in translation["required_context"]:
        raise Abstain("Primary source absent from required context")
    mapped, previous, problems = [], [], []
    for number, line in enumerate(model.splitlines(), 1):
        if not line.strip():
            continue
        src, synth = SRC.fullmatch(line), SYNTH.fullmatch(line)
        if src:
            if previous and previous[0][0] == "synth":
                problems.append(f"Line {number}: Source tag cannot replace a pending synthetic annotation")
                previous = []
            path, origin, excerpt = src[1], int(src[2]), src[3]
            if anchor(bundle, path, origin).strip() != excerpt.strip():
                raise Abstain("Fabricated source provenance excerpt")
            if path not in translation["required_context"]:
                raise Abstain("Provenance uses undeclared context")
            previous.append(("source", path, origin, excerpt))
            continue
        if synth:
            if previous:
                problems.append(f"Line {number}: Synthetic tag cannot discard pending source provenance")
            previous = [("synth",)]
            continue
        if line.lstrip().startswith("//"):
            if previous:
                problems.append(f"Line {number}: Comment interrupts pending source provenance")
                previous = []
            continue
        if not previous:
            problems.append(f"Untagged Dafny line {number}: {line.strip()[:160]}")
            continue
        if previous[0][0] == "synth":
            if not SYNTH_CODE.fullmatch(line.strip()):
                problems.append(f"Line {number}: Computation cannot hide behind a synthetic annotation tag: "
                                f"{line.strip()[:160]}")
        else:
            if re.match(r"\s*(requires|ensures)\b", line):
                problems.append(f"Line {number}: Contracts must use synthetic tags, not frozen computation tags")
            for _, path, origin, excerpt in previous:
                mapped.append({"model_line": number, "path": path,
                               "line": origin, "source": excerpt, "model": line.strip()})
        previous = []
    if previous:
        problems.append("A trailing provenance tag has no corresponding Dafny line")
    covered = {x["line"] for x in mapped if x["path"] == claim["path"]}
    required = set(claim["lines"])
    for line in range(first, last + 1):
        text = anchor(bundle, claim["path"], line).strip()
        if text and not re.fullmatch(r"[{}();\[\]]+", text) and not text.startswith(("//", "#", "/*", "*")):
            required.add(line)
    if not required <= covered:
        missing = sorted(required - covered)
        problems.append(f"Source provenance does not cover the declared source slice: "
                        f"{claim['path']} lines {missing[:30]}. Consecutive exact src tags may "
                        "map multiple source lines to one Dafny computation line.")
    environment = translation.get("environment", [])
    if len(environment) > 30:
        raise Abstain("Environment context budget exceeded")
    declared_helpers = set(re.findall(r"(?m)^\s*(?:ghost\s+)?(?:method|function|predicate|lemma)\s+(\w+)\s*\(", clean_code(model))) - {entry}
    described_helpers = set()
    for item in environment:
        if item.get("kind") not in ("inferred", "source-derived") or not item.get("description"):
            raise Abstain("Environment abstraction needs explicit provenance and meaning")
        described_helpers.add(item["name"])
        origin = item["source"]
        if not origin["lines"] or origin["path"] not in translation["required_context"]:
            raise Abstain("Missing environment source context")
        for line in origin["lines"]:
            anchor(bundle, origin["path"], line)
    if declared_helpers != described_helpers:
        problems.append("Helper declarations and environment metadata disagree: "
                        f"missing metadata for {sorted(declared_helpers - described_helpers)}; "
                        f"metadata without declarations {sorted(described_helpers - declared_helpers)}. "
                        "Every non-entry callable, including specification functions and predicates, "
                        "needs its own source-anchored environment entry.")
    if not translation.get("correspondence") or not translation.get("explanation"):
        raise Abstain("Missing conditional source correspondence/explanation")
    for note in translation.get("source_notes", []):
        anchor(bundle, note["path"], note["line"])
        if not isinstance(note.get("note"), str):
            raise Abstain("Invalid source overlay note")
        if note.get("kind") not in (None, "invariant", "spec"):
            raise Abstain("Source overlay kind must be invariant or spec")
    if problems:
        shown = problems[:40]
        if len(problems) > len(shown):
            shown.append(f"{len(problems) - len(shown)} additional translation audit errors omitted")
        raise Abstain("Translation audit failed:\n" + "\n".join(shown))
    return mapped


def signature_parameters(model, entry):
    signature = integrity._entry_signature(model, entry)
    match = re.search(rf"\bmethod\s+{re.escape(entry)}\s*\(([^()]*)\)\s*returns\s*\(([^()]*)\)", signature)
    if not match:
        raise Abstain("Witness bridge requires named value inputs and return values")
    def parse(text):
        parsed = []
        for item in text.split(",") if text.strip() else []:
            parameter = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*:\s*(int|bool|seq<int>|seq<bool>)\s*", item)
            if not parameter or parameter[1].startswith("Nfv"):
                raise Abstain("Unsupported concrete witness parameter/return type")
            parsed.append((parameter[1], parameter[2]))
        if len({x[0] for x in parsed}) != len(parsed):
            raise Abstain("Repeated parameter names")
        return parsed
    inputs, outputs = parse(match[1]), parse(match[2])
    if not outputs or len(inputs) > 20 or len(outputs) > 10:
        raise Abstain("Witness arity exceeds supported bounds")
    return inputs, outputs


def literal(value, typ):
    if typ == "bool" and type(value) is bool:
        return str(value).lower()
    if typ == "int" and type(value) is int and abs(value) <= 2**128:
        return str(value) if value >= 0 else f"({value})"
    if typ in ("seq<int>", "seq<bool>") and isinstance(value, list) and len(value) <= 100:
        return "[" + ", ".join(literal(x, typ[4:-1]) for x in value) + "]"
    raise Abstain("Witness must give a concrete type-correct value to every input")


def witness_bridge(model, entry, witness):
    inputs, outputs = signature_parameters(model, entry)
    if set(witness) != {x[0] for x in inputs}:
        raise Abstain("Witness must bind exactly the input parameters")
    pins = [f"{name} == {literal(witness[name], typ)}" for name, typ in inputs]
    driver = ["method NfvWitnessCheck()", "{"]
    driver += [f"  var {name}: {typ} := {literal(witness[name], typ)};" for name, typ in inputs]
    results = ", ".join(f"NfvResult{i}" for i in range(len(outputs)))
    driver += [f"  var {results} := {entry}(" + ", ".join(x[0] for x in inputs) + ");", "}"]
    return pins, "\n".join(driver) + "\n"


def annotate(frozen, plan):
    if not isinstance(plan, list) or len(plan) > 60:
        raise Abstain("Proof annotation plan exceeds budget")
    lines = frozen.splitlines()
    additions = {}
    for edit in plan:
        after, text = edit["after"], edit["text"]
        if type(after) is not int or not 1 <= after <= len(lines) or lines[after - 1].lstrip().startswith("//"):
            raise Abstain("Invalid annotation insertion point")
        if not isinstance(text, str) or len(text) > 4000 or "\n" in text or "\r" in text:
            raise Abstain("Proof annotations must be single lines")
        if not re.match(r"^\s*(assert|invariant|decreases)\s+", text):
            raise Abstain("Proof repairs may only insert assert/invariant/decreases annotations")
        if re.search(r"//|/\*|\*/|[{}]|:=|\b(assume|expect|requires|ensures|return|var|method|function|lemma)\b", text):
            raise Abstain("Proof repair contains computation, a contract change, or an escape")
        if text.strip().startswith("assert "):
            if not text.rstrip().endswith(";") or text.count(";") != 1:
                raise Abstain("Assertion repair must contain exactly one assertion")
        elif ";" in text:
            raise Abstain("Invalid annotation terminator")
        if re.match(r"\s*decreases\s+\*", text):
            raise Abstain("Termination cannot be disabled")
        additions.setdefault(after, []).append("  " + text.strip())
    output = []
    for number, line in enumerate(lines, 1):
        output.append(line)
        for text in additions.get(number, []):
            output.extend(["// synth: proof repair annotation", text])
    result = "\n".join(output) + "\n"
    if not integrity.check_freeze(frozen, result).ok:
        raise Abstain("Core source freeze rejected the annotation plan")
    return result


def remove_requirement(model, entry, clause):
    signature = integrity._entry_signature(model, entry)
    lines = signature.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"\s*requires\b", line) and normalized(re.sub(r"^\s*requires\s+", "", line)) == normalized(clause):
            del lines[index]
            return model.replace(signature, "\n".join(lines) + "\n", 1)
    raise Abstain("Cannot locate the exact optional condition for minimization")


def final_gate(original, current, translation, retained, pins):
    from nfv.agent import refute
    entry = translation["entry_point"]
    audit(current, entry)
    inverse = refute.invert_entry_spec(original, entry, BACKEND)
    if not inverse.ok or clauses(current, entry, "ensures") != [inverse.inverted]:
        raise Abstain("Negated goal was changed")
    expected = [normalized(translation["assumptions"][i]["clause"]) for i in retained] + [normalized(x) for x in pins]
    if clauses(current, entry, "requires") != expected:
        raise Abstain("Frozen preconditions or witness were changed")
    frozen = integrity.check_freeze(original, current)
    if not frozen.ok:
        raise Abstain("Frozen source computation was changed")
    return {"integrity": True, "goal": True, "source_freeze": True,
            "conditions": True, "source_freeze_hash": integrity.freeze_hash(original),
            "computation_sha256": hashlib.sha256(original.encode()).hexdigest()}
