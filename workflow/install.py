# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Agent-free NFV installation and upgrades using Python's standard library and gh."""

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
PAUSE_GATE = "vars.NFV_ENABLED != 'false'"
SHARED_FILES = (
    ".github/skills/agentic-workflows/SKILL.md",
    ".github/workflows/agentics-maintenance.yml",
)
ACTION_LOCK = ".github/aw/actions-lock.json"
WORKFLOW_FILES = (
    ".github/workflows/nfv-source-context.md",
    ".github/workflows/nfv-source-context.lock.yml",
    ".github/workflows/nfv-pr-review.md",
    ".github/workflows/nfv-pr-review.lock.yml",
)
SHA = r"[0-9a-f]{40}"
MODEL = r"[A-Za-z0-9][A-Za-z0-9_.:/-]*"
BILLING_SCOPES = {
    ".github/workflows/nfv-source-context.md": ("  ",),
    ".github/workflows/nfv-pr-review.md": ("  ", "      "),
}


class InstallError(ValueError):
    pass


def workflow_billing(source, path):
    frontmatter = source.split("\n---\n", 1)[0]
    entries = re.findall(r"(?m)^([ \t]*)copilot-requests:[ \t]*(.*)$", frontmatter)
    if not entries:
        # Older PAT-only releases omitted this permission.
        return "pat"
    if (tuple(indent for indent, _ in entries) != BILLING_SCOPES[path]
            or len({value for _, value in entries}) != 1
            or entries[0][1] not in ("none", "write")):
        raise InstallError(f"Invalid or inconsistent Copilot billing permissions: {path}")
    return "organization" if entries[0][1] == "write" else "pat"


def configure_billing(source, path, billing):
    if workflow_billing(source, path) == billing:
        return source
    frontmatter, separator, prompt = source.partition("\n---\n")
    if not re.search(r"(?m)^  copilot-requests: (none|write)$", frontmatter):
        raise InstallError(f"This NFV release does not support selecting organization billing: {path}")
    permission = "write" if billing == "organization" else "none"
    frontmatter = re.sub(r"(?m)^([ \t]*)copilot-requests: (none|write)$",
                         rf"\1copilot-requests: {permission}", frontmatter)
    return frontmatter + separator + prompt


def options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, metavar="OWNER/REPO")
    parser.add_argument("--model", help="Approved Copilot model ID (installation default: gpt-5.6-sol)")
    parser.add_argument("--billing", choices=("pat", "organization"),
                        help="Inference billing (installation default: pat; organization requires an enabled org policy)")
    parser.add_argument("--ref", default="HEAD", help="Trusted NFV ref (default: latest commit on NFV's default branch)")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--upgrade", action="store_true", help="Prepare a paused upgrade, preserving model and billing")
    phase.add_argument("--verify-bootstrap", action="store_true", help="Retry disabling NFV for the draft installation PR")
    phase.add_argument("--enable-after-merge", action="store_true", help="Enable NFV after the installation PR merges")
    parser.add_argument("--pull-request", type=int, help="Installation PR number for either follow-up phase")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--repository must be OWNER/REPO")
    if args.verify_bootstrap or args.enable_after_merge:
        if not args.pull_request or args.pull_request < 1 or args.model or args.billing is not None:
            parser.error("Follow-up phases require --pull-request NUMBER, without --model or --billing")
    elif args.upgrade:
        if args.model or args.billing is not None or args.pull_request:
            parser.error("--upgrade preserves the existing model and billing; omit --model, --billing and --pull-request")
    else:
        if args.billing is None:
            args.billing = "pat"
        if args.model is None:
            args.model = "gpt-5.6-sol"
        if not re.fullmatch(MODEL, args.model) or args.pull_request:
            parser.error("Installation requires a valid --model, without --pull-request")
    if not args.ref or any(ord(char) < 32 for char in args.ref):
        parser.error("--ref must be a nonempty trusted NFV revision")
    return args


