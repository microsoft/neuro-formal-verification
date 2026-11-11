# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Allowlisted, versioned dashboard metadata; never source or proof evidence."""

import re

from .outcomes import OUTCOMES


def review_summary(status, environment):
    identity = status.get("identity", {})
    repository = identity.get("repository", environment.get("GITHUB_REPOSITORY", ""))
    fields = {
        "repository_id": environment.get("NFV_SUMMARY_REPOSITORY_ID", ""),
        "run_id": environment.get("NFV_SUMMARY_RUN_ID", ""),
        "run_attempt": environment.get("NFV_SUMMARY_RUN_ATTEMPT", ""),
        "number": identity.get("number", environment.get("NFV_SUMMARY_PR_NUMBER", "")),
    }
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        return None
    if any(not re.fullmatch(r"[1-9][0-9]*", str(value)) for value in fields.values()):
        return None
    hashes = {
        "head_sha": identity.get("head_sha", environment.get("NFV_SUMMARY_HEAD_SHA", "")),
        "base_sha": identity.get("base_sha", environment.get("NFV_SUMMARY_BASE_SHA", "")),
        "release_sha": environment.get("NFV_SUMMARY_RELEASE_SHA", ""),
    }
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
           for value in hashes.values()):
        return None
    counts = None
    if status.get("analysis_completed") is True:
        counts = status.get("outcomes")
        if (not isinstance(counts, dict) or set(counts) != set(OUTCOMES)
                or any(type(value) is not int or value < 0 for value in counts.values())
                or sum(counts.values()) > 10):
            return None
        counts = {name: counts[name] for name in OUTCOMES}
    if status.get("status") not in ("completed", "infrastructure_failure", "superseded"):
        return None
    if status.get("publication") not in ("submitted", "unconfirmed", "not_attempted", "not_needed"):
        return None
    return {
        "schema_version": 1, "kind": "nfv-pr-audit-summary",
        "repository": repository, **{key: int(value) for key, value in fields.items()}, **hashes,
        "status": status["status"], "completed": status.get("completed") is True,
        "analysis_completed": status.get("analysis_completed") is True,
        "publication": status["publication"], "counts": counts,
    }
