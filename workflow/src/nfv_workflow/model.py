"""A deliberately small, typed, non-executable translation boundary.

The proposer cannot supply Dafny programs, commands, attributes, or contracts.
Only expressions in a closed JSON grammar cross the boundary.
"""

from __future__ import annotations

import hashlib
import json
import re

from .github import Abstain, source_path

TYPES = {"int", "bool", "seq<int>", "seq<bool>"}
OPERATORS = {"+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=", "&&", "||"}
IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def name(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value) or value.startswith("Nfv"):
        raise Abstain("Invalid or reserved model identifier")
    return value


def expression(node, variables, functions=(), depth=0):
    if depth > 24:
        raise Abstain("Expression nesting exceeds budget")
    recur = lambda x: expression(x, variables, functions, depth + 1)
    if type(node) is bool:
        return str(node).lower()
    if type(node) is int and abs(node) <= 2**128:
        return str(node) if node >= 0 else f"({node})"
    if isinstance(node, str):
        if node not in variables:
            raise Abstain(f"Unknown model variable: {node}")
        return node
    if not isinstance(node, dict) or len(node) != 1:
        raise Abstain("Expressions must use the closed JSON grammar")
    op, args = next(iter(node.items()))
    if op in OPERATORS and isinstance(args, list) and len(args) == 2:
        return f"({recur(args[0])} {op} {recur(args[1])})"
    if op == "not":
        return f"!({recur(args)})"
    if op == "if" and isinstance(args, list) and len(args) == 3:
        return f"(if {recur(args[0])} then {recur(args[1])} else {recur(args[2])})"
    if op == "seq" and isinstance(args, list) and len(args) <= 100:
        return "[" + ", ".join(recur(x) for x in args) + "]"
    if op == "length":
        return f"|{recur(args)}|"
    if op == "index" and isinstance(args, list) and len(args) == 2:
        return f"{recur(args[0])}[{recur(args[1])}]"
    if op == "call" and isinstance(args, list) and len(args) <= 20 and args:
        if args[0] not in functions:
            raise Abstain("Calls must refer to supplied, acyclic environment definitions")
        return f"{args[0]}(" + ", ".join(recur(x) for x in args[1:]) + ")"
    raise Abstain(f"Unsupported expression operator: {op}")


def parameters(items):
    if not isinstance(items, list) or len(items) > 20:
        raise Abstain("Parameter budget exceeded")
    names = []
    for item in items:
        n = name(item["name"])
        if n in names or item["type"] not in TYPES:
            raise Abstain("Duplicate parameter or unsupported type")
        names.append(n)
    return names, ", ".join(f'{x["name"]}: {x["type"]}' for x in items)


def normalize(value):
    return " ".join(value.casefold().split())


def validate_claim(candidate, bundle, allow_inferred=False):
    path = source_path(candidate["path"])
    lines = candidate["lines"]
    if (path not in bundle["changed"] or not isinstance(lines, list) or not lines
            or not all(type(x) is int and x in bundle["changed"][path] for x in lines)):
        raise Abstain("Property is not tied to added/changed head code")
    source = bundle["sources"][path]
    first_line = source.get("line_start", 1)
    if not all(first_line <= n < first_line + len(source["text"].splitlines()) for n in lines):
        raise Abstain("Property anchor missing from captured source")
    quote = candidate["quote"]
    if allow_inferred and candidate.get("origin") == "source":
        evidence = "\n".join(source["text"].splitlines()[n - first_line] for n in lines)
        if (candidate["signal_id"] != f"source:{path}" or not isinstance(quote, str)
                or not quote.strip() or " ".join(quote.split()) not in " ".join(evidence.split())):
            raise Abstain("Inferred property lacks an exact captured-source quote")
    else:
        if candidate.get("origin", "intent") != "intent":
            raise Abstain("Unsupported property origin")
        signal = next((x for x in bundle["signals"] if x["id"] == candidate["signal_id"]), None)
        if (not signal or not isinstance(quote, str) or not quote.strip()
                or " ".join(quote.split()) not in " ".join(signal["text"].split())):
            raise Abstain("Property lacks an exact PR/commit evidence quote")
    if not isinstance(candidate.get("property"), str) or not candidate["property"].strip():
        raise Abstain("Missing intended property")
    if len(candidate["property"]) > 4000:
        raise Abstain("Property exceeds text budget")
    return path


