# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""GitHub data is read as text, never checked out or executed."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import urllib.parse
import urllib.request


class Abstain(ValueError):
    """Missing or unsupported evidence; not a finding."""


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise Abstain("Expected an immutable, full commit SHA")
    return value


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        raise Abstain("Invalid repository")
    return value


def source_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise Abstain("Invalid Git source path")
    if value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise Abstain("Unsafe Git source path")
    if any(ord(c) < 32 for c in value):
        raise Abstain("Control character in source path")
    return value


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")

    def request(self, path, data=None):
        url = "https://api.github.com/" + path.lstrip("/")
        body = None if data is None else json.dumps(data).encode()
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nfv-workflow"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    def pages(self, path, max_pages=100):
        rows = []
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in path else "?"
            part = self.request(f"{path}{sep}per_page=100&page={page}")
            if not isinstance(part, list):
                raise Abstain("Unexpected paginated response")
            rows.extend(part)
            if len(part) < 100:
                return rows
        raise Abstain("Pagination budget exceeded; refusing incomplete evidence")

    def graphql(self, query, variables):
        result = self.request("graphql", {"query": query, "variables": variables})
        if result.get("errors"):
            raise Abstain("GitHub GraphQL failed; no publication attempted")
        return result["data"]

    def blob(self, repo, oid):
        result = self.request(f"repos/{repository(repo)}/git/blobs/{sha(oid)}")
        if result.get("size", 0) > 1_000_000 or result.get("encoding") != "base64":
            raise Abstain("Source blob unsupported or too large")
        raw = base64.b64decode(result["content"])
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if actual != oid:
            raise Abstain("Source blob hash mismatch")
        return raw.decode("utf-8")


OPEN_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review")


def identity(event):
    pr = event.get("pull_request", {})
    if event.get("action") == "closed" and pr.get("state") == "closed":
        if pr.get("merged") is not True or not pr.get("merge_commit_sha"):
            raise Abstain("Only merged pull requests are supported; closed without merging")
        merged = True
    elif event.get("action") in OPEN_ACTIONS and pr.get("state") == "open":
        merged = False
    else:
        raise Abstain("Only open or merged pull requests are supported")
    repo = repository(event["repository"]["full_name"])
    if pr["base"]["repo"]["full_name"] != repo:
        raise Abstain("Event repository does not match PR base")
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
    # Commit objects of merged/fork PRs are also reachable via the base repo API.
    return {"repository": repo, "number": int(pr["number"]),
            "base_sha": sha(pr["base"]["sha"]), "head_sha": sha(pr["head"]["sha"]),
            "source_sha": sha(pr["head"]["sha"]), "source_repository": repo,
            "head_repository": head_repo, "merged": merged,
            "merge_sha": sha(pr["merge_commit_sha"]) if merged else None,
            "source_kind": "pull-request-head", "closed_at": pr.get("closed_at")}


def unchanged(latest, ident):
    state = ("closed", True) if ident["merged"] else ("open", False)
    return (latest["head"]["sha"], latest["base"]["sha"], latest["state"],
            bool(latest.get("merged"))) == (ident["head_sha"], ident["base_sha"], *state)


def added_lines(patch):
    lines, current = [], None
    for line in (patch or "").splitlines():
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            current = int(match[1])
        elif current is not None:
            if line.startswith("+"):
                lines.append(current)
                current += 1
            elif line.startswith(" "):
                current += 1
    return lines


def change_blocks(patch):
    """Pair each contiguous run of removed/added lines as (old_lines, new_lines)."""
    blocks, old, new, removed, added = [], None, None, [], []
    def flush():
        if removed or added:
            blocks.append((list(removed), list(added)))
        removed.clear()
        added.clear()
    for line in (patch or "").splitlines():
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            flush()
            old, new = int(match[1]), int(match[2])
        elif old is not None:
            if line.startswith("-"):
                removed.append(old)
                old += 1
            elif line.startswith("+"):
                added.append(new)
                new += 1
            elif line.startswith(" "):
                flush()
                old += 1
                new += 1
    flush()
    return blocks


