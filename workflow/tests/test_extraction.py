# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nfv_workflow.cli import inference_chat
from nfv_workflow.extraction import extract
from nfv_workflow.github import Abstain
from nfv_workflow.outcomes import ResourcesExhausted
from nfv_workflow.source_context import REQUEST_BYTES, function_catalog
from test_workflow import ScratchTest, bundle


class Chat:
    def __init__(self, answers):
        self.answers, self.requests = iter(answers), []

    async def ask(self, system, prompt, timeout):
        self.requests.append((system, json.loads(prompt), timeout))
        return SimpleNamespace(text=json.dumps(next(self.answers)))


class ExtractionTests(ScratchTest):
    def setUp(self):
        super().setUp()
        self.bundle = bundle()
        self.function = function_catalog(self.bundle)["functions"][0]["id"]
        self.property = {"function_id": self.function, "origin": "source",
                         "signal_id": "source:src/f.cpp", "quote": "return x + 1;",
                         "property": "The result increases the input by one.", "language": "cpp"}

    def test_shortlist_then_full_scoped_source_with_cross_file_context(self):
        self.bundle["sources"]["src/helper.h"] = {"text": "int helper(int x);\n"}
        self.bundle["sources"]["src/f.cpp"]["text"] = "int f(int x) {\n  return helper(x);\n}\n"
        self.bundle["repository_context"] = [{"path": "src/helper.h", "symbols": ["helper"]}]
        function = function_catalog(self.bundle)["functions"][0]["id"]
        prop = {**self.property, "function_id": function, "quote": "return helper(x);"}
        before = copy.deepcopy(self.bundle)
        chat = Chat([[function], [prop]])
        result = asyncio.run(extract(self.bundle, chat, output=self.directory))
        self.assertEqual(result, [prop])
        self.assertEqual(self.bundle, before)
        self.assertEqual(len(chat.requests), 2)
        shortlist, inspection = [item[1] for item in chat.requests]
        self.assertNotIn("sources", shortlist)
        self.assertEqual(shortlist["function_catalog"][0]["declaration_preview"], "int f(int x) {")
        self.assertIn("return helper(x);", inspection["context"]["sources"]["src/f.cpp"]["numbered_lines"])
        self.assertIn("int helper(int x);", inspection["context"]["sources"]["src/helper.h"]["numbered_lines"])
        self.assertEqual(inspection["function_catalog"]["functions"][0]["id"], function)
        saved = json.loads((self.directory / "extraction.json").read_text())
        self.assertIs(saved["completed"], True)
        self.assertEqual(saved["inspected_functions"], [function])
        self.assertEqual(saved["proposals"], result)
        for system, data, _ in chat.requests:
            self.assertLessEqual(len(system.encode()) + len(json.dumps(data, ensure_ascii=False).encode()),
                                 REQUEST_BYTES)

    def test_invalid_shortlist_cannot_become_successful_empty_extraction(self):
        for selected in ([], ["unknown"], [self.function] * 2, [{"id": self.function}]):
            with self.subTest(selected=selected):
                with self.assertRaisesRegex(Abstain, "Function selection"):
                    asyncio.run(extract(self.bundle, Chat([selected]), output=self.directory))
                saved = json.loads((self.directory / "extraction.json").read_text())
                self.assertIs(saved["completed"], False)

    def test_empty_results_require_completed_source_inspection(self):
        chat = Chat([[self.function], []])
        self.assertEqual(asyncio.run(extract(self.bundle, chat, output=self.directory)), [])
        saved = json.loads((self.directory / "extraction.json").read_text())
        self.assertIs(saved["completed"], True)
        self.assertEqual(saved["inspected_functions"], [self.function])
        self.assertEqual(len(chat.requests), 2)

    def test_invalid_source_does_not_masquerade_as_no_properties(self):
        self.bundle["sources"]["src/f.cpp"]["text"] = "int f(int x) {\n"
        chat = Chat([])
        with self.assertRaisesRegex(Abstain, "No inspectable"):
            asyncio.run(extract(self.bundle, chat, output=self.directory))
        self.assertEqual(chat.requests, [])
        self.assertIs(json.loads((self.directory / "extraction.json").read_text())["completed"], False)

    def test_request_limit_is_enforced_before_model_call(self):
        self.bundle["signals"][0]["text"] = "x" * REQUEST_BYTES
        chat = Chat([])
        with self.assertRaises(ResourcesExhausted):
            asyncio.run(extract(self.bundle, chat))
        self.assertEqual(chat.requests, [])

    def test_shared_property_budget_balances_selected_functions(self):
        self.bundle["sources"]["src/f.cpp"]["text"] += "int g(int x) {\n  return x - 1;\n}\n"
        self.bundle["changed"]["src/f.cpp"] += [5]
        functions = [item["id"] for item in function_catalog(self.bundle)["functions"]]
        chat = Chat([functions, [self.property] * 5, []])
        proposals = asyncio.run(extract(self.bundle, chat))
        self.assertEqual(len(proposals), 5)
        self.assertEqual([item[1]["max_properties"] for item in chat.requests[1:]], [5, 5])

    def test_over_budget_response_is_an_explicit_extraction_failure(self):
        chat = Chat([[self.function], [self.property] * 11])
        with self.assertRaisesRegex(Abstain, "max_properties"):
            asyncio.run(extract(self.bundle, chat, output=self.directory))
        self.assertIs(json.loads((self.directory / "extraction.json").read_text())["completed"], False)


class InferenceCredentialTests(unittest.TestCase):
    def test_foreground_requires_dedicated_token_and_never_reuses_repository_token(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_AW": "false",
                                     "GH_TOKEN": "repository-token", "GITHUB_TOKEN": "repository-token"},
                        clear=True):
            with self.assertRaisesRegex(Abstain, "dedicated COPILOT_GITHUB_TOKEN"):
                inference_chat("test-model", "extraction")
            os.environ["COPILOT_GITHUB_TOKEN"] = "dedicated-token"
            with patch("nfv_workflow.chat.WorkflowChat") as chat:
                inference_chat("test-model", "extraction")
            chat.assert_called_once_with(model="test-model", concurrency=1, provider=None)
            self.assertNotIn("GH_TOKEN", os.environ)
            self.assertNotIn("GITHUB_TOKEN", os.environ)
            self.assertEqual(os.environ["COPILOT_GITHUB_TOKEN"], "dedicated-token")
