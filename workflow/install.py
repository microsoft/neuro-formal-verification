# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

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
COMPILER = "v1.2.2"
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
SHARED_FILES = (
    ".github/skills/agentic-workflows/SKILL.md",
    ".github/workflows/agentics-maintenance.yml",
)
ACTION_LOCK = ".github/aw/actions-lock.json"
ACTIVATION_GATE = "vars.NFV_RELEASE_SHA != ''"


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


def configure_copilot_permissions(source, path, billing):
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
    phase.add_argument("--verify-bootstrap", action="store_true", help="Verify inference stays gated off for the draft installation PR")
    phase.add_argument("--enable-after-merge", action="store_true", help="Enable NFV after the installation PR merges")
    parser.add_argument("--pull-request", type=int, help="Installation PR number for either follow-up phase")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--repository must be OWNER/REPO")
    if args.verify_bootstrap or args.enable_after_merge:
        if not args.pull_request or args.pull_request < 1 or args.model or args.billing is not None:
            parser.error("Follow-up phases require --pull-request NUMBER, without --model or --billing")
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

    def run(self, *args, combined_output=False):
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise InstallError(
                f"{shlex.join(args)} failed (exit {result.returncode}):\n{result.stderr}{result.stdout}"
            )
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        output = result.stdout + ("\n" + result.stderr if combined_output else "")
        return output.rstrip("\r\n")

    def api(self, endpoint):
        return json.loads(self.run("gh", "api", "--hostname", "github.com", endpoint))

    def runtime_pin(self):
        variables = json.loads(self.run(
            "gh", "variable", "list", "--repo", self.repository, "--json", "name,value"))
        return next((item["value"] for item in variables if item["name"] == "NFV_RELEASE_SHA"), "")

    def require_inert_bootstrap(self):
        if self.runtime_pin():
            raise InstallError(
                "NFV_RELEASE_SHA must be unset during first installation. "
                "Confirm it is not used by an existing installation before removing it; no variable was changed.")
        if self.api(f"repos/{self.repository}")["owner"]["type"] == "Organization":
            inherited = self.run(
                "gh", "api", "--hostname", "github.com", "--paginate",
                f"repos/{self.repository}/actions/organization-variables?per_page=30",
                "--jq", '.variables[] | select(.name == "NFV_RELEASE_SHA") | .value')
            if inherited:
                raise InstallError(
                    "NFV_RELEASE_SHA must be unset in applicable organization variables too. "
                    "The inherited value would bypass the first-install gate; no variable was changed.")

    def check_activation_gate(self, source, path):
        if path.endswith(".md"):
            frontmatter = source.split("\n---\n", 1)[0].split("\nsteps:", 1)[0]
            conditions = re.findall(r"(?m)^(?:    )?if: (.*)$", frontmatter)
            expected = 1
            if path.endswith("nfv-pr-review.md"):
                expected = 3 if any(f"\n  {job}:\n" in frontmatter
                                    for job in ("nfv_admission", "admission")) else 2
            if len(conditions) != expected or any(
                    condition != ACTIVATION_GATE and not condition.startswith(ACTIVATION_GATE + " && ")
                    for condition in conditions):
                raise InstallError(f"Workflow lacks the first-install activation gate: {path}")
        else:
            activation = "pre_activation" if "\n  pre_activation:\n" in source else "activation"
            jobs = [activation]
            if path.endswith(WORKFLOW):
                jobs.extend(job for job in ("nfv_admission", "admission")
                            if f"\n  {job}:\n" in source)
                jobs.append("repository_context")
            for job in jobs:
                block = re.search(rf"(?ms)^  {job}:\n(.*?)(?=^  \S|\Z)", source)
                condition = re.search(r"(?m)^    if: ([^\n]*(?:\n      [^\n]*)*)", block[1]) if block else None
                if not condition or not re.match(
                        rf"(?:>\s*)?\(*{re.escape(ACTIVATION_GATE)}(?=\s*(?:&&|\)|$))", condition[1]):
                    raise InstallError(f"Compiled workflow lacks its activation gate: {path}:{job}")

    def snapshot_shared_files(self):
        snapshot = {}
        for path in (*SHARED_FILES, ACTION_LOCK):
            file = self.root / path
            if file.is_symlink() or file.exists() and not file.is_file():
                raise InstallError(f"Shared gh-aw asset must be a regular file: {path}")
            snapshot[path] = file.read_bytes() if file.exists() else None
        return snapshot

    def restore_shared_files(self, snapshot, *, merge_lock):
        for path in SHARED_FILES:
            file, before = self.root / path, snapshot[path]
            if before is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(before)
        file, before = self.root / ACTION_LOCK, snapshot[ACTION_LOCK]
        if not merge_lock:
            if before is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(before)
        else:
            after = file.read_bytes() if file.exists() else None
            if before is not None:
                file.write_bytes(before)
            if after is None:
                raise InstallError("Compilation did not produce the shared gh-aw action lock.")
            original = json.loads(before) if before is not None else {"entries": {}}
            generated = json.loads(after)
            if (not isinstance(original, dict) or not isinstance(generated, dict)
                    or not isinstance(original.get("entries"), dict)
                    or not isinstance(generated.get("entries"), dict)):
                raise InstallError("Invalid shared gh-aw action lock; no existing pins were changed.")
            if before is None:
                return
            merged = dict(original["entries"])
            for key, entry in generated["entries"].items():
                if key in merged and merged[key] != entry:
                    raise InstallError(f"Compiler changed an existing action pin: {key}; original lock restored.")
                merged[key] = entry
            if merged != original["entries"]:
                file.write_text(json.dumps({**original, "entries": merged}, indent=2) + "\n",
                                encoding="utf-8", newline="\n")

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
                "NFV is not confirmed disabled. Leave NFV_RELEASE_SHA unset and retry "
                f"post-merge activation after workflow registration.\n{error}"
            ) from error

    def check_package_source(self, source, release, path):
        if not re.search(rf"(?m)^source: {re.escape(PACKAGE)}/workflow@{release}\r?$", source):
            raise InstallError(f"Installed package and runtime pin differ: {path}")

    def follow_up(self, default_branch):
        pr = json.loads(self.run(
            "gh", "pr", "view", str(self.args.pull_request), "--repo", self.repository,
            "--json", "state,isDraft,baseRefName,headRefName,headRefOid,mergeCommit,url",
        ))
        if pr["baseRefName"] != default_branch or not re.fullmatch(r"nfv-install-[0-9a-f]{12}", pr["headRefName"]):
            raise InstallError("Expected an NFV installation PR targeting the default branch.")
        if self.args.verify_bootstrap:
            if pr["state"] != "OPEN" or not pr["isDraft"]:
                raise InstallError("Bootstrap verification requires an open draft installation PR.")
            self.require_inert_bootstrap()
            for path in WORKFLOW_FILES:
                record = self.api(f"repos/{self.repository}/contents/{path}?ref={pr['headRefOid']}")
                source = base64.b64decode("".join(record["content"].split()), validate=True).decode("utf-8")
                self.check_activation_gate(source, path)
            print(f"NFV inference is gated off. Review {pr['url']}; leave NFV_RELEASE_SHA unset through merge.")
            return
        if pr["state"] != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
            raise InstallError("NFV can be enabled only after the installation PR is merged.")
        head = self.api(f"repos/{self.repository}/commits/{quote(default_branch, safe='')}")["sha"]
        relation = self.api(f"repos/{self.repository}/compare/{pr['mergeCommit']['oid']}...{head}")
        if relation["status"] not in ("identical", "ahead"):
            raise InstallError("The installation merge is not retained on the default branch.")
        self.disable_reviewer()
        release = None
        billing_modes = set()
        for path in WORKFLOW_FILES:
            record = self.api(f"repos/{self.repository}/contents/{path}?ref={head}")
            if record["type"] != "file" or record["encoding"] != "base64":
                raise InstallError(f"Installed workflow is not a captured file: {path}")
            source = base64.b64decode("".join(record["content"].split()), validate=True).decode("utf-8")
            self.check_activation_gate(source, path)
            if path.endswith(".md"):
                pin = re.search(rf"(?m)^source: {re.escape(PACKAGE)}/workflow@({SHA})\r?$", source)
                if release is None:
                    release = pin[1] if pin else ""
                if not re.fullmatch(SHA, release) or pr["headRefName"] != f"nfv-install-{release[:12]}":
                    raise InstallError("The installed package pin does not match this installation PR.")
                self.check_package_source(source, release, path)
                billing_modes.add(workflow_billing(source, path))
        if len(billing_modes) != 1:
            raise InstallError("Installed workflows disagree on Copilot billing. Leave NFV disabled.")
        billing = billing_modes.pop()
        self.check_secret_names(billing)
        model = self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository)
        if not re.fullmatch(MODEL, model):
            raise InstallError("NFV_MODEL is missing or invalid. Leave NFV disabled.")
        current_pin = self.runtime_pin()
        if current_pin and current_pin != release:
            raise InstallError("The runtime pin does not match this installation PR. Leave NFV disabled.")
        self.run("gh", "variable", "set", "NFV_RELEASE_SHA", "--repo", self.repository, "--body", release)
        if self.runtime_pin() != release:
            raise InstallError("Runtime pin read-back failed. Leave NFV disabled.")
        self.run("gh", "workflow", "enable", WORKFLOW, "--repo", self.repository)
        if self.api(f"repos/{self.repository}/actions/workflows/{WORKFLOW}")["state"] != "active":
            raise InstallError("NFV enablement was not confirmed.")
        print(f"NFV is enabled with {billing} billing. No audit was started. Use a separate, approved pilot PR.")

    def install(self, default_branch):
        if self.run("git", "status", "--porcelain", "--untracked-files=all"):
            raise InstallError("The checkout has local changes. Preserve them and use a clean checkout; nothing was installed.")
        if self.run("git", "branch", "--show-current") != default_branch:
            raise InstallError(f"Start from the target default branch: {default_branch}")
        if any(self.root.joinpath(path).exists() for path in WORKFLOW_FILES):
            raise InstallError("NFV files already exist. Use the documented upgrade procedure instead.")
        self.check_secret_names(self.args.billing)
        self.require_inert_bootstrap()
        release = self.api(f"repos/{PACKAGE}/commits/{quote(self.args.ref, safe='')}")["sha"]
        if not re.fullmatch(SHA, release):
            raise InstallError("Could not resolve an immutable NFV release.")
        if not re.search(r"github/gh-aw(?:\s|$)", self.run("gh", "extension", "list")):
            self.run("gh", "extension", "install", "github/gh-aw", "--pin", COMPILER)
        version = self.run("gh", "aw", "version", combined_output=True)
        if re.findall(r"(?m)^gh aw version (v[^\s]+)", version) != [COMPILER]:
            raise InstallError(f"Use gh-aw {COMPILER}. The installer will not replace a different installed compiler.")
        self.run("git", "fetch", "origin", default_branch)
        base = self.run("git", "rev-parse", "FETCH_HEAD")
        if self.run("git", "rev-parse", "HEAD") != base:
            raise InstallError("The local default branch differs from origin. Reconcile it before installing; no checkout was changed.")
        branch = f"nfv-install-{release[:12]}"
        attributes = self.root / ".gitattributes"
        before = attributes.read_bytes() if attributes.exists() else None
        self.run("git", "switch", "-c", branch, base)
        print(f"Installing NFV {release} with model {self.args.model} and {self.args.billing} billing on {branch}.")
        shared = self.snapshot_shared_files()
        compiled = False
        try:
            print(self.run("gh", "aw", "add", f"{PACKAGE}/workflow@{release}"))
            for path in BILLING_SCOPES:
                file = self.root / path
                if not file.is_file():
                    raise InstallError(f"Installation did not produce {path}")
                source = file.read_text(encoding="utf-8")
                configured = configure_copilot_permissions(source, path, self.args.billing)
                if configured != source:
                    file.write_text(configured, encoding="utf-8", newline="\n")
            print(self.run("gh", "aw", "compile", "nfv-source-context", "nfv-pr-review", "--approve"))
            compiled = True
        finally:
            self.restore_shared_files(shared, merge_lock=compiled)
        for path in WORKFLOW_FILES:
            file = self.root / path
            if not file.is_file():
                raise InstallError(f"Installation did not produce {path}")
            self.check_activation_gate(file.read_text(encoding="utf-8"), path)
            if path.endswith(".md"):
                source = file.read_text(encoding="utf-8")
                self.check_package_source(source, release, path)
                if workflow_billing(source, path) != self.args.billing:
                    raise InstallError(f"Installed workflow billing differs from the selected mode: {path}")
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
        if set(self.run("git", "diff", "--cached", "--name-only").splitlines()) != set(changed):
            raise InstallError("Unexpected staged installer changes. Nothing was committed.")
        self.run("git", "diff", "--cached", "--check")
        self.run("git", "commit", "-m", f"Install NFV PR review at {release}")
        self.run("gh", "variable", "set", "NFV_MODEL", "--repo", self.repository, "--body", self.args.model)
        if self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository) != self.args.model:
            raise InstallError("Variable read-back failed: NFV_MODEL. Do not proceed with installation.")
        self.run("git", "push", "--set-upstream", "origin", branch)
        body = (
            f"Install the NFV prototype at immutable release {release} with model {self.args.model} "
            f"and {self.args.billing} inference billing.\n\n"
            "Leave NFV_RELEASE_SHA unset through marking ready and merging: both inference "
            "sessions are gated off even before GitHub registers the workflow. Leave unrelated CI enabled. "
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
        self.require_inert_bootstrap()
        print(f"NFV inference is gated off. Review PR {created['number']}; leave NFV_RELEASE_SHA unset through merge.")
        print("After merge: " + shlex.join([
            sys.executable, str(Path(__file__).resolve()), "--repository", self.repository,
            "--enable-after-merge", "--pull-request", str(created["number"]),
        ]))
        print("Only secret names were checked; no PR code was executed and no NFV audit was started.")
        if self.args.billing == "organization":
            print("Confirm the organization permits CLI billing and the selected model. "
                  "Policy, model access and billing attribution require the approved pilot; no PAT fallback is used.")

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
