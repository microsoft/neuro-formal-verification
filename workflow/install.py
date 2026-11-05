# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Install or upgrade NFV PR Auditor using standard-library Python and gh."""

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
COMPILER = "v0.88.7"
COMMON_FILES = (
    ".github/workflows/nfv-source-context.md",
    ".github/workflows/nfv-source-context.lock.yml",
)
REVIEWERS = {"pr": "nfv-pr-review", "post-merge": "nfv-pr-auditor",
             "legacy-post-merge": "nfv-post-merge-review"}
DISPATCHERS = {"post-merge": ".github/workflows/nfv-pr-auditor-dispatch.yml",
               "legacy-post-merge": ".github/workflows/nfv-post-merge-dispatch.yml"}
DISPATCHER = DISPATCHERS["post-merge"]


def workflow_files(mode):
    name = REVIEWERS[mode]
    return (*COMMON_FILES, f".github/workflows/{name}.md",
            f".github/workflows/{name}.lock.yml", *((DISPATCHERS[mode],) if mode in DISPATCHERS else ()))


WORKFLOW = REVIEWERS["post-merge"] + ".lock.yml"
WORKFLOW_FILES = workflow_files("post-merge")
ALL_FILES = set().union(*(workflow_files(mode) for mode in REVIEWERS))
SHA = r"[0-9a-f]{40}"
MODEL = r"[A-Za-z0-9][A-Za-z0-9_.:/-]*"
BILLING_SCOPES = {
    ".github/workflows/nfv-source-context.md": ("  ",),
    ".github/workflows/nfv-pr-review.md": ("  ", "      "),
    ".github/workflows/nfv-post-merge-review.md": ("  ", "      "),
    ".github/workflows/nfv-pr-auditor.md": ("  ", "      "),
}


