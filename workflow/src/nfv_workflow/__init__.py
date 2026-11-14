# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Refutation-only GitHub Agentic Workflow integration."""

import importlib.resources

__version__ = "0.1.0"

_LICENSE_HEADER = (
    "# Copyright (c) Microsoft Corporation.\n"
    "# Licensed under the MIT License.\n\n"
)


def read_prompt(name: str) -> str:
    """Load UTF-8 prompt content without its file-level legal notice."""
    text = importlib.resources.files(__name__).joinpath(name).read_text(encoding="utf-8")
    return text.removeprefix(_LICENSE_HEADER)
