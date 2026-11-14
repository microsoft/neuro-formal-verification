# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import importlib.resources
import unittest
from unittest.mock import patch

from nfv_workflow import read_prompt


class PromptTests(unittest.TestCase):
    def test_file_legal_notices_are_not_sent_to_models(self):
        prefix = "# Copyright (c) Microsoft Corporation.\n# Licensed under the MIT License.\n\n"
        resources = importlib.resources.files("nfv_workflow")
        prompts = [path for path in resources.iterdir() if path.name.endswith("_prompt.txt")]
        self.assertEqual(len(prompts), 6)
        for path in prompts:
            with self.subTest(prompt=path.name):
                raw = path.read_text(encoding="utf-8")
                self.assertTrue(raw.startswith(prefix))
                self.assertEqual(read_prompt(path.name), raw[len(prefix):])

    def test_prompt_loading_is_utf8_and_preserves_unwrapped_content(self):
        with patch("nfv_workflow.importlib.resources.files") as files:
            resource = files.return_value.joinpath.return_value
            resource.read_text.return_value = "intent \N{GREEK SMALL LETTER PI}\n"
            self.assertEqual(read_prompt("sample.txt"), "intent \N{GREEK SMALL LETTER PI}\n")
            resource.read_text.assert_called_once_with(encoding="utf-8")