def installed_mode(paths):
    captured = set(paths) & ALL_FILES
    modes = [mode for mode in REVIEWERS if captured == set(workflow_files(mode))]
    if len(modes) != 1:
        raise InstallError("Expected exactly one complete NFV review mode, with no other reviewer or dispatcher files.")
    return modes[0]


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
    phase.add_argument("--upgrade", action="store_true", help="Pause, drain and replace an existing NFV installation")
    phase.add_argument("--verify-bootstrap", action="store_true", help="Retry disabling NFV for the draft installation PR")
    phase.add_argument("--enable-after-merge", action="store_true", help="Enable NFV after the installation PR merges")
    parser.add_argument("--pull-request", type=int, help="Installation PR number for either follow-up phase")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--repository must be OWNER/REPO")
    if args.verify_bootstrap or args.enable_after_merge:
        if (not args.pull_request or args.pull_request < 1 or args.model
                or args.billing is not None):
            parser.error("Follow-up phases require --pull-request NUMBER, without --model or --billing")
    else:
        if args.billing is None and not args.upgrade:
            args.billing = "pat"
        if args.model is None and not args.upgrade:
            args.model = "gpt-5.6-sol"
        if (args.model is not None and not re.fullmatch(MODEL, args.model)) or args.pull_request:
            parser.error("Installation requires a valid --model, without --pull-request")
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

    def selected_workflows(self, mode):
        names = [REVIEWERS[mode] + ".lock.yml"]
        if mode in DISPATCHERS:
            names.append(Path(DISPATCHERS[mode]).name)
        return names

    def disable_reviewer(self, mode):
        try:
            for name in self.selected_workflows(mode):
                self.run("gh", "workflow", "disable", name, "--repo", self.repository)
                state = self.api(f"repos/{self.repository}/actions/workflows/{name}")["state"]
                if state != "disabled_manually":
                    raise InstallError(f"Unexpected {name} state: {state}")
        except InstallError as error:
            raise InstallError(
                "Keep the installation PR draft; NFV is not confirmed disabled. "
                "If GitHub has not registered it yet, retry --verify-bootstrap --pull-request NUMBER "
                f"after registration. Do not mark ready or merge.\n{error}"
            ) from error

    def check_package_source(self, source, release, path):
        if not re.search(rf"(?m)^source: {re.escape(PACKAGE)}/workflow@{release}\r?$", source):
            raise InstallError(f"Installed package and runtime pin differ: {path}")

    def workflows(self):
        rows = []
        for page in range(1, 101):
            result = self.api(f"repos/{self.repository}/actions/workflows?per_page=100&page={page}")
            rows.extend(result["workflows"])
            if len(rows) == result["total_count"]:
                return rows
            if len(result["workflows"]) < 100:
                break
        raise InstallError("Incomplete registered-workflow inventory; leave NFV disabled.")

    def check_exclusive_activation(self, mode):
        other = {name for candidate in REVIEWERS if candidate != mode
                 for name in self.selected_workflows(candidate)}
        for record in self.workflows():
            if Path(record["path"]).name in other and record["state"] == "active":
                raise InstallError("The unselected NFV mode is still active. Leave NFV disabled.")

    def captured_file(self, repo, path, revision):
        record = self.api(f"repos/{repo}/contents/{path}?ref={quote(revision, safe='')}")
        if record["type"] != "file" or record["encoding"] != "base64":
            raise InstallError(f"Workflow is not a captured file: {path}")
        return base64.b64decode("".join(record["content"].split()), validate=True).decode("utf-8")

    def check_dispatcher_pin(self, source, release):
        if not source.startswith(f"# NFV package: {PACKAGE}/workflow@{release}\n"):
            raise InstallError("The PR Auditor dispatcher package pin differs from NFV_RELEASE_SHA.")

    def follow_up(self, default_branch):
        pr = json.loads(self.run(
            "gh", "pr", "view", str(self.args.pull_request), "--repo", self.repository,
            "--json", "state,isDraft,baseRefName,headRefName,mergeCommit,url",
        ))
        branch = re.fullmatch(r"nfv-(install|upgrade)-(?:(pr|post-merge|pr-auditor)-)?([0-9a-f]{12})", pr["headRefName"])
        if pr["baseRefName"] != default_branch or not branch:
            raise InstallError("Expected an NFV installation PR targeting the default branch.")
        mode = {"pr-auditor": "post-merge", "post-merge": "legacy-post-merge"}.get(branch[2], "pr")
        if self.args.verify_bootstrap:
            if pr["state"] != "OPEN" or not pr["isDraft"]:
                raise InstallError("Bootstrap verification requires an open draft installation PR.")
            self.disable_reviewer(mode)
            print(f"NFV is disabled. Review {pr['url']}; keep it disabled through marking ready and merging.")
            return
        if mode != "post-merge":
            raise InstallError("This legacy reviewer must upgrade to NFV PR Auditor before activation.")
        if pr["state"] != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
            raise InstallError("NFV can be enabled only after the installation PR is merged.")
        head = self.api(f"repos/{self.repository}/commits/{quote(default_branch, safe='')}")["sha"]
        relation = self.api(f"repos/{self.repository}/compare/{pr['mergeCommit']['oid']}...{head}")
        if relation["status"] not in ("identical", "ahead"):
            raise InstallError("The installation merge is not retained on the default branch.")
        release = self.api(f"repos/{self.repository}/actions/variables/NFV_RELEASE_SHA")["value"]
        if not re.fullmatch(SHA, release) or branch[3] != release[:12]:
            raise InstallError("The runtime pin does not match this installation PR.")
        listing = self.api(f"repos/{self.repository}/contents/.github/workflows?ref={head}")
        paths = [f".github/workflows/{record['name']}" for record in listing]
        if installed_mode(paths) != mode:
            raise InstallError("Merged files do not match the selected NFV review mode.")
        billing_modes = set()
        for path in workflow_files(mode):
            source = self.captured_file(self.repository, path, head)
            if path.endswith(".md"):
                self.check_package_source(source, release, path)
                billing_modes.add(workflow_billing(source, path))
            elif path == DISPATCHER:
                self.check_dispatcher_pin(source, release)
        if len(billing_modes) != 1:
            raise InstallError("Installed workflows disagree on Copilot billing. Leave NFV disabled.")
        billing = billing_modes.pop()
        self.check_secret_names(billing)
        model = self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository)
        if not re.fullmatch(MODEL, model):
            raise InstallError("NFV_MODEL is missing or invalid. Leave NFV disabled.")
        self.check_exclusive_activation(mode)
        if mode == "post-merge":
            self.run("gh", "label", "create", "NFV-formal-review", "--repo", self.repository,
                     "--color", "5319e7", "--description", "Conditional NFV MODEL refutations", "--force")
        try:
            for name in self.selected_workflows(mode):
                self.run("gh", "workflow", "enable", name, "--repo", self.repository)
                if self.api(f"repos/{self.repository}/actions/workflows/{name}")["state"] != "active":
                    raise InstallError(f"NFV enablement was not confirmed: {name}")
            self.check_exclusive_activation(mode)
        except InstallError:
            self.disable_reviewer(mode)
            raise
        print(f"NFV PR Auditor is enabled with {billing} billing. No audit was started. Use a separate, approved pilot PR.")

    def install(self, default_branch):
        if self.run("git", "status", "--porcelain", "--untracked-files=all"):
            raise InstallError("The checkout has local changes. Preserve them and use a clean checkout; nothing was installed.")
        if self.run("git", "branch", "--show-current") != default_branch:
            raise InstallError(f"Start from the target default branch: {default_branch}")
        existing = [path for path in ALL_FILES if self.root.joinpath(path).exists()]
        previous_mode = None
        if self.args.upgrade:
            previous_mode = installed_mode(existing)
            old_billing = {workflow_billing((self.root / path).read_text(encoding="utf-8"), path)
                           for path in workflow_files(previous_mode) if path.endswith(".md")}
            if len(old_billing) != 1:
                raise InstallError("Existing workflows disagree on Copilot billing.")
            self.args.billing = self.args.billing or old_billing.pop()
            self.args.model = self.args.model or self.run("gh", "variable", "get", "NFV_MODEL", "--repo", self.repository)
            if not re.fullmatch(MODEL, self.args.model):
                raise InstallError("The existing NFV_MODEL is missing or invalid.")
        elif existing:
            raise InstallError("NFV files already exist. Use --upgrade after reviewing existing customizations.")
        mode, files = "post-merge", WORKFLOW_FILES
        self.check_secret_names(self.args.billing)
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
        if previous_mode:
            self.disable_reviewer(previous_mode)
            for name in (*self.selected_workflows(previous_mode), "nfv-source-context.lock.yml"):
                for state in ("queued", "in_progress", "waiting", "pending", "requested"):
                    runs = self.api(f"repos/{self.repository}/actions/workflows/{name}/runs?status={state}&per_page=1")
                    if runs["total_count"]:
                        raise InstallError("Active/queued NFV runs remain. Leave NFV disabled, drain them, then retry --upgrade.")
        branch = f"nfv-{'upgrade' if previous_mode else 'install'}-pr-auditor-{release[:12]}"
        attributes = self.root / ".gitattributes"
        before = attributes.read_bytes() if attributes.exists() else None
        self.run("git", "switch", "-c", branch, base)
        print(f"Installing NFV PR Auditor {release} with model {self.args.model} and {self.args.billing} billing on {branch}.")
        add = ["gh", "aw", "add", f"{PACKAGE}/workflow@{release}"]
        if previous_mode:
            add.append("--force")
            for path in set(workflow_files(previous_mode)) - set(files):
                (self.root / path).unlink()
        print(self.run(*add))
        if mode == "post-merge":
            dispatcher = self.captured_file(PACKAGE, "workflow/pr-auditor-dispatch.yml", release)
            (self.root / DISPATCHER).write_text(
                f"# NFV package: {PACKAGE}/workflow@{release}\n" + dispatcher,
                encoding="utf-8", newline="\n")
        for path in (path for path in files if path.endswith(".md")):
            file = self.root / path
            if not file.is_file():
                raise InstallError(f"Installation did not produce {path}")
            source = file.read_text(encoding="utf-8")
            configured = configure_billing(source, path, self.args.billing)
            if configured != source:
                file.write_text(configured, encoding="utf-8", newline="\n")
        print(self.run("gh", "aw", "compile", "nfv-source-context", REVIEWERS[mode], "--approve"))
        for path in files:
            file = self.root / path
            if not file.is_file():
                raise InstallError(f"Installation did not produce {path}")
            if path.endswith(".md"):
                source = file.read_text(encoding="utf-8")
                self.check_package_source(source, release, path)
                if workflow_billing(source, path) != self.args.billing:
                    raise InstallError(f"Installed workflow billing differs from the selected mode: {path}")
        installed_mode(path for path in ALL_FILES if (self.root / path).exists())
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
            if path not in set(files) | set(existing) and path != ".gitattributes" and not path.startswith(".github/aw/"):
                raise InstallError(f"Unexpected installer change: {path}. Nothing was committed.")
        for path in files:
            if not previous_mode and path not in changed:
                raise InstallError(f"Generated workflow is excluded from the Git changes: {path}. Check ignore rules; nothing was committed.")
        self.run("git", "add", "--", *changed)
        self.run("git", "diff", "--cached", "--check")
        self.run("git", "commit", "-m", f"{'Upgrade' if previous_mode else 'Install'} NFV PR Auditor at {release}")
        variables = [("NFV_MODEL", self.args.model)]
        if not previous_mode:
            variables.append(("NFV_RELEASE_SHA", release))
        for name, value in variables:
            self.run("gh", "variable", "set", name, "--repo", self.repository, "--body", value)
            if self.run("gh", "variable", "get", name, "--repo", self.repository) != value:
                raise InstallError(f"Variable read-back failed: {name}. Do not proceed with installation.")
        self.run("git", "push", "--set-upstream", "origin", branch)
        body = (
            f"Install the NFV prototype at immutable release {release} with model {self.args.model} "
            f"and {self.args.billing} inference billing.\n\n"
            "NFV PR Auditor reviews merged PRs; only this auditor and its helpers are installed.\n\n"
            "Keep this PR draft until the selected reviewer and trigger are confirmed disabled. Keep NFV "
            "disabled through marking ready and merging; leave unrelated CI enabled. "
            "Do not require an NFV check for its own installation PR.\n\n"
            "Review workflows, generated locks, package/action records and any .gitattributes "
            "change. Do not merge without approval. Enable NFV only after the installation "
            "merges into the default branch. Do not start a pilot audit without separate approval."
        )
        if previous_mode:
            body += (f"\n\nAfter merge, with NFV still disabled, update NFV_RELEASE_SHA to {release}; "
                     "the old runtime pin is unchanged while this upgrade is under review. "
                     "Review existing workflow customizations; billing, model and repository budgets are independent of review mode.")
        url = self.run(
            "gh", "pr", "create", "--repo", self.repository, "--base", default_branch,
            "--head", branch, "--draft", "--title", f"{'Upgrade' if previous_mode else 'Install'} NFV PR Auditor", "--body", body,
        )
        print(f"Draft installation PR: {url}")
        created = json.loads(self.run(
            "gh", "pr", "view", url, "--repo", self.repository,
            "--json", "number,isDraft,headRefName,baseRefName",
        ))
        if not created["isDraft"] or created["headRefName"] != branch or created["baseRefName"] != default_branch:
            raise InstallError("Installation PR identity/draft status was not confirmed. Do not mark ready or merge.")
        self.disable_reviewer(mode)
        print(f"NFV is disabled. Review PR {created['number']}, then approve and merge while it remains disabled.")
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