def removed_lines(patch):
    return [n for old, _ in change_blocks(patch) for n in old]


def reanchor(properties, base_bundle):
    """Move head-side claim lines to the removed lines of the same change blocks."""
    mapping = base_bundle.get("head_to_base")
    if base_bundle["identity"].get("source_kind") != "pull-request-base" or mapping is None:
        raise Abstain("Re-anchoring requires a base-side bundle")
    moved, dropped = [], []
    for item in properties:
        target = mapping.get(item.get("path"), {})
        lines = sorted({n for x in item.get("lines", []) for n in target.get("lines", {}).get(str(x), [])})
        if lines:
            moved.append(dict(item, path=target["path"], lines=lines,
                              head_path=item.get("path"), head_lines=item.get("lines")))
        else:
            dropped.append(dict(item, reason="claim lines are pure additions with no base counterpart"))
    return {"properties": moved, "dropped": dropped}


def collect(event, api, side="head"):
    if side not in ("head", "base"):
        raise Abstain("Collection side must be head or base")
    # Base snapshots reuse the head snapshot's context order so before/after runs see the
    # same surrounding files and differ only in the changed code.
    preferred = {p: i for i, p in enumerate(collect(event, api)["sources"])} if side == "base" else {}
    ident = identity(event)
    if side == "base":
        # Pre-merge snapshot of the same PR, anchored on removed lines; never publishable.
        ident = dict(ident, source_sha=ident["base_sha"], source_kind="pull-request-base")
    repo, number = ident["repository"], ident["number"]
    prefix = f"repos/{repo}"
    latest = api.request(f"{prefix}/pulls/{number}")
    if not unchanged(latest, ident):
        raise Abstain("PR identity changed since event; refusing mutable snapshot")
    comparison = api.request(
        f"{prefix}/compare/{ident['base_sha']}...{ident['head_sha']}")
    files = comparison.get("files", [])
    if len(files) != latest["changed_files"] or len(files) > 300:
        raise Abstain("Changed-file inventory incomplete or exceeds 300-file budget")
    signals, signal_bytes = [], 0
    # Intent comes from the PR description, conversation (issue) comments, and
    # commit messages; review comments and reviews are not collected.
    description = {"id": number, "body": latest.get("body"), "html_url": latest.get("html_url", "")}
    for kind, items in (("description", [description]),
                        ("comment", api.pages(f"{prefix}/issues/{number}/comments")),
                        ("commit", api.pages(f"{prefix}/pulls/{number}/commits"))):
        for item in items:
            body = item.get("body") or (item.get("commit") or {}).get("message", "")
            if (kind == "comment" and (item.get("user") or {}).get("login") == "github-actions[bot]"
                    and any(marker in body for marker in (
                        "<!-- gh-aw-workflow-id: nfv-pr-review -->",
                        "<!-- gh-aw-agentic-workflow: NFV PR reviewer,"))):
                continue
            if body.strip():
                signal_bytes += len(body[:12000].encode("utf-8"))
                if len(signals) >= 500 or signal_bytes > 1_000_000:
                    raise Abstain("Commentary exceeds 500-signal/1 MB collection budget")
                signals.append({"id": f"{kind}:{item.get('id', item.get('sha'))}",
                                "kind": kind, "text": body[:12000],
                                "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                                "truncated": len(body) > 12000,
                                "url": item.get("html_url", ""),
                                "path": item.get("path"), "line": item.get("line")})
    tree = api.request(f"{prefix}/git/trees/{ident['source_sha']}?recursive=1")
    if tree.get("truncated"):
        raise Abstain("Source tree truncated")
    blobs = {x["path"]: x for x in tree["tree"]
             if x["type"] == "blob" and x.get("mode") in ("100644", "100755")}
    sources, changed, omissions, head_to_base = {}, {}, [], {}
    captured_bytes = 0
    for file in files:
        path = source_path(file["filename"])
        if side == "base":
            if file["status"] == "added":
                omissions.append(f"{path}: added; no base implementation")
                continue
            path = source_path(file.get("previous_filename") or file["filename"])
        elif file["status"] == "removed":
            omissions.append(f"{path}: deleted; no head implementation")
            continue
        if path not in blobs:
            omissions.append(f"{path}: non-regular or missing source")
            continue
        try:
            text = api.blob(repo, blobs[path]["sha"])
        except (Abstain, UnicodeError) as exc:
            omissions.append(f"{path}: {exc}")
            continue
        lines = (removed_lines if side == "base" else added_lines)(file.get("patch"))
        if not lines:
            omissions.append(f"{path}: no available {'removed' if side == 'base' else 'added'}-line anchors")
            continue
        if side == "base":
            head_to_base[source_path(file["filename"])] = {"path": path, "lines": {
                str(n): old for old, new in change_blocks(file.get("patch")) if old for n in new}}
        if captured_bytes + len(text.encode("utf-8")) > 6_000_000:
            omissions.append(f"{path}: source context byte budget exhausted")
            continue
        sources[path] = {"text": text, "blob_sha": blobs[path]["sha"]}
        captured_bytes += len(text.encode("utf-8"))
        changed[path] = lines
    # Resolve quoted includes and same-basename headers first. Remaining nearby
    # declarations are a bounded lexical aid, not a claim of whole-program analysis.
    related = set()
    def dependencies(path, text):
        found = set()
        for include in re.findall(r'#\s*include\s*["<]([^">]+)[">]', text):
            found.update(name for name in blobs if name == include or name.endswith("/" + include))
        for header in list(found) + ([path] if path.endswith(".h") else []):
            if header.endswith(".h"):
                stem = header[:-2]
                found.update(stem + extension for extension in (".c", ".cc", ".cpp", ".cxx")
                             if stem + extension in blobs)
        directory = path.rpartition("/")[0]
        suffix = path.rsplit(".", 1)[-1].lower()
        language_neighbors = {
            "java": (".java",), "cs": (".cs",), "go": (".go",), "py": (".py",), "rs": (".rs",),
            "js": (".js", ".mjs", ".ts"), "jsx": (".js", ".jsx", ".ts", ".tsx"),
            "ts": (".ts", ".tsx", ".js"), "tsx": (".ts", ".tsx", ".js", ".jsx"),
        }
        extensions = language_neighbors.get(suffix, ())
        if extensions:
            found.update(name for name in blobs if name.rpartition("/")[0] == directory
                         and name.endswith(extensions))
        for module in re.findall(r'(?:from\s+|require\s*\(\s*|import\s*)["\'](\.[^"\']+)["\']', text):
            stem = posixpath.normpath(posixpath.join(directory, module))
            found.update(name for name in blobs if name == stem or any(
                name in (stem + ext, stem + "/index" + ext) for ext in extensions))
        if suffix == "rs":
            # `mod m;` lives beside lib.rs/main.rs/mod.rs, otherwise under foo/;
            # `use crate::a::b` maps to a.rs, a/b.rs or a/b/mod.rs.
            filename = path.rpartition("/")[2]
            base = directory if filename in ("lib.rs", "main.rs", "mod.rs") else path[:-3]
            for module in re.findall(r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", text):
                found.update(name for name in blobs if name in (
                    posixpath.join(base, module + ".rs"), posixpath.join(base, module, "mod.rs")))
            for use in re.findall(r"\buse\s+crate::([\w:]+)", text):
                parts = [part for part in use.split("::") if part]
                for k in range(len(parts), 0, -1):
                    stem = "/".join(parts[:k])
                    found.update(name for name in blobs
                                 if name.endswith(("/" + stem + ".rs", "/" + stem + "/mod.rs")))
        for module in re.findall(r"(?m)^\s*(?:from|import)\s+([\w.]+)", text):
            stem = module.replace(".", "/").strip("/")
            found.update(name for name in blobs if any(
                name == stem + ext or name.endswith("/" + stem + ext)
                or name.endswith("/" + stem + "/__init__.py")
                for ext in extensions))
        return found

    # Fetch nearest context first: files named after identifiers on changed lines,
    # then direct includes/neighbours, then their dependencies (name breaks ties).
    depth = {}
    def relate(paths, level):
        for p in paths:
            # Implementations rank one step behind their declaring headers.
            lvl = level + 1 if level and p.endswith((".c", ".cc", ".cpp", ".cxx")) else level
            if p not in depth or lvl < depth[p]:
                depth[p] = lvl
            related.add(p)
    words = set()
    for path, record in sources.items():
        text_lines = record["text"].splitlines()
        for n in changed.get(path, []):
            if 0 < n <= len(text_lines):
                words.update(w for w in re.findall(r"[A-Za-z_]\w{3,}", text_lines[n - 1]))
    words -= {"return", "false", "true", "void", "const", "auto", "bool", "char", "else",
              "while", "break", "self", "this", "null", "None", "static", "struct", "class",
              "unsigned", "signed", "public", "private", "inline", "override", "virtual",
              "impl", "match", "Self", "Some", "Option", "Result", "usize", "isize"}
    families = [(".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"), (".java",), (".cs",), (".go",),
                (".py",), (".js", ".jsx", ".mjs", ".ts", ".tsx"), (".rs",)]
    wanted = tuple(ext for family in families for ext in family
                   if any(path.endswith(family) for path in changed))
    relate((p for p in blobs if p.endswith(wanted or ("\0",))
            and p.rsplit("/", 1)[-1].rsplit(".", 1)[0] in words), 0)
    for path, record in list(sources.items()):
        directory = path.rpartition("/")[0]
        relate(dependencies(path, record["text"]), 1)
        stem = path.rsplit(".", 1)[0]
        relate((p for p in blobs if p == stem + ".h"), 1)
        relate((p for p in blobs if p.rpartition("/")[0] == directory and p.endswith(".h")), 3)
    anchors = [p.split("/")[:-1] for p in changed]
    def proximity(p):
        parts = p.split("/")[:-1]
        return max((len(posixpath.commonprefix([parts, a])) for a in anchors), default=0)
    related.update(p for p in preferred if p in blobs)
    visited = set(sources)
    for _ in range(80):
        pending = sorted(related - visited, key=lambda p: (
            preferred.get(p, len(preferred)), depth.get(p, 99), -proximity(p), p))
        if not pending:
            break
        path = pending[0]
        visited.add(path)
        try:
            text = api.blob(repo, blobs[path]["sha"])
            if captured_bytes + len(text.encode("utf-8")) > 6_000_000:
                omissions.append(f"{path}: source context byte budget exhausted")
                continue
            sources[path] = {"text": text,
                             "blob_sha": blobs[path]["sha"]}
            captured_bytes += len(text.encode("utf-8"))
            relate(dependencies(path, text), depth.get(path, 98) + 1)
        except (Abstain, UnicodeError):
            omissions.append(f"{path}: context unavailable")
    if len(related - sources.keys()) > 0:
        omissions.append("Some related files omitted by context budget")
    declarations = []
    calls = set()
    for path, record in sources.items():
        for line, text in enumerate(record["text"].splitlines(), 1):
            if re.search(r"\b(requires|assert|SASSERT|VERIFY|class|struct|typedef|def|func|function|interface|namespace|public|private)\b", text):
                declarations.append({"path": path, "line": line, "text": text[:500]})
            calls.update(re.findall(r"\b([A-Za-z_]\w*)\s*\(", text))
    latest = api.request(f"{prefix}/pulls/{number}")
    if not unchanged(latest, ident):
        raise Abstain("PR changed during collection")
    bundle = {"schema": 1, "identity": ident, "signals": signals, "changed": changed,
              "sources": sources, "declarations_and_preconditions": declarations[:500],
              "callee_names": sorted(calls)[:500], "context_omissions": omissions,
              "source_inventory": sorted(blobs), "source_inventory_complete": True}
    if side == "base":
        bundle["head_to_base"] = head_to_base
    return bundle
