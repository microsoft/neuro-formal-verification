# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Validate repository-context hints and capture their immutable source blobs."""

import hashlib
import json
import re
from pathlib import Path

from .github import Abstain, repository, sha, source_path
from .outcomes import ResourcesExhausted
from .source_context import CPP_OPERATOR_SYMBOL, unqualified_name

MAX_FILES = 16
MAX_BYTES = 2_000_000
SUFFIXES = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".java", ".cs", ".go",
            ".js", ".jsx", ".mjs", ".ts", ".tsx", ".py", ".rs"}


def validate_manifest(manifest, bundle):
    if not isinstance(manifest, dict) or manifest.get("identity") != bundle["identity"]:
        raise Abstain("Repository context refers to a different PR snapshot")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise Abstain("Repository context exceeds its file budget")
    inventory = set(bundle["source_inventory"])
    selected, seen = [], set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "symbols"}:
            raise Abstain("Repository context contains only source paths and symbol names")
        path = source_path(item["path"])
        symbols = item["symbols"]
        if path not in inventory or Path(path).suffix.lower() not in SUFFIXES or path in seen:
            raise Abstain("Repository context must name distinct regular source files in the snapshot")
        if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 8
                or any(not isinstance(name, str) or len(name) > 128 or not re.fullmatch(
                    r"(?:[A-Za-z_]\w*::)*(?:[A-Za-z_]\w*|operator" + CPP_OPERATOR_SYMBOL + ")", name)
                       for name in symbols)):
            raise Abstain("Repository context requires bounded source identifier names")
        selected.append({"path": path, "symbols": sorted({
            unqualified_name(name) for name in symbols})})
        seen.add(path)
    return {"identity": bundle["identity"], "files": selected}


def context_input(bundle):
    return {**{key: bundle[key] for key in ("identity", "changed")},
            "source_suffixes": sorted(SUFFIXES),
            "source_inventory": [path for path in bundle["source_inventory"]
                                 if Path(path).suffix.lower() in SUFFIXES]}


def sanitize_manifest(bundle, input_path, output_path, *, hints_only=False):
    path = Path(input_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 25000:
        raise Abstain("Context session must produce a bounded regular JSON manifest")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if hints_only:
        if not isinstance(manifest, dict) or set(manifest) != {"files"}:
            raise Abstain("Context hints must contain only the files list; trusted code binds the identity")
        manifest = {"identity": bundle["identity"], "files": manifest["files"]}
    manifest = validate_manifest(manifest, bundle)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def enrich(bundle, manifest, api):
    manifest = validate_manifest(manifest, bundle)
    ident = bundle["identity"]
    repo = repository(ident.get("source_repository") or ident["repository"])
    tree = api.request(f"repos/{repo}/git/trees/{sha(ident['source_sha'])}?recursive=1")
    if tree.get("truncated"):
        raise Abstain("Repository context source tree is incomplete")
    blobs = {item["path"]: item for item in tree["tree"]
             if item["type"] == "blob" and item.get("mode") in ("100644", "100755")}
    sources = dict(bundle["sources"])
    total = 0
    for item in manifest["files"]:
        path = item["path"]
        blob = blobs.get(path)
        if blob is None:
            raise Abstain("Context hint does not identify a regular immutable source blob")
        if path in sources and sources[path].get("line_start", 1) == 1:
            raw = sources[path]["text"].encode("utf-8")
            actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if actual == blob["sha"]:
                continue
        if blob.get("size", 0) > MAX_BYTES - total:
            raise ResourcesExhausted("Additional repository source exceeds its byte budget")
        text = api.blob(repo, blob["sha"])
        total += len(text.encode("utf-8"))
        if total > MAX_BYTES:
            raise ResourcesExhausted("Additional repository source exceeds its byte budget")
        sources[path] = {"text": text, "blob_sha": blob["sha"]}
    result = {**bundle, "sources": sources, "repository_context": manifest["files"]}
    if len(json.dumps(result).encode("utf-8")) > 12_000_000:
        raise ResourcesExhausted("Enriched bundle exceeds its input byte budget")
    return result
