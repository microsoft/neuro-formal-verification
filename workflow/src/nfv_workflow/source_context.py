# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bounded lexical source context, not a source-language semantic frontend."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import textwrap
from collections import defaultdict, deque
from dataclasses import dataclass

from .github import Abstain
from .outcomes import ResourcesExhausted

LANGUAGES = {
    ".c": "c", ".h": "cpp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".java": "java", ".cs": "csharp", ".go": "go",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".py": "python", ".rs": "rust",
}
CONTEXT_BYTES = 32000
REQUEST_BYTES = 80000
RELATED_LIMIT = 4
ENVIRONMENT_LIMIT = 12
CPP_OPERATOR_SYMBOL = (
    r"(?:\(\)|\[\]|<<=?|>>=?|->\*?|<=>|[-+*/%&|^!=<>]=?|&&|\|\||\+\+|--|~|,)")
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
CONTROL = {"if", "for", "while", "switch", "catch", "match", "with", "sizeof",
           "decltype", "noexcept", "return", "assert", "SASSERT", "VERIFY"}
LITERAL_PREFIX = (
    r'//[^\n]*|/\*[\s\S]*?\*/|R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\([\s\S]*?\)(?P=delimiter)"'
    r'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`')
LITERALS = re.compile(LITERAL_PREFIX + r"|'(?:\\.|[^'\\])*'")
RUST_LITERALS = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|r(?P<hashes>#{0,16})"[\s\S]*?"(?P=hashes)'
    r'|"(?:\\[\s\S]|[^"\\])*"' + r"|'(?:\\.|[^'\\\n])'")


def unqualified_name(name):
    name = name.rsplit("::", 1)[-1]
    return "operator" if re.fullmatch("operator" + CPP_OPERATOR_SYMBOL, name) else name


@dataclass(frozen=True)
class Function:
    name: str
    first: int
    declaration: int
    body: int
    last: int
    calls: frozenset[str]


def mask(source, language=None):
    pattern = RUST_LITERALS if language == "rust" else LITERALS
    return pattern.sub(lambda match: "".join("\n" if c == "\n" else " " for c in match[0]), source)


def comment_start(lines, declaration):
    first = declaration
    while first > 1:
        previous = lines[first - 2].strip()
        if not previous or not previous.startswith(("#", "//", "/*", "*", "@", "[")):
            break
        first -= 1
    return first


def functions(record, language):
    source = record["text"]
    first = record.get("line_start", 1)
    lines = source.splitlines()
    if language == "python":
        try:
            tree = ast.parse(textwrap.dedent(source))
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise Abstain("Cannot identify Python function boundaries in captured source") from exc
        result = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            declaration = min([node.lineno] + [x.lineno for x in node.decorator_list])
            calls = frozenset(
                call.func.id if isinstance(call.func, ast.Name) else call.func.attr
                for call in ast.walk(node) if isinstance(call, ast.Call)
                and isinstance(call.func, (ast.Name, ast.Attribute)))
            result.append(Function(node.name, first + comment_start(lines, declaration) - 1,
                                   first + node.lineno - 1, first + node.body[0].lineno - 1,
                                   first + node.end_lineno - 1, calls))
        return result
    clean = mask(source, language)
    pairs, stack = {}, []
    separators = list(clean) if language == "rust" else None
    brackets = 0
    for index, char in enumerate(clean):
        if separators is not None:
            if char == "[":
                brackets += 1
            elif char == "]":
                brackets -= 1
            elif char == ";" and brackets > 0:
                # Rust array-type semicolons do not end the function signature.
                separators[index] = " "
        if char == "{":
            stack.append(index)
        elif char == "}":
            if not stack:
                raise Abstain("Cannot identify function boundaries: unbalanced source braces")
            pairs[stack.pop()] = index
    if stack:
        raise Abstain("Cannot identify function boundaries: incomplete captured source braces")
    boundaries = clean if separators is None else "".join(separators)
    result = []
    for opening, closing in sorted(pairs.items()):
        start = max(boundaries.rfind(";", 0, opening), clean.rfind("{", 0, opening),
                    clean.rfind("}", 0, opening)) + 1
        header = clean[start:opening]
        # A declaration must start with a signature, not a control block or assignment.
        header = re.sub(r"(?s)\[[^\]]*\]", "", header)
        header = re.sub(r"@\w+(?:\([^)]*\))?", "", header).strip()
        if not header or len(header) > 4000:
            continue
        if header.count("(") != header.count(")"):
            continue
        keyword = r"fn" if language == "rust" else r"(?:fn|function)"
        explicit = re.search(r"\b" + keyword + r"\s+(\w+)\s*(?:<[^{}]*>)?\s*\(", header)
        go = re.search(r"\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*\(", header)
        arrow = re.search(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?"
                          r"(?:\([^)]*\)|[A-Za-z_]\w*)\s*(?::\s*[^=]+)?=>\s*$", header)
        operator = re.search(r"\b((?:[A-Za-z_]\w*::)*operator)\s*"
                             + CPP_OPERATOR_SYMBOL + r"\s*\(", header)
        ordinary = re.search(r"\b([A-Za-z_]\w*(?:::\w+)*)\s*\(", header)
        match = explicit if language == "rust" else (explicit or go or arrow or operator or ordinary)
        if not match or match[1].split("::")[-1] in CONTROL:
            continue
        if not (explicit or go or arrow):
            prefix = header[:match.start()]
            if any(token in prefix for token in ("=", ".", "->")) or re.search(
                    r"\b(?:return|new|throw|if|for|while|switch|catch)\b", prefix):
                continue
            tail = header[match.end():]
            if "=>" in tail:
                continue
        signature_start = start + len(clean[start:opening]) - len(clean[start:opening].lstrip())
        declaration = clean.count("\n", 0, signature_start) + 1
        body = clean.count("\n", 0, opening) + 1
        last = clean.count("\n", 0, closing) + 1
        result.append(Function(unqualified_name(match[1]),
                               first + comment_start(lines, declaration) - 1,
                               first + declaration - 1, first + body - 1, first + last - 1,
                               frozenset(CALL.findall(clean[opening + 1:closing])) - CONTROL))
    return result