class Installer:
    def __init__(self, args):
        self.args = args
        self.repository = args.repository
        self.root = Path.cwd()

    def run(self, *args, include_stderr=False):
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise InstallError(
                f"{shlex.join(args)} failed (exit {result.returncode}):\n{result.stderr}{result.stdout}"
            )
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        return (result.stdout + ("\n" + result.stderr if include_stderr else "")).rstrip("\r\n")

    def set_variable(self, name, value):
        self.run("gh", "variable", "set", name, "--repo", self.repository, "--body", value)
        if self.run("gh", "variable", "get", name, "--repo", self.repository) != value:
            raise InstallError(f"Variable read-back failed: {name}. Leave NFV paused.")

    def update_package(self, release):
        snapshots = {}
        for path in (*SHARED_FILES, ACTION_LOCK):
            file = self.root / path
            if file.is_symlink():
                raise InstallError(f"Shared gh-aw file is a symlink: {path}")
            snapshots[path] = file.read_bytes() if file.exists() else None
        complete = False
        try:
            command = ["gh", "aw", "add", f"{PACKAGE}/workflow@{release}"]
            if self.args.upgrade:
                command.append("--force")
            print(self.run(*command))
            for path in BILLING_SCOPES:
                file = self.root / path
                if not file.is_file():
                    raise InstallError(f"Installation did not produce {path}")
                source = file.read_text(encoding="utf-8")
                configured = configure_billing(source, path, self.args.billing)
                if configured != source:
                    file.write_text(configured, encoding="utf-8", newline="\n")
            print(self.run("gh", "aw", "compile", "nfv-source-context", "nfv-pr-review", "--approve"))
            previous = snapshots[ACTION_LOCK]
            if previous is not None:
                file = self.root / ACTION_LOCK
                old = json.loads(previous)
                current = json.loads(file.read_text(encoding="utf-8"))
                if not isinstance(old, dict) or not isinstance(current, dict):
                    raise InstallError("Invalid shared action lock; expected an object.")
                merged = {**old, **current}
                for section in ("entries", "containers"):
                    existing, generated = old.get(section, {}), current.get(section, {})
                    if not isinstance(existing, dict) or not isinstance(generated, dict):
                        raise InstallError(f"Invalid shared action lock section: {section}")
                    for key in existing.keys() & generated.keys():
                        if existing[key] != generated[key]:
                            raise InstallError(f"Shared action pin changed unexpectedly: {key}")
                    if section in old or section in current:
                        merged[section] = {**existing, **generated}
                file.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8", newline="\n")
            complete = True
        finally:
            for path, before in snapshots.items():
                if path == ACTION_LOCK and complete:
                    continue
                file = self.root / path
                if before is None:
                    if file.exists():
                        file.unlink()
                else:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(before)

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

    def check_secret_names(self, billing):
        if billing == "organization":
            target = self.api(f"repos/{self.repository}")
            if target["owner"]["type"] != "Organization":
                raise InstallError("Organization billing requires an organization-owned target repository.")
        secrets = json.loads(self.run("gh", "secret", "list", "--repo", self.repository, "--json", "name"))
        required = ["COPILOT_GITHUB_TOKEN"] if billing == "pat" else []
        if self.api(f"repos/{PACKAGE}")["private"]:
            required.append("NFV_RELEASE_TOKEN")
        names = {item["name"] for item in secrets}
        for name in required:
            if name not in names:
                raise InstallError(
                    f"Missing {name}. Enter it securely with: gh secret set {name} --repo {self.repository}. "
                    "Never pass a token value to this script."
                )

    def disable_reviewer(self, allow_unregistered=False):
        try:
            self.run("gh", "workflow", "disable", WORKFLOW, "--repo", self.repository)
            state = self.api(f"repos/{self.repository}/actions/workflows/{WORKFLOW}")["state"]
            if state != "disabled_manually":
                raise InstallError(f"Unexpected reviewer state: {state}")
        except InstallError as error:
            if allow_unregistered and re.search(r"\bHTTP 404\b", str(error)):
                if self.run("gh", "variable", "get", "NFV_ENABLED", "--repo", self.repository) != "false":
                    raise InstallError("Unregistered workflow is not protected by NFV_ENABLED=false.") from error
                print("Reviewer is not registered yet (HTTP 404); NFV_ENABLED=false keeps it paused through merge.")
                return
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
        if pr["baseRefName"] != default_branch or not re.fullmatch(r"nfv-(install|upgrade)-[0-9a-f]{12}", pr["headRefName"]):
            raise InstallError("Expected an NFV installation or upgrade PR targeting the default branch.")
        upgrade = pr["headRefName"].startswith("nfv-upgrade-")
        if self.args.verify_bootstrap:
            if pr["state"] != "OPEN" or not pr["isDraft"]:
                raise InstallError("Bootstrap verification requires an open draft installation PR.")
            release = self.run("gh", "variable", "get", "NFV_RELEASE_SHA", "--repo", self.repository)
            if not upgrade:
                record = self.api(
                    f"repos/{self.repository}/contents/{WORKFLOW_FILES[2]}?ref={pr['headRefName']}"
                )
                source = base64.b64decode("".join(record["content"].split()), validate=True).decode("utf-8")
                self.check_package_source(source, release, WORKFLOW_FILES[2])
                self.check_pause_gate(source)
            self.set_variable("NFV_ENABLED", "false")
            self.disable_reviewer(allow_unregistered=not upgrade)
            print(f"NFV is paused. Review {pr['url']}; keep it paused through marking ready and merging.")
            return
        if pr["state"] != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
            raise InstallError("NFV can be enabled only after the installation PR is merged.")
        head = self.api(f"repos/{self.repository}/commits/{quote(default_branch, safe='')}")["sha"]
        relation = self.api(f"repos/{self.repository}/compare/{pr['mergeCommit']['oid']}...{head}")
        if relation["status"] not in ("identical", "ahead"):
            raise InstallError("The installation merge is not retained on the default branch.")
        release = self.api(f"repos/{self.repository}/actions/variables/NFV_RELEASE_SHA")["value"]
        if not upgrade and (not re.fullmatch(SHA, release) or pr["headRefName"] != f"nfv-install-{release[:12]}"):
            raise InstallError("The runtime pin does not match this installation PR.")
        billing_modes = set()
        package_pins = set()
        for path in WORKFLOW_FILES:
            record = self.api(f"repos/{self.repository}/contents/{path}?ref={head}")
            if record["type"] != "file" or record["encoding"] != "base64":
                raise InstallError(f"Installed workflow is not a captured file: {path}")
            if path.endswith(".md"):
                source = base64.b64decode("".join(record["content"].split()), validate=True).decode("utf-8")
                if upgrade:
                    match = re.search(rf"(?m)^source: {re.escape(PACKAGE)}/workflow@({SHA})\r?$", source)
                    if not match:
                        raise InstallError(f"Missing immutable package pin: {path}")
                    package_pins.add(match[1])
                else:
                    self.check_package_source(source, release, path)
                if path == WORKFLOW_FILES[2]:
                    self.check_pause_gate(source)
                billing_modes.add(workflow_billing(source, path))
        if len(billing_modes) != 1:
            raise InstallError("Installed workflows disagree on Copilot billing. Leave NFV disabled.")
        billing = billing_modes.pop()
        self.check_secret_names(billing)
        model = self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository)
        if not re.fullmatch(MODEL, model):
            raise InstallError("NFV_MODEL is missing or invalid. Leave NFV disabled.")
        if upgrade:
            if len(package_pins) != 1 or pr["headRefName"] != f"nfv-upgrade-{next(iter(package_pins))[:12]}":
                raise InstallError("The upgrade PR and merged package pins disagree. Leave NFV paused.")
            self.set_variable("NFV_RELEASE_SHA", package_pins.pop())
        self.run("gh", "workflow", "enable", WORKFLOW, "--repo", self.repository)
        if self.api(f"repos/{self.repository}/actions/workflows/{WORKFLOW}")["state"] != "active":
            raise InstallError("NFV enablement was not confirmed.")
        self.set_variable("NFV_ENABLED", "true")
        print(f"NFV is enabled with {billing} billing. No audit was started. Use a separate, approved pilot PR.")

    def install(self, default_branch):
        if self.run("git", "status", "--porcelain", "--untracked-files=all"):
            raise InstallError("The checkout has local changes. Preserve them and use a clean checkout; nothing was installed.")
        if self.run("git", "branch", "--show-current") != default_branch:
            raise InstallError(f"Start from the target default branch: {default_branch}")
        if self.args.upgrade:
            modes = {workflow_billing((self.root / path).read_text(encoding="utf-8"), path)
                     for path in BILLING_SCOPES}
            if len(modes) != 1:
                raise InstallError("Existing workflows disagree on Copilot billing; no upgrade was prepared.")
            self.args.billing = modes.pop()
            self.args.model = self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository)
            if not re.fullmatch(MODEL, self.args.model):
                raise InstallError("NFV_MODEL is missing or invalid; no upgrade was prepared.")
        elif any(self.root.joinpath(path).exists() for path in WORKFLOW_FILES):
            raise InstallError("NFV files already exist. Use the documented upgrade procedure instead.")
        self.check_secret_names(self.args.billing)
        release = self.api(f"repos/{PACKAGE}/commits/{quote(self.args.ref, safe='')}")["sha"]
        if not re.fullmatch(SHA, release):
            raise InstallError("Could not resolve an immutable NFV release.")
        if not re.search(r"github/gh-aw(?:\s|$)", self.run("gh", "extension", "list")):
            self.run("gh", "extension", "install", "github/gh-aw", "--pin", COMPILER)
        version = self.run("gh", "aw", "version", include_stderr=True)
        if not re.search(rf"(?m)^gh aw version {re.escape(COMPILER)}(?:\s|$)", version):
            raise InstallError(f"Use gh-aw {COMPILER}. The installer will not replace a different installed compiler.")
        self.run("git", "fetch", "origin", default_branch)
        base = self.run("git", "rev-parse", "FETCH_HEAD")
        if self.run("git", "rev-parse", "HEAD") != base:
            raise InstallError("The local default branch differs from origin. Reconcile it before installing; no checkout was changed.")
        kind = "upgrade" if self.args.upgrade else "install"
        branch = f"nfv-{kind}-{release[:12]}"
        attributes = self.root / ".gitattributes"
        before = attributes.read_bytes() if attributes.exists() else None
        self.run("git", "switch", "-c", branch, base)
        print(f"Installing NFV {release} with model {self.args.model} and {self.args.billing} billing on {branch}.")
        self.update_package(release)
        for path in WORKFLOW_FILES:
            file = self.root / path
            if not file.is_file():
                raise InstallError(f"Installation did not produce {path}")
            if path.endswith(".md"):
                source = file.read_text(encoding="utf-8")
                self.check_package_source(source, release, path)
                if workflow_billing(source, path) != self.args.billing:
                    raise InstallError(f"Installed workflow billing differs from the selected mode: {path}")
                if path == WORKFLOW_FILES[2]:
                    self.check_pause_gate(source)
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
        self.set_variable("NFV_ENABLED", "false")
        self.disable_reviewer(allow_unregistered=not self.args.upgrade)
        self.run("git", "commit", "-m", f"{kind.capitalize()} NFV PR review at {release}")
        if not self.args.upgrade:
            for name, value in (("NFV_RELEASE_SHA", release), ("NFV_MODEL", self.args.model)):
                self.set_variable(name, value)
        self.run("git", "push", "--set-upstream", "origin", branch)
        body = (
            f"{kind.capitalize()} the NFV prototype at immutable release {release} with model {self.args.model} "
            f"and {self.args.billing} inference billing.\n\n"
            "NFV_ENABLED=false is verified; keep NFV paused through marking ready and merging. "
            "A first-install workflow may return HTTP 404 until merge; the pause latch prevents activation. "
            "Registered workflows are also disabled. Leave unrelated CI enabled. "
            "Do not require an NFV check for its own installation PR.\n\n"
            "Review workflows, generated locks, package/action records and any .gitattributes "
            "change. Do not merge without approval. Enable NFV only after the installation "
            "merges into the default branch. Do not start a pilot audit without separate approval."
        )
        url = self.run(
            "gh", "pr", "create", "--repo", self.repository, "--base", default_branch,
            "--head", branch, "--draft", "--title", f"{kind.capitalize()} NFV PR review", "--body", body,
        )
        print(f"Draft installation PR: {url}")
        created = json.loads(self.run(
            "gh", "pr", "view", url, "--repo", self.repository,
            "--json", "number,isDraft,headRefName,baseRefName",
        ))
        if not created["isDraft"] or created["headRefName"] != branch or created["baseRefName"] != default_branch:
            raise InstallError("Installation PR identity/draft status was not confirmed. Do not mark ready or merge.")
        print(f"NFV is paused. Review PR {created['number']}, then approve and merge while it remains paused.")
        print("After merge: " + shlex.join([
            sys.executable, str(Path(__file__).resolve()), "--repository", self.repository,
            "--enable-after-merge", "--pull-request", str(created["number"]),
        ]))
        print("Only secret names were checked; no PR code was executed and no NFV audit was started.")
        if self.args.billing == "organization":
            print("Confirm the organization permits CLI billing and the selected model. "
                  "Policy, model access and billing attribution require the approved pilot; no PAT fallback is used.")

    def check_pause_gate(self, source):
        if source.count(PAUSE_GATE) != 2:
            raise InstallError("This release lacks the verified pre-merge NFV pause gate; use a newer trusted release.")

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