def select_candidates(raw, bundle):
    if not isinstance(raw, list) or len(raw) > 100:
        raise Abstain("Expected at most 100 proposals; at most 10 are checked")
    selected, seen = [], set()
    for candidate in raw:
        path = validate_claim(candidate, bundle)
        for key in ("property", "explanation", "correspondence"):
            if not isinstance(candidate.get(key), str) or not candidate[key].strip():
                raise Abstain(f"Missing {key}")
            if len(candidate[key]) > 4000:
                raise Abstain("Candidate prose exceeds budget")
        model = candidate["model"]
        for definition in model.get("environment", []):
            origin = definition.get("source", {})
            origin_path = source_path(origin["path"])
            origin_lines = origin["lines"]
            record = bundle["sources"].get(origin_path, {})
            first = record.get("line_start", 1)
            if (origin_path not in bundle["sources"] or not origin_lines
                    or not all(type(n) is int and first <= n < first + len(
                        record["text"].splitlines())
                        for n in origin_lines)):
                raise Abstain("Environment definition lacks captured source anchors")
        # Model identity catches equivalent repeated proposals with different prose.
        canonical = json.dumps(model, sort_keys=True, separators=(",", ":"))
        property_key = json.dumps({k: model.get(k) for k in
                                   ("parameters", "property", "body", "environment")},
                                  sort_keys=True, separators=(",", ":"))
        keys = ((path, normalize(candidate["property"])),
                (path, property_key))
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        candidate = dict(candidate)
        candidate["id"] = hashlib.sha256((path + canonical).encode()).hexdigest()[:20]
        selected.append(candidate)
        if len(selected) == 10:
            break
    return selected


def render_model(candidate, retained=None):
    model = candidate["model"]
    if len(json.dumps(model)) > 60000:
        raise Abstain("Model size exceeds budget")
    params, signature = parameters(model["parameters"])
    if "result" in params:
        raise Abstain("result is reserved")
    if model["result_type"] not in TYPES:
        raise Abstain("Unsupported result type")
    functions, declarations = [], []
    environment = model.get("environment", [])
    if len(environment) > 20:
        raise Abstain("Environment budget exceeded")
    for function in environment:
        fn = name(function["name"])
        if fn in functions or fn in params:
            raise Abstain("Environment name collision")
        names, sig = parameters(function["parameters"])
        if function["result_type"] not in TYPES:
            raise Abstain("Unsupported environment result type")
        if not function.get("source") or not function.get("description"):
            raise Abstain("Environment definitions need source and meaning")
        body = expression(function["body"], names, functions)
        declarations.append(f'function {fn}({sig}): {function["result_type"]} {{ {body} }}')
        functions.append(fn)
    preconditions = model.get("preconditions", [])
    assumptions = model.get("assumptions", [])
    if len(preconditions) + len(assumptions) > 20:
        raise Abstain("Assumption budget exceeded")
    if retained is None:
        retained = list(range(len(assumptions)))
    contracts = [expression(x["expression"], params, functions) for x in preconditions]
    optional = [expression(x["expression"], params, functions) for x in assumptions]
    if any(not x.get("description") for x in preconditions + assumptions):
        raise Abstain("Conditions need developer-language descriptions")
    if any(x.get("origin", "inferred") != "inferred" for x in preconditions + assumptions):
        raise Abstain("Model-supplied preconditions are inferred, not certified source contracts")
    body = expression(model["body"], params, functions)
    prop = expression(model["property"], [*params, "result"], functions)
    witness = model["witness"]
    if set(witness) != set(params):
        raise Abstain("Concrete witness must assign every input exactly once")
    # Literal-only witnesses: no symbolic variables, calls, or unknown inputs.
    def literal(value, typ):
        if typ == "int" and type(value) is int:
            return expression(value, [])
        if typ == "bool" and type(value) is bool:
            return expression(value, [])
        if typ.startswith("seq<") and isinstance(value, list) and len(value) <= 100:
            base = typ[4:-1]
            return "[" + ", ".join(literal(x, base) for x in value) + "]"
        raise Abstain("Witness must be a type-correct concrete literal")
    requires = "\n".join("  requires " + x for x in
                         contracts + [optional[i] for i in retained])
    declarations.append(f'function NfvTarget({signature}): {model["result_type"]}\n'
                        f"{requires}\n{{ {body} }}")
    lines = ["method NfvRefutation()", "{"]
    for item in model["parameters"]:
        lines.append(f'  var {item["name"]}: {item["type"]} := '
                     f'{literal(witness[item["name"]], item["type"])};')
    # Keep original applicability checks even after dependency minimization.
    lines.extend("  assert " + x + ";" for x in contracts + optional)
    lines.append("  var result := NfvTarget(" + ", ".join(params) + ");")
    lines.append("  assert !(" + prop + ");")
    lines.append("}")
    return "\n\n".join(declarations) + "\n\n" + "\n".join(lines) + "\n"