def excerpt(record, low, high):
    first = record.get("line_start", 1)
    lines = record["text"].splitlines()
    return "\n".join(f"{n}| {lines[n - first]}" for n in range(low, high + 1))


def size(value):
    import json
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def entry_function(definitions, anchors):
    targets = set()
    for line in anchors:
        enclosing = sorted((fn for fn in definitions if fn.first <= line <= fn.last),
                           key=lambda fn: fn.last - fn.first)
        if not enclosing:
            raise Abstain("Changed anchors must belong to one identifiable function")
        if len(enclosing) > 1 and enclosing[0].last - enclosing[0].first == enclosing[1].last - enclosing[1].first:
            raise Abstain("Changed anchors have ambiguous function boundaries")
        targets.add(enclosing[0])
    if len(targets) != 1:
        raise Abstain("Changed anchors must belong to one identifiable function")
    return targets.pop()


def function_catalog(bundle):
    """Identify changed entries before asking a model to select properties."""
    from pathlib import PurePosixPath
    catalog = {"functions": [], "errors": []}
    for path, changed in sorted(bundle["changed"].items()):
        language = LANGUAGES.get(PurePosixPath(path).suffix.lower())
        if not language:
            continue
        record = bundle["sources"].get(path)
        if record is None:
            catalog["errors"].append({"path": path, "diagnostic": "Changed source was not captured"})
            continue
        try:
            definitions = functions(record, language)
        except Abstain as exc:
            catalog["errors"].append({"path": path, "diagnostic": str(exc)})
            continue
        anchors, unscoped = defaultdict(list), []
        source_lines = record["text"].splitlines()
        offset = record.get("line_start", 1)
        source_hash = hashlib.sha256(record["text"].encode("utf-8")).hexdigest()
        for line in sorted(set(changed)):
            index = line - offset
            if not 0 <= index < len(source_lines):
                continue
            text = source_lines[index].strip()
            if not text or text.startswith(("//", "#", "/*", "*")) or re.fullmatch(r"[{}();\[\]]+", text):
                continue
            try:
                anchors[entry_function(definitions, [line])].append(line)
            except Abstain:
                unscoped.append(line)
        if unscoped:
            catalog["errors"].append({
                "path": path, "lines": unscoped[:30], "total": len(unscoped),
                "diagnostic": "Changed lines outside an unambiguous captured function are not entry candidates",
            })
        for fn, lines in sorted(anchors.items(), key=lambda pair: pair[0].declaration):
            identity = json.dumps([path, language, fn.declaration, fn.last, source_hash],
                                  ensure_ascii=False).encode("utf-8")
            catalog["functions"].append({
                "id": "function-" + hashlib.sha256(identity).hexdigest()[:20],
                "path": path, "name": fn.name, "language": language,
                "first": fn.first, "declaration": fn.declaration, "last": fn.last,
                "lines": lines,
            })
    return catalog


