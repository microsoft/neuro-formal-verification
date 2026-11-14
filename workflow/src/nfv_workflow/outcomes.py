# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""The four public outcomes of a workflow property analysis."""

from .github import Abstain

REFUTED = "refuted"
VERIFIED = "verified"
EXHAUSTED = "resources exhausted"
INVALID = "type-check/audit failure"
OUTCOMES = (REFUTED, VERIFIED, EXHAUSTED, INVALID)


class ResourcesExhausted(Abstain):
    pass


class InferenceFailure(RuntimeError):
    """Provider or SDK failure, not a property-analysis outcome."""
