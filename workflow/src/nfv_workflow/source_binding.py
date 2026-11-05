# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Fail-closed correspondence for a small scalar C/C++ source subset.

This is a parser/interpreter, never a compiler or execution of repository code.
Broader models may be useful hypotheses, but are not publishable findings.
"""

import re
import posixpath

from .github import Abstain

TOKEN = re.compile(r"\s*(\d+|[A-Za-z_]\w*|==|!=|<=|>=|&&|\|\||[()+*/%!,<>?:-])")
PRECEDENCE = {"||": 1, "&&": 2, "==": 3, "!=": 3,
              "<": 4, "<=": 4, ">": 4, ">=": 4, "+": 5, "-": 5,
              "*": 6, "/": 6, "%": 6}


class Parser:
    def __init__(self, source):
        self.tokens = []
        source = source.strip()
        while source:
            match = TOKEN.match(source)
            if not match:
                raise Abstain("Unsupported token in source expression")
            self.tokens.append(match[1])
            source = source[match.end():]
        self.offset = 0

    def peek(self):
        return self.tokens[self.offset] if self.offset < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise Abstain("Unsupported source expression syntax")
        self.offset += 1
        return token

    def expression(self, minimum=0):
        token = self.take()
        if token == "(":
            left = self.expression()
            self.take(")")
        elif token in ("!", "-"):
            operand = self.expression(7)
            left = {"not": operand} if token == "!" else (
                -operand if type(operand) is int else {"-": [0, operand]})
        elif token.isdigit():
            if len(token) > 1 and token.startswith("0"):
                raise Abstain("Octal source literals require a richer frontend")
            left = int(token)
        elif token in ("true", "false"):
            left = token == "true"
        elif re.fullmatch(r"[A-Za-z_]\w*", token):
            left = token
            if self.peek() == "(":
                self.take()
                arguments = []
                if self.peek() != ")":
                    arguments.append(self.expression())
                    while self.peek() == ",":
                        self.take()
                        arguments.append(self.expression())
                self.take(")")
                left = {"call": [token, *arguments]}
        else:
            raise Abstain("Unsupported source expression")
        while self.peek() in PRECEDENCE and PRECEDENCE[self.peek()] >= minimum:
            operator = self.take()
            right = self.expression(PRECEDENCE[operator] + 1)
            left = {operator: [left, right]}
        if minimum == 0 and self.peek() == "?":
            self.take()
            then = self.expression()
            self.take(":")
            left = {"if": [left, then, self.expression()]}
        return left

    def parse(self):
        result = self.expression()
        if self.peek() is not None:
            raise Abstain("Trailing source expression tokens")
        return result


def mask(source):
    if "\\" in source or "??" in source:
        raise Abstain("Escapes, line splicing, and trigraphs require a richer frontend")
    if re.search(r'(?m)^\s*#\s*(?!include\b|pragma\s+once\b)', source):
        raise Abstain("Source preprocessor conditionals/macros require a richer frontend")
    if re.search(r'\b(?:R|u8R|uR|UR|LR)"', source):
        raise Abstain("Raw strings require a richer source frontend")
    pattern = r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    clean = re.sub(pattern, lambda m: "".join("\n" if c == "\n" else " " for c in m[0]), source)
    if "/*" in clean or "*/" in clean:
        raise Abstain("Unbalanced source comments")
    return clean


def source_function(bundle, path, anchors):
    record = bundle["sources"][path]
    source = record["text"]
    if record.get("line_start", 1) != 1:
        raise Abstain("Publishable source binding needs complete declaration context, not an excerpt")
    clean = mask(source)
    pattern = re.compile(
        r"\b(int|bool)\s+([A-Za-z_]\w*)\s*\(([^{};]*)\)\s*\{([^{}]*)\}")
    matches = []
    for match in pattern.finditer(clean):
        prefix = clean[clean.rfind("\n", 0, match.start()) + 1:match.start()]
        if prefix.strip():
            continue
        if clean[:match.start()].count("{") != clean[:match.start()].count("}"):
            continue
        first = clean.count("\n", 0, match.start()) + 1
        last = clean.count("\n", 0, match.end()) + 1
        if all(first <= line <= last for line in anchors):
            matches.append(match)
    if len(matches) != 1:
        raise Abstain("No unique supported source function contains the changed anchors")
    match = matches[0]
    parameters = []
    for parameter in match[3].split(",") if match[3].strip() not in ("", "void") else []:
        parsed = re.fullmatch(r"\s*(int|bool)\s+([A-Za-z_]\w*)\s*", parameter)
        if not parsed:
            raise Abstain("Source binding supports only named int/bool scalar parameters")
        parameters.append({"name": parsed[2], "type": parsed[1]})
    if len({p["name"] for p in parameters}) != len(parameters):
        raise Abstain("Duplicate source parameter names")
    body = re.fullmatch(r"\s*return\s+([\s\S]+?)\s*;\s*", match[4])
    if not body:
        raise Abstain("Source binding supports only one straight-line return expression")
    return {"name": match[2], "parameters": parameters, "result_type": match[1],
            "body": Parser(body[1]).parse(),
            "start_line": clean.count("\n", 0, match.start()) + 1}


def evaluate(node, values, functions, integer_checks=False):
    def checked(value):
        if integer_checks and type(value) is int and not -(2**31) <= value < 2**31:
            raise Abstain("Concrete source calculation may overflow a signed 32-bit int")
        return value
    recur = lambda n: evaluate(n, values, functions, integer_checks)
    if type(node) in (bool, int):
        return checked(node)
    if isinstance(node, str):
        if node not in values:
            raise Abstain("Source expression refers to uncaptured state")
        return checked(values[node])
    operator, args = next(iter(node.items()))
    if operator == "not":
        return not recur(args)
    if operator == "if":
        return recur(args[1] if recur(args[0]) else args[2])
    if operator == "call":
        if args[0] not in functions:
            raise Abstain("Source call has no independently matched callee")
        function = functions[args[0]]
        arguments = [recur(x) for x in args[1:]]
        if len(arguments) != len(function["parameters"]):
            raise Abstain("Source call arity mismatch")
        bindings = {p["name"]: value for p, value in zip(function["parameters"], arguments)}
        return evaluate(function["body"], bindings, functions, integer_checks)
    if operator == "seq":
        return [recur(x) for x in args]
    if operator == "length":
        return len(recur(args))
    if operator == "index":
        return recur(args[0])[recur(args[1])]
    left = recur(args[0])
    if operator == "&&":
        return bool(left) and bool(recur(args[1]))
    if operator == "||":
        return bool(left) or bool(recur(args[1]))
    right = recur(args[1])
    if operator in ("/", "%"):
        if right == 0 or left < 0 or right < 0:
            raise Abstain("Division needs nonnegative operands and a nonzero divisor for C/Dafny agreement")
        return checked(left // right if operator == "/" else left % right)
    operations = {"+": lambda: left + right, "-": lambda: left - right,
                  "*": lambda: left * right, "==": lambda: left == right,
                  "!=": lambda: left != right, "<": lambda: left < right,
                  "<=": lambda: left <= right, ">": lambda: left > right,
                  ">=": lambda: left >= right}
    if operator not in operations:
        raise Abstain("Unsupported expression in source correspondence check")
    return checked(operations[operator]())


def cross_file_leaf(bundle, caller, definition, function):
    if bundle.get("source_inventory_complete") is not True:
        raise Abstain("Cross-file binding needs the complete immutable regular-file inventory")
    inventory = set(bundle["source_inventory"])
    captured = bundle["sources"]
    pending, headers = [caller, definition], set()
    while pending:
        path = pending.pop()
        if path not in captured or captured[path].get("line_start", 1) != 1:
            raise Abstain("Include closure is not completely captured")
        raw = captured[path]["text"]
        clean = mask(raw)
        if re.search(r"\b(template|namespace|class|struct|typedef|using|extern)\b", clean):
            raise Abstain("Cross-file overload/linkage context is unsupported")
        includes = re.findall(r'(?m)^\s*#\s*include\s+([<"])([^>"]+)[>"]\s*$', raw)
        if len(includes) != len(re.findall(r'(?m)^\s*#\s*include\b', raw)):
            raise Abstain("Computed or noncanonical include is unsupported")
        for delimiter, name in includes:
            if delimiter != '"':
                raise Abstain("Cross-file binding requires explicit relative quoted includes")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), name))
            if resolved.startswith("../") or resolved not in inventory or not resolved.endswith(".h"):
                raise Abstain("Quoted header does not resolve to an immutable regular header")
            if resolved not in headers:
                headers.add(resolved)
                pending.append(resolved)
    if not headers:
        raise Abstain("Cross-file callee has no captured include declaration")

    name = function["name"]
    if re.search(rf"\b(?:int|bool)\s+{re.escape(name)}\s*\(", mask(captured[caller]["text"])):
        raise Abstain("Caller has a competing local declaration for the cross-file callee")
    declarations = []
    for path in sorted(headers):
        clean = mask(captured[path]["text"])
        # Every occurrence of this name in a header must be the one supported
        # global declaration, not an overload, macro, member, or hidden call.
        pattern = re.compile(rf"\b(int|bool)\s+{re.escape(name)}\s*\(([^;{{}}]*)\)\s*;")
        matches = list(pattern.finditer(clean))
        if len(matches) != len(re.findall(rf"\b{re.escape(name)}\b", clean)):
            raise Abstain("Header callee references are not a unique plain scalar declaration")
        for match in matches:
            prefix = clean[clean.rfind("\n", 0, match.start()) + 1:match.start()]
            if prefix.strip() or clean[:match.start()].count("{") != clean[:match.start()].count("}"):
                raise Abstain("Header declaration has unsupported qualifiers or scope")
            types = []
            for parameter in match[2].split(",") if match[2].strip() not in ("", "void") else []:
                part = re.fullmatch(r"\s*(int|bool)(?:\s+[A-Za-z_]\w*)?\s*", parameter)
                if not part:
                    raise Abstain("Header prototype is not scalar")
                types.append(part[1])
            declarations.append((path, match[1], types))
    if len(declarations) != 1:
        raise Abstain("Cross-file callee declaration is absent or overloaded")
    header, result_type, types = declarations[0]
    if result_type != function["result_type"] or types != [x["type"] for x in function["parameters"]]:
        raise Abstain("Header prototype and captured definition disagree")
    stem = header[:-2]
    implementations = [stem + ext for ext in (".c", ".cc", ".cpp", ".cxx")
                       if stem + ext in inventory]
    if implementations != [definition]:
        raise Abstain("Header does not identify exactly one same-basename implementation")
    raw_definition = captured[definition]["text"]
    clean_definition = mask(raw_definition)
    if re.search(r"\b(template|namespace|class|struct|typedef|using|extern)\b", clean_definition):
        raise Abstain("Callee implementation linkage context is unsupported")
    definitions = re.findall(rf"\b(?:int|bool)\s+{re.escape(name)}\s*\(", clean_definition)
    if len(definitions) != 1:
        raise Abstain("Callee implementation has ambiguous declarations")
    def has_call(node):
        if isinstance(node, dict):
            return "call" in node or any(has_call(x) for x in node.values())
        return isinstance(node, list) and any(has_call(x) for x in node)
    if has_call(function["body"]):
        raise Abstain("Cross-file environment currently supports leaf functions only")
    return header


def bind_source(candidate, bundle):
    model = candidate["model"]
    source = source_function(bundle, candidate["path"], candidate["lines"])
    for key in ("parameters", "result_type", "body"):
        if source[key] != model[key]:
            raise Abstain(f"Proposed {key} does not match independently parsed source")
    functions, contracts = {}, []
    for function in model.get("environment", []):
        origin = function["source"]
        parsed = source_function(bundle, origin["path"], origin["lines"])
        if parsed["name"] == source["name"]:
            raise Abstain("Recursive or shadowed source entry calls are unsupported")
        cross_file = origin["path"] != candidate["path"]
        if not cross_file and parsed["start_line"] >= source["start_line"]:
            raise Abstain("Only preceding same-file scalar callees are supported")
        for key in ("name", "parameters", "result_type", "body"):
            if parsed[key] != function[key]:
                raise Abstain(f"Environment {key} does not match independently parsed source")
        header = cross_file_leaf(bundle, candidate["path"], origin["path"], parsed) if cross_file else None
        # Evaluating a callee may call only earlier bound functions; recursion and
        # cycles are additionally rejected by the model grammar.
        functions[function["name"]] = parsed
        contracts.append({"function": parsed["name"], "definition": origin["path"],
                          "declaration": header, "parameters": parsed["parameters"],
                          "result_type": parsed["result_type"], "body": parsed["body"],
                          "origin": "source-derived-definition"})
    result = evaluate(source["body"], model["witness"], functions, integer_checks=True)
    predicate = evaluate(model["property"], {**model["witness"], "result": result}, functions)
    if predicate is not False:
        raise Abstain("Source-matched concrete calculation does not falsify the modeled expectation")
    conditions = [
        "The captured declaration is compiled without macro substitution or a different overload.",
        "Source int parameters and arithmetic use signed 32-bit values; the concrete calculation stays in range.",
        "The concrete inputs satisfy any external API or caller preconditions not expressed in the captured declaration.",
        "The displayed formalized expectation accurately represents the quoted developer claim."
    ]
    if any(x["declaration"] for x in contracts):
        conditions.append("The build links each captured cross-file leaf, identified by the unique same-basename implementation of its quoted header.")
    return {"status": "matched-scalar-source", "result": result,
            "function": source["name"],
            "source_derived_callee_contracts": contracts,
            "conditions": conditions}