def root_levels(definitions):
    """Caller-first levels of the lexical graph, with recursive groups kept together."""
    names = defaultdict(list)
    for index, fn in enumerate(definitions):
        names[fn.name].append(index)
    graph = [set(other for name in fn.calls for other in names[name]) for fn in definitions]
    reverse = [set() for _ in definitions]
    for node, edges in enumerate(graph):
        for other in edges:
            reverse[other].add(node)
    seen, finished = set(), []
    for start in range(len(graph)):
        stack = [(start, False)]
        while stack:
            node, done = stack.pop()
            if done:
                finished.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((other, False) for other in sorted(graph[node], reverse=True))
    groups, group_count = {}, 0
    for start in reversed(finished):
        if start in groups:
            continue
        group = group_count
        group_count += 1
        stack = [start]
        groups[start] = group
        while stack:
            node = stack.pop()
            for other in reverse[node]:
                if other not in groups:
                    groups[other] = group
                    stack.append(other)
    edges = [set() for _ in range(group_count)]
    incoming = [0] * len(edges)
    for node, children in enumerate(graph):
        for child in children:
            if groups[node] != groups[child]:
                edges[groups[node]].add(groups[child])
    for children in edges:
        for child in children:
            incoming[child] += 1
    levels = [0] * len(edges)
    ready = deque(index for index, count in enumerate(incoming) if not count)
    while ready:
        group = ready.popleft()
        for child in edges[group]:
            levels[child] = max(levels[child], levels[group] + 1)
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    return {fn: levels[groups[index]] for index, fn in enumerate(definitions)}


def caller_first(bundle, claims):
    """Order selected properties; unscopable claims still abstain in run_property."""
    files, ranks = {}, {}
    for index, claim in enumerate(claims):
        key = (claim["path"], claim["language"])
        try:
            if key not in files:
                definitions = functions(bundle["sources"][claim["path"]], claim["language"])
                files[key] = (definitions, root_levels(definitions))
            definitions, levels = files[key]
            ranks[index] = levels[entry_function(definitions, claim["lines"])]
        except Abstain:
            # The same boundary check is repeated by scoped_context and reported per claim.
            ranks[index] = float("inf")
    return [claim for index, claim in sorted(enumerate(claims), key=lambda item: ranks[item[0]])]


