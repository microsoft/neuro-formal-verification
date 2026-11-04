# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bounded, tool-free property extraction from immutable source."""

from __future__ import annotations

import json
from pathlib import Path

from . import read_prompt
from .general import json_answer, save
from .github import Abstain
from .source_context import check_request, function_catalog, scoped_context


async def extract(bundle, chat, timeout=240, output=None):
    catalog = function_catalog(bundle)
    record = {"completed": False, "function_catalog": catalog, "selected_functions": [],
              "inspected_functions": [], "proposals": []}

    def persist():
        if output is not None:
            save(Path(output) / "extraction.json", record)

    persist()
    if not catalog["functions"]:
        if catalog["errors"]:
            raise Abstain("No inspectable changed functions: " + json.dumps(catalog["errors"]))
        record["completed"] = True
        persist()
        return []
    functions = {item["id"]: item for item in catalog["functions"]}
    previews = []
    for item in catalog["functions"]:
        source = bundle["sources"][item["path"]]
        line = source["text"].splitlines()[item["declaration"] - source.get("line_start", 1)]
        previews.append({**item, "declaration_preview": line[:400],
                         "preview_truncated": len(line) > 400})
    system = read_prompt("function_prompt.txt")
    prompt = json.dumps({"signals": bundle["signals"], "function_catalog": previews},
                        ensure_ascii=False)
    check_request(system, prompt, "Function selection")
    selected = json_answer((await chat.ask(system, prompt, timeout=timeout)).text)
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 10
            or any(not isinstance(item, str) or item not in functions for item in selected)
            or len(set(selected)) != len(selected)):
        raise Abstain("Function selection must contain 1..10 distinct captured function IDs")
    record["selected_functions"] = selected
    record["selection_omissions"] = [key for key in functions if key not in selected]
    persist()
    system = read_prompt("intent_prompt.txt")
    for index, key in enumerate(selected):
        function = functions[key]
        context = scoped_context(bundle, {**function, "lines": function["lines"]})
        remaining = 10 - len(record["proposals"])
        maximum = max(1, remaining // (len(selected) - index))
        prompt = json.dumps({"signals": bundle["signals"], "function_catalog": {"functions": [function]},
                             "context": context, "max_properties": maximum}, ensure_ascii=False)
        check_request(system, prompt, "Property extraction")
        proposals = json_answer((await chat.ask(system, prompt, timeout=timeout)).text)
        if not isinstance(proposals, list) or len(proposals) > maximum:
            raise Abstain("Property extraction must return an array within max_properties")
        record["proposals"].extend(proposals)
        record["inspected_functions"].append(key)
        persist()
    record["completed"] = True
    persist()
    return record["proposals"]
