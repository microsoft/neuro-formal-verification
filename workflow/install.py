"""Agent-free initial NFV installation using Python's standard library and gh."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import quote


PACKAGE = "microsoft/neuro-formal-verification"
WORKFLOW = "nfv-pr-review.lock.yml"
COMPILER = "v0.88.7"
WORKFLOW_FILES = (
    ".github/workflows/nfv-source-context.md",
    ".github/workflows/nfv-source-context.lock.yml",
    ".github/workflows/nfv-pr-review.md",
    ".github/workflows/nfv-pr-review.lock.yml",
)
SHA = r"[0-9a-f]{40}"
MODEL = r"[A-Za-z0-9][A-Za-z0-9_.:/-]*"


class InstallError(ValueError):
    pass


def options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, metavar="OWNER/REPO")
    parser.add_argument("--model", help="Approved Copilot model ID; required for installation")
    parser.add_argument("--ref", default="HEAD", help="Trusted NFV ref (default: latest commit on NFV's default branch)")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--verify-bootstrap", action="store_true", help="Retry disabling NFV for the draft installation PR")
    phase.add_argument("--enable-after-merge", action="store_true", help="Enable NFV after the installation PR merges")
    parser.add_argument("--pull-request", type=int, help="Installation PR number for either follow-up phase")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--repository must be OWNER/REPO")
    if args.verify_bootstrap or args.enable_after_merge:
        if not args.pull_request or args.pull_request < 1 or args.model:
            parser.error("Follow-up phases require --pull-request NUMBER, without --model")
    elif not args.model or not re.fullmatch(MODEL, args.model) or args.pull_request:
        parser.error("Installation requires --model APPROVED_MODEL, without --pull-request")
    if not args.ref or any(ord(char) < 32 for char in args.ref):
        parser.error("--ref must be a nonempty trusted NFV revision")
    return args


class Installer:
    def __init__(self, args):
        self.args = args
        self.repository = args.repository
        self.root = Path.cwd()

    def run(self, *args):
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise InstallError(
                f"{shlex.join(args)} failed (exit {result.returncode}):\n{result.stderr}{result.stdout}"
            )
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        return result.stdout.rstrip("\r\n")

    def api(self, endpoint):
        return json.loads(self.run("gh", "api", "--hostname", "github.com", endpoint))

    def check_checkout_identity(self):
        if os.environ.get("GH_HOST", "github.com") != "github.com":
            raise InstallError("This installer supports github.com only; GH_HOST points elsewhere.")
        for program in ("git", "gh"):
            if not shutil.which(program):
                raise InstallError(f"Install {program} before running this script.")
        self.run("gh", "auth", "status", "--hostname", "github.com")
        self.root = Path(self.run("git", "rev-parse", "--show-toplevel"))
        remote_pattern = (
            r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
            + re.escape(self.repository) + r"(?:\.git)?"
        )
        for flags in (("--all",), ("--push", "--all")):
            remote = self.run("git", "remote", "get-url", *flags, "origin")
            if not re.fullmatch(remote_pattern, remote, re.I):
                raise InstallError("The origin fetch and push URLs must both identify the target GitHub repository.")
        repo = json.loads(self.run(
            "gh", "repo", "view", self.repository, "--json", "nameWithOwner,defaultBranchRef",
        ))
        branch = (repo.get("defaultBranchRef") or {}).get("name")
        if repo["nameWithOwner"].lower() != self.repository.lower() or not branch:
            raise InstallError("Could not verify the target repository and its default branch.")
        return branch

    def check_secret_names(self):
        secrets = json.loads(self.run("gh", "secret", "list", "--repo", self.repository, "--json", "name"))
        required = ["COPILOT_GITHUB_TOKEN"]
        if self.api(f"repos/{PACKAGE}")["private"]:
            required.append("NFV_RELEASE_TOKEN")
        names = {item["name"] for item in secrets}
        for name in required:
            if name not in names:
                raise InstallError(
                    f"Missing {name}. Enter it securely with: gh secret set {name} --repo {self.repository}. "
                    "Never pass a token value to this script."
                )

    def disable_reviewer(self):
        try:
            self.run("gh", "workflow", "disable", WORKFLOW, "--repo", self.repository)
            state = self.api(f"repos/{self.repository}/actions/workflows/{WORKFLOW}")["state"]
            if state != "disabled_manually":
                raise InstallError(f"Unexpected reviewer state: {state}")
        except InstallError as error:
            raise InstallError(
                "Keep the installation PR draft; NFV is not confirmed disabled. "
                "If GitHub has not registered it yet, retry --verify-bootstrap --pull-request NUMBER "
                f"after registration. Do not mark ready or merge.\n{error}"
            ) from error

    def check_package_source(self, source, release, path):
        if not re.search(rf"(?m)^source: {re.escape(PACKAGE)}/workflow@{release}\r?$", source):
            raise InstallError(f"Installed package and runtime pin differ: {path}")

    def follow_up(self, default_branch):
        pr = json.loads(self.run(
            "gh", "pr", "view", str(self.args.pull_request), "--repo", self.repository,
            "--json", "state,isDraft,baseRefName,headRefName,mergeCommit,url",
        ))
        if pr["baseRefName"] != default_branch or not re.fullmatch(r"nfv-install-[0-9a-f]{12}", pr["headRefName"]):
            raise InstallError("Expected an NFV installation PR targeting the default branch.")
        if self.args.verify_bootstrap:
            if pr["state"] != "OPEN" or not pr["isDraft"]:
                raise InstallError("Bootstrap verification requires an open draft installation PR.")
            self.disable_reviewer()
            print(f"NFV is disabled. Review {pr['url']}; keep it disabled through marking ready and merging.")
            return
        if pr["state"] != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
            raise InstallError("NFV can be enabled only after the installation PR is merged.")
        head = self.api(f"repos/{self.repository}/commits/{quote(default_branch, safe='')}")["sha"]
        relation = self.api(f"repos/{self.repository}/compare/{pr['mergeCommit']['oid']}...{head}")
        if relation["status"] not in ("identical", "ahead"):
            raise InstallError("The installation merge is not retained on the default branch.")
        release = self.api(f"repos/{self.repository}/actions/variables/NFV_RELEASE_SHA")["value"]
        if not re.fullmatch(SHA, release) or pr["headRefName"] != f"nfv-install-{release[:12]}":
            raise InstallError("The runtime pin does not match this installation PR.")
        for path in WORKFLOW_FILES:
            record = self.api(f"repos/{self.repository}/contents/{path}?ref={head}")
            if record["type"] != "file" or record["encoding"] != "base64":
                raise InstallError(f"Installed workflow is not a captured file: {path}")
            if path.endswith(".md"):
                source = base64.b64decode("".join(record["content"].split()), validate=True).decode("utf-8")
                self.check_package_source(source, release, path)
        self.check_secret_names()
        model = self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository)
        if not re.fullmatch(MODEL, model):
            raise InstallError("NFV_MODEL is missing or invalid. Leave NFV disabled.")
        self.run("gh", "workflow", "enable", WORKFLOW, "--repo", self.repository)
        if self.api(f"repos/{self.repository}/actions/workflows/{WORKFLOW}")["state"] != "active":
            raise InstallError("NFV enablement was not confirmed.")
        print("NFV is enabled. No audit was started. Use a separate, approved pilot PR.")

    def install(self, default_branch):
        if self.run("git", "status", "--porcelain", "--untracked-files=all"):
            raise InstallError("The checkout has local changes. Preserve them and use a clean checkout; nothing was installed.")
        if self.run("git", "branch", "--show-current") != default_branch:
            raise InstallError(f"Start from the target default branch: {default_branch}")
        if any(self.root.joinpath(path).exists() for path in WORKFLOW_FILES):
            raise InstallError("NFV files already exist. Use the documented upgrade procedure instead.")
        self.check_secret_names()
        release = self.api(f"repos/{PACKAGE}/commits/{quote(self.args.ref, safe='')}")["sha"]
        if not re.fullmatch(SHA, release):
            raise InstallError("Could not resolve an immutable NFV release.")
        if not re.search(r"github/gh-aw(?:\s|$)", self.run("gh", "extension", "list")):
            self.run("gh", "extension", "install", "github/gh-aw", "--pin", COMPILER)
        if not re.match(rf"gh aw version {re.escape(COMPILER)}(?:\s|$)", self.run("gh", "aw", "version")):
            raise InstallError(f"Use gh-aw {COMPILER}. The installer will not replace a different installed compiler.")
        self.run("git", "fetch", "origin", default_branch)
        base = self.run("git", "rev-parse", "FETCH_HEAD")
        if self.run("git", "rev-parse", "HEAD") != base:
            raise InstallError("The local default branch differs from origin. Reconcile it before installing; no checkout was changed.")
        branch = f"nfv-install-{release[:12]}"
        attributes = self.root / ".gitattributes"
        before = attributes.read_bytes() if attributes.exists() else None
        self.run("git", "switch", "-c", branch, base)
        print(f"Installing NFV {release} with model {self.args.model} on {branch}.")
        print(self.run("gh", "aw", "add", f"{PACKAGE}/workflow@{release}"))
        print(self.run("gh", "aw", "compile", "nfv-source-context", "nfv-pr-review", "--approve"))
        for path in WORKFLOW_FILES:
            file = self.root / path
            if not file.is_file():
                raise InstallError(f"Installation did not produce {path}")
            if path.endswith(".md"):
                self.check_package_source(file.read_text(encoding="utf-8"), release, path)
        if before is not None and not attributes.exists():
            raise InstallError(".gitattributes was removed. Review manually; nothing was committed.")
        if attributes.exists():
            after = attributes.read_bytes()
            if not after.startswith(before or b""):
                raise InstallError("Unexpected .gitattributes edits; review them manually. Nothing was committed.")
            for line in after[len(before or b""):].decode("utf-8").splitlines():
                if not re.fullmatch(r"\s*(?:#.*|\.github/workflows/\*\.lock\.yml linguist-generated=true)?\s*", line):
                    raise InstallError("Unexpected .gitattributes rule; review it manually. Nothing was committed.")
        changed = sorted(set(
            self.run("git", "diff", "HEAD", "--name-only").splitlines()
            + self.run("git", "ls-files", "--others", "--exclude-standard").splitlines()
        ))
        for path in changed:
            if path not in WORKFLOW_FILES and path != ".gitattributes" and not path.startswith(".github/aw/"):
                raise InstallError(f"Unexpected installer change: {path}. Nothing was committed.")
        for path in WORKFLOW_FILES:
            if path not in changed:
                raise InstallError(f"Generated workflow is excluded from the Git changes: {path}. Check ignore rules; nothing was committed.")
        self.run("git", "add", "--", *changed)
        self.run("git", "diff", "--cached", "--check")
        self.run("git", "commit", "-m", f"Install NFV PR review at {release}")
        for name, value in (("NFV_RELEASE_SHA", release), ("NFV_MODEL", self.args.model)):
            self.run("gh", "variable", "set", name, "--repo", self.repository, "--body", value)
            if self.run("gh", "variable", "get", name, "--repo", self.repository) != value:
                raise InstallError(f"Variable read-back failed: {name}. Do not proceed with installation.")
        self.run("git", "push", "--set-upstream", "origin", branch)
        body = (
            f"Install the NFV prototype at immutable release {release} with model {self.args.model}.\n\n"
            "Keep this PR draft until nfv-pr-review.lock.yml is confirmed disabled. Keep NFV "
            "disabled through marking ready and merging; leave unrelated CI enabled. "
            "Do not require an NFV check for its own installation PR.\n\n"
            "Review workflows, generated locks, package/action records and any .gitattributes "
            "change. Do not merge without approval. Enable NFV only after the installation "
            "merges into the default branch. Do not start a pilot audit without separate approval."
        )
        url = self.run(
            "gh", "pr", "create", "--repo", self.repository, "--base", default_branch,
            "--head", branch, "--draft", "--title", "Install NFV PR review", "--body", body,
        )
        print(f"Draft installation PR: {url}")
        created = json.loads(self.run(
            "gh", "pr", "view", url, "--repo", self.repository,
            "--json", "number,isDraft,headRefName,baseRefName",
        ))
        if not created["isDraft"] or created["headRefName"] != branch or created["baseRefName"] != default_branch:
            raise InstallError("Installation PR identity/draft status was not confirmed. Do not mark ready or merge.")
        self.disable_reviewer()
        print(f"NFV is disabled. Review PR {created['number']}, then approve and merge while it remains disabled.")
        print("After merge: " + shlex.join([
            sys.executable, str(Path(__file__).resolve()), "--repository", self.repository,
            "--enable-after-merge", "--pull-request", str(created["number"]),
        ]))
        print("Only secret names were checked; no PR code was executed and no NFV audit was started.")

    def execute(self):
        default_branch = self.check_checkout_identity()
        if self.args.verify_bootstrap or self.args.enable_after_merge:
            self.follow_up(default_branch)
        else:
            self.install(default_branch)


def main(argv=None):
    args = options(argv)
    try:
        Installer(args).execute()
    except (OSError, ValueError) as error:
        print(f"NFV installation blocked: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