def declarations(record, language):
    """Captured type and constant definitions; strings/comments are not declarations."""
    offset = record.get("line_start", 1) - 1
    source = record["text"]
    if language == "python":
        for node in ast.parse(textwrap.dedent(source)).body:
            names = []
            first = node.lineno
            if isinstance(node, ast.ClassDef):
                names = [node.name]
                first = min([first] + [item.lineno for item in node.decorator_list])
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [target.id for target in targets if isinstance(target, ast.Name)]
            for name in names:
                yield name, first + offset, node.end_lineno + offset
        return
    clean = mask(source, language)
    for match in re.finditer(r"\b(?:enum\s+class|class|struct|enum|interface)\s+([A-Za-z_]\w*)", clean):
        end = match.end()
        brace, semicolon = clean.find("{", end), clean.find(";", end)
        if brace >= 0 and (semicolon < 0 or brace < semicolon):
            depth = 1
            end = brace + 1
            while end < len(clean) and depth:
                depth += (clean[end] == "{") - (clean[end] == "}")
                end += 1
        yield match[1], clean.count("\n", 0, match.start()) + 1 + offset, clean.count("\n", 0, end) + 1 + offset
    for match in re.finditer(r"(?m)^[^\n]*\b(?:const|constexpr)\b[^\n]*", clean):
        assignment = re.search(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*=", match[0])
        end = clean.find(";", match.start() + assignment.end()) if assignment else -1
        if assignment and end >= 0:
            yield assignment[1], clean.count("\n", 0, match.start()) + 1 + offset, clean.count("\n", 0, end) + 1 + offset


def scoped_context(bundle, claim):
    path = claim["path"]
    record = bundle["sources"][path]
    local = functions(record, claim["language"])
    target = entry_function(local, claim["lines"])
    selected = [target]
    captured_omissions = bundle.get("context_omissions", [])
    omissions = list(captured_omissions[:30])
    if len(captured_omissions) > 30:
        omissions.append(f"{len(captured_omissions) - 30} additional capture omissions remain in the full bundle.")
    omissions.append("All other function bodies and files are omitted; model their calls as environment.")
    context = {
        "source_scope": {"path": path, "function": target.name,
                         "first": target.first, "last": target.last,
                         "policy": "Translate this changed function; other calls are environment."},
        "sources": {path: {"blob_sha": record.get("blob_sha"),
                           "numbered_lines": excerpt(record, target.first, target.last),
                           "ranges": [[target.first, target.last]]}},
        "related_functions": [], "environment_declarations": [],
        "context_omissions": omissions,
    }
    if size(context) > CONTEXT_BYTES:
        raise ResourcesExhausted("Changed function exceeds the scoped source context byte budget")
    callees = sorted((fn for fn in local if fn != target and fn.name in target.calls
                      and not target.first <= fn.first <= fn.last <= target.last),
                     key=lambda fn: abs(fn.first - target.first))
    callers = sorted((fn for fn in local if fn != target and target.name in fn.calls),
                     key=lambda fn: abs(fn.first - target.first))
    candidates = [(fn, "callee") for fn in callees[:2]] + [(fn, "caller") for fn in callers[:2]]
    candidates.sort(key=lambda item: (item[1] != "callee", abs(item[0].first - target.first)))
    for fn, role in candidates[:RELATED_LIMIT]:
        if fn in selected or any(other.first <= fn.first and fn.last <= other.last for other in selected):
            continue
        addition = excerpt(record, fn.first, fn.last)
        view = context["sources"][path]
        previous = view["numbered_lines"]
        view["numbered_lines"] += "\n\n" + addition
        view["ranges"].append([fn.first, fn.last])
        context["related_functions"].append({"name": fn.name, "role": role,
                                            "first": fn.first, "last": fn.last})
        if size(context) > CONTEXT_BYTES:
            view["numbered_lines"] = previous
            view["ranges"].pop()
            context["related_functions"].pop()
            omissions.append(f"{path}:{fn.declaration}: {role} body omitted by scoped context budget")
        else:
            selected.append(fn)
    calls = set().union(*(fn.calls for fn in selected))
    environment = calls - {fn.name for fn in selected}
    context["callee_names"] = sorted(environment)

    def add_environment(other_path, other_record, name, low, high):
        item = {"path": other_path, "name": name, "first": low, "last": high,
                "mode": "environment-only"}
        if item in context["environment_declarations"]:
            return
        view = context["sources"].setdefault(other_path, {
            "blob_sha": other_record.get("blob_sha"), "numbered_lines": "", "ranges": []})
        previous = view["numbered_lines"]
        view["numbered_lines"] += ("\n\n" if previous else "") + excerpt(other_record, low, high)
        view["ranges"].append([low, high])
        context["environment_declarations"].append(item)
        if size(context) > CONTEXT_BYTES:
            context["environment_declarations"].pop()
            view["numbered_lines"] = previous
            view["ranges"].pop()
            if not view["ranges"]:
                del context["sources"][other_path]
            omissions.append(f"{other_path}:{low}: {name} omitted by scoped context budget")

    identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", context["sources"][path]["numbered_lines"]))
    for item in bundle.get("repository_context", []):
        other_path = item["path"]
        other_record = bundle["sources"][other_path]
        other_language = "python" if other_path.endswith(".py") else (
            "rust" if other_path.endswith(".rs") else
            "cpp" if claim["language"] == "python" else claim["language"])
        definitions = functions(other_record, other_language)
        for fn in definitions:
            if fn.name in item["symbols"] and not (other_path == path and fn in selected):
                add_environment(other_path, other_record, fn.name, fn.first, fn.last)
        for name, first, last in declarations(other_record, other_language):
            if name in item["symbols"]:
                add_environment(other_path, other_record, name, first, last)
    for other_path, other_record in bundle["sources"].items():
        if len(context["environment_declarations"]) >= ENVIRONMENT_LIMIT:
            break
        other_language = "python" if other_path.endswith(".py") else (
            "rust" if other_path.endswith(".rs") else
            "cpp" if claim["language"] == "python" else claim["language"])
        try:
            definitions = local if other_path == path else functions(other_record, other_language)
        except Abstain:
            omissions.append(f"{other_path}: environment declarations unavailable")
            continue
        lines = other_record["text"].splitlines()
        offset = other_record.get("line_start", 1) - 1
        clean_lines = mask(other_record["text"], other_language).splitlines()
        for name, first, last in declarations(other_record, other_language):
            if name in identifiers and len(context["environment_declarations"]) < ENVIRONMENT_LIMIT:
                add_environment(other_path, other_record, name, first, last)
        for index, line in enumerate(clean_lines, 1):
            if len(context["environment_declarations"]) >= ENVIRONMENT_LIMIT:
                break
            if any(fn.first <= index + offset <= fn.last for fn in definitions):
                continue
            declaration = None if other_language == "python" else re.search(
                r"\b(?:type|typedef|using)\b", line)
            if declaration and not CALL.search(line):
                names = set(re.findall(r"\b[A-Za-z_]\w*\b", line))
                names -= {"class", "struct", "enum", "interface", "type", "typedef", "using",
                          "const", "constexpr", "static", "public", "private", "int", "bool",
                          "unsigned", "long", "float", "double", "char", "string", "void"}
                for name in sorted(names & identifiers)[:1]:
                    add_environment(other_path, other_record, name,
                                    comment_start(lines, index) + offset, index + offset)
        for fn in definitions:
            if fn.name not in environment or (other_path == path and fn in selected):
                continue
            # Only leading comments and signature lines, never an external function body.
            high = fn.body - 1
            if other_language != "python":
                source_lines = other_record["text"].splitlines()
                body_line = source_lines[fn.body - other_record.get("line_start", 1)]
                clean_body_line = mask(body_line, other_language)
                if "{" in clean_body_line and not clean_body_line.split("{", 1)[1].strip():
                    high = fn.body
            if high < fn.first:
                continue
            if len(context["environment_declarations"]) >= ENVIRONMENT_LIMIT:
                break
            add_environment(other_path, other_record, fn.name, fn.first, high)
        for index, line in enumerate(clean_lines, 1):
            if len(context["environment_declarations"]) >= ENVIRONMENT_LIMIT:
                break
            if any(fn.first <= index + offset <= fn.last for fn in definitions):
                continue
            declaration = re.match(r"^\s*(?:[A-Za-z_]\w*(?:::\w+)*[ *&]+)+(\w+)\s*\([^{};]*\)\s*(?:const\s*)?;", line)
            if declaration and declaration[1] in environment:
                add_environment(other_path, other_record, declaration[1],
                                comment_start(lines, index) + offset, index + offset)
    if size(context) > CONTEXT_BYTES:
        omitted = 0
        omissions.append("")
        while len(omissions) > 1 and size(context) > CONTEXT_BYTES:
            omissions.pop(-2)
            omitted += 1
            omissions[-1] = f"{omitted} additional context-omission notices omitted from this bounded prompt."
    if size(context) > CONTEXT_BYTES:
        raise ResourcesExhausted("Scoped source context exceeds its byte budget")
    return context


def check_request(system, prompt, stage):
    # UTF-8 bytes conservatively bound byte-tokenizer input; leave ample output/SDK headroom.
    if len(system.encode("utf-8")) + len(prompt.encode("utf-8")) > REQUEST_BYTES:
        raise ResourcesExhausted(f"{stage} request exceeds the {REQUEST_BYTES}-byte model prompt budget")


def quoted_context(context, model):
    """Deduplicate exact captured rows without losing source text or indentation."""
    from .general_gates import SRC
    quoted = {}
    for index, line in enumerate(model.splitlines(), 1):
        tag = SRC.fullmatch(line)
        if tag:
            quoted[(tag[1], int(tag[2]), tag[3].strip())] = index
    sources, changed = {}, False
    for path, record in context["sources"].items():
        rows, references = [], []
        for row in record["numbered_lines"].splitlines():
            match = re.fullmatch(r"(\d+)\| (.*)", row)
            if match and match[2].strip():
                text = match[2]
                index = quoted.get((path, int(match[1]), text.strip()))
                if index is not None:
                    reference = [int(match[1]), index, text[:len(text) - len(text.lstrip())],
                                 text[len(text.rstrip()):]]
                    if len(json.dumps(reference, ensure_ascii=False).encode("utf-8")) + 10 < len(row.encode("utf-8")):
                        references.append(reference)
                        continue
            rows.append(row)
        sources[path] = {**record, "numbered_lines": "\n".join(rows)}
        if references:
            sources[path]["quoted_in_model"] = references
            changed = True
    if not changed:
        return context
    result = {**context, "sources": sources,
              "source_text_references": "quoted_in_model rows are [source line, model tag line, "
              "leading whitespace, trailing whitespace]. Reconstruct the exact captured source "
              "from that src tag's trimmed excerpt and saved whitespace. Unreferenced rows remain "
              "verbatim below. This is deduplication, not permission to omit source behavior."}
    return result if size(result) < size(context) else context


def diagnostics(result):
    """Keep error locations and tails, with explicit truncation; full logs remain in evidence."""
    output = dict(result)
    for key in ("stdout", "stderr"):
        value = output.get(key, "")
        if len(value.encode("utf-8")) > 8000:
            raw = value.encode("utf-8")
            output[key] = (raw[:3500].decode("utf-8", errors="ignore")
                           + "\n[verifier output omitted from repair prompt; see full evidence log]\n"
                           + raw[-3500:].decode("utf-8", errors="ignore"))
    return output
