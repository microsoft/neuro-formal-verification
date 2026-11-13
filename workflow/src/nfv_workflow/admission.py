# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Reserve one automatic PR audit before either inference session starts."""

import json
import os
from pathlib import Path
import re
import sys

from .github import Abstain, GitHub, identity
from .publish import publication_allowed


def admit(event, api, run_id, run_attempt=1):
    ident = identity(event)
    pr = event["pull_request"]
    if (event["action"] not in ("opened", "ready_for_review")
            or pr.get("draft") is not False
            or pr["base"]["ref"] != event["repository"]["default_branch"]):
        return False
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)) or run_attempt < 1:
        raise Abstain("Invalid Actions run identity")
    repo, number = ident["repository"], ident["number"]
    prefix = f"<!-- nfv-pr-attempt:{repo}:{number}:"
    previous = []
    for comment in api.pages(f"repos/{repo}/issues/{number}/comments"):
        login = (comment.get("user") or {}).get("login")
        body = comment.get("body") or ""
        if (login == "github-actions[bot]" and "<!-- nfv-merged-pr:" in body
                and any(tag in body for tag in (
                    "<!-- gh-aw-workflow-id: nfv-pr-review -->",
                    "<!-- gh-aw-agentic-workflow: NFV PR reviewer,"))):
            return False
        match = re.match(re.escape(prefix) + r"([1-9][0-9]*) -->", body)
        if match:
            if login != "github-actions[bot]":
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]+", login):
                    raise Abstain("Invalid reservation author")
                permission = api.request(f"repos/{repo}/collaborators/{login}/permission")
                if permission.get("permission") not in ("admin", "maintain", "write"):
                    continue
            previous.append(match[1])
    if previous:
        # Rerunning the same failed workflow is an explicit retry, not a new event.
        return run_attempt > 1 and set(previous) == {str(run_id)}
    body = (
        f"{prefix}{run_id} -->\n"
        "<details>\n<summary>NFV audit record</summary>\n\n"
        f"[Run and diagnostics](https://github.com/{repo}/actions/runs/{run_id}).\n"
        f"Saved head `{ident['head_sha']}`; base `{ident['base_sha']}`.\n\n"
        "Keep this record to prevent duplicate automatic audits, including after failures. "
        "To retry a failed attempt, a maintainer can rerun the linked workflow.\n\n"
        "</details>"
    )
    result = api.request(f"repos/{repo}/issues/{number}/comments", {"body": body})
    if type(result.get("id")) is not int:
        raise Abstain("Could not confirm the PR audit reservation")
    return True


def main():
    try:
        if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
            raise Abstain("PR admission requires a pull_request event")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        api = GitHub()
        admitted = publication_allowed(api) and admit(
            event, api, os.environ["GITHUB_RUN_ID"], int(os.environ["GITHUB_RUN_ATTEMPT"]))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"admitted={str(admitted).lower()}\n")
        return 0
    except (Abstain, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"NFV admission failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
