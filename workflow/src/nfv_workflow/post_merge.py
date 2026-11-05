# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Trusted post-merge dispatch and publication metadata; never execute PR code."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from .github import Abstain, GitHub, identity, repository, sha
from .publish import LABEL, marker
from .repository_context import SUFFIXES


WORKFLOW = "nfv-pr-auditor.lock.yml"


def tracked_issues(api, repo):
    return api.pages(f"repos/{repo}/issues?state=all&labels={quote(LABEL)}"
                     "&creator=github-actions%5Bbot%5D")


def has_source_changes(api, captured):
    pr, repo = captured["pull_request"], captured["repository"]["full_name"]
    files = api.pages(f"repos/{repo}/pulls/{pr['number']}/files")
    if len(files) != pr["changed_files"]:
        raise Abstain("Merged PR file inventory is incomplete")
    return any(Path(item["filename"]).suffix.lower() in SUFFIXES for item in files)


def eligibility(event, api, ref):
    captured = review_event(event, api, ref)
    ident = identity(captured)
    if any(marker(ident) in (issue.get("body") or "")
           for issue in tracked_issues(api, ident["repository"])):
        return {"review": False, "reason": "refutation already tracked"}
    if not has_source_changes(api, captured):
        return {"review": False, "reason": "no supported source changes"}
    return {"review": True}


def default_branch(event, api, ref):
    repo = repository(event["repository"]["full_name"])
    branch = api.request(f"repos/{repo}")["default_branch"]
    if ref != f"refs/heads/{branch}":
        raise Abstain("Post-merge NFV must execute on the trusted default branch")
    return repo, branch


def review_event(event, api, ref):
    repo, branch = default_branch(event, api, ref)
    inputs = event.get("inputs", {})
    number = str(inputs.get("pull-request-number", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,9}", number):
        raise Abstain("A positive merged pull-request number is required")
    merge = sha(inputs.get("merge-sha"))
    pr = api.request(f"repos/{repo}/pulls/{number}")
    if (pr.get("merged") is not True or pr.get("state") != "closed"
            or pr["base"]["repo"]["full_name"] != repo or pr["base"]["ref"] != branch
            or pr.get("merge_commit_sha") != merge):
        raise Abstain("The requested PR does not match a default-branch merge")
    head = sha(api.request(f"repos/{repo}/commits/{quote(branch, safe='')}")["sha"])
    relation = api.request(f"repos/{repo}/compare/{merge}...{head}")
    if relation["status"] not in ("ahead", "identical"):
        raise Abstain("The PR merge is not retained on the default branch")
    result = {"action": "closed", "repository": event["repository"], "pull_request": pr}
    identity(result)
    return result


def dispatch(event, api):
    repo, branch = default_branch(event, api, event.get("ref"))
    if event.get("deleted") or event.get("forced"):
        raise Abstain("Post-merge dispatch requires a non-forced default-branch push")
    before, after = sha(event["before"]), sha(event["after"])
    if before == "0" * 40:
        return {"dispatched": [], "skipped": [], "reason": "default branch creation"}
    comparison = api.request(f"repos/{repo}/compare/{before}...{after}")
    commits = comparison["commits"]
    if (comparison["status"] != "ahead" or len(commits) != comparison["total_commits"]
            or len(commits) > 250):
        raise Abstain("Pushed commit inventory is incomplete or exceeds 250 commits")
    candidates = {}
    for commit in commits:
        revision = sha(commit["sha"])
        for pr in api.pages(f"repos/{repo}/commits/{revision}/pulls"):
            if (pr.get("merged_at") and pr["base"]["ref"] == branch
                    and pr["base"]["repo"]["full_name"] == repo
                    and pr.get("merge_commit_sha") == revision):
                candidates[pr["number"]] = revision
    if not candidates:
        return {"dispatched": [], "skipped": [], "reason": "no merged PRs in push"}
    issues = tracked_issues(api, repo)
    dispatched, skipped = [], []
    for number, merge in candidates.items():
        raw = {"repository": event["repository"],
               "inputs": {"pull-request-number": str(number), "merge-sha": merge}}
        captured = review_event(raw, api, event["ref"])
        ident = identity(captured)
        if any(marker(ident) in (issue.get("body") or "") for issue in issues):
            skipped.append({"number": number, "reason": "refutation already tracked"})
            continue
        if not has_source_changes(api, captured):
            skipped.append({"number": number, "reason": "no supported source changes"})
            continue
        api.request(f"repos/{repo}/actions/workflows/{WORKFLOW}/dispatches",
                    {"ref": branch, "inputs": raw["inputs"]})
        dispatched.append(number)
    return {"dispatched": dispatched, "skipped": skipped}


def issue_payload(bundle, body):
    ident = bundle["identity"]
    if ident.get("merged") is not True:
        raise Abstain("Only a captured merged PR can produce an NFV issue")
    repo, number = repository(ident["repository"]), int(ident["number"])
    merge = sha(ident["merge_sha"])
    feedback = (
        "\n\n## Maintainer assessment\n\n"
        "This is not an NFV verdict. Reference property IDs when recording your assessment.\n\n"
        "- [ ] Source investigation confirms an actionable source bug\n"
        "- [ ] Useful property or regression-test opportunity\n"
        "- [ ] Modeling or helper-assumption gap\n"
        "- [ ] Not actionable / already addressed\n"
        "- [ ] Further investigation needed\n\n"
        "Record rationale and any follow-up change in a comment; close when triage is complete.\n"
    )
    return {
        "title": f"NFV PR Auditor: MODEL refutations for merged PR #{number} ({merge[:12]})",
        "body": (f"Review of merged [{repo}#{number}](https://github.com/{repo}/pull/{number}). "
                 "The analysis uses the captured PR-head snapshot, not the current default-branch tip.\n\n"
                 "**Conditional MODEL refutations, not confirmed source bugs.**\n\n"
                 + body + feedback),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dispatch", "eligible", "event"))
    parser.add_argument("--event", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        from .cli import load, save
        event, api = load(args.event), GitHub()
        if args.command == "dispatch":
            print(json.dumps(dispatch(event, api)), flush=True)
        elif args.command == "eligible":
            result = eligibility(event, api, os.environ.get("GITHUB_REF"))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as stream:
                    stream.write(f"review={str(result['review']).lower()}\n")
            print(json.dumps(result), flush=True)
        else:
            if not args.out:
                raise Abstain("Event normalization requires --out")
            captured = review_event(event, api, os.environ.get("GITHUB_REF"))
            save(args.out, captured)
            if os.environ.get("GITHUB_ENV"):
                values = {"NFV_EVENT": str(Path(args.out).resolve()),
                          "NFV_BASE_REPOSITORY": repository(captured["pull_request"]["base"]["repo"]["full_name"]),
                          "NFV_BASE_SHA": sha(captured["pull_request"]["base"]["sha"])}
                if any("\n" in value or "\r" in value for value in values.values()):
                    raise Abstain("Invalid reviewer environment value")
                with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8", newline="\n") as stream:
                    stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"NFV PR Auditor input/dispatch failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
