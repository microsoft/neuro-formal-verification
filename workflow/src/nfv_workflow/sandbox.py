"""Deterministic NFV foreground driver for gh-aw's sandboxed SDK engine."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .cli import audit, audit_limits, awf_provider, load, propose, save
from .github import Abstain
from .outcomes import InferenceFailure


def review():
    env = os.environ
    if env.get("GITHUB_AW") != "true" or env.get("AWF_REFLECT_ENABLED") != "1":
        raise Abstain("NFV sandbox driver requires the gh-aw AWF runtime")
    if any(env.get(key) for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
        raise Abstain("Do not expose GitHub credentials to the NFV sandbox driver")
    if not env.get("COPILOT_PROVIDER_BASE_URL") or not env.get("COPILOT_PROVIDER_TYPE"):
        raise Abstain("gh-aw must resolve the SDK provider through AWF reflection")
    awf_provider()
    limits = audit_limits()
    from .followups import Options
    Options.from_env()
    if not env.get("COPILOT_SDK_URI") or not env.get("COPILOT_CONNECTION_TOKEN"):
        raise Abstain("The gh-aw SDK sidecar and per-run connection token are required")
    if not all(env.get(key) for key in ("NFV_BUNDLE", "NFV_MODEL", "NFV_OUT")):
        raise Abstain("Trusted collection must configure NFV inputs, model and output")
    output = Path(env["NFV_OUT"])
    output.mkdir(parents=True, exist_ok=True)
    # A harness retry must not restart an audit or submit a second write intent.
    with (output / "sandbox-run.json").open("x", encoding="utf-8") as stream:
        json.dump({"execution": "sandboxed-foreground"}, stream)
    bundle = load(env["NFV_BUNDLE"])
    save(output / "bundle.json", bundle)
    proposals = asyncio.run(propose(bundle, env["NFV_MODEL"], intent_only=True, output=output))
    if load(output / "extraction.json").get("completed") is not True:
        raise Abstain("Property extraction is incomplete")
    proposals_path = output / "proposals.json"
    save(proposals_path, proposals)
    summary = audit(proposals_path, limits)
    if summary["errors"] or load(output / "report.json").get("completed") is not True:
        raise Abstain("NFV audit did not produce a complete error-free report")
    body = (output / "comment.md").read_text(encoding="utf-8")
    if not body.strip():
        raise Abstain("NFV audit produced an empty report")
    result = subprocess.run(
        ["safeoutputs", "add_comment", "."],
        input=json.dumps({"body": body}, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    save(output / "publication.json", {"returncode": result.returncode,
                                     "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode:
        raise Abstain("Safe-output submission failed: " + result.stderr[-2000:])
    return {**summary, "publication": "submitted"}


def main():
    try:
        print(json.dumps(review()), flush=True)
    except (Abstain, InferenceFailure, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"NFV sandbox audit failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
