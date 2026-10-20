import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch


SOURCE = Path(__file__).parents[1] / "install.py"
SPEC = importlib.util.spec_from_file_location("nfv_installer", SOURCE)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)
RELEASE = "a" * 40


class FakeCLI:
    def __init__(self, root, phase, **case):
        self.root, self.phase, self.case = root, phase, case
        self.calls = []
        self.state = "disabled_manually" if phase == "enable" else "active"
        self.variables = {"NFV_RELEASE_SHA": RELEASE, "NFV_MODEL": "approved-model"}
        self.installed = False
        self.extension_installed = not case.get("missing_extension")

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if kwargs["cwd"] != self.root:
            raise AssertionError("Command escaped the target checkout")
        code, output = self.response(args)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr="")

    def response(self, args):
        program, command, *rest = args
        if program == "git":
            if command == "rev-parse":
                if rest[0] == "--show-toplevel":
                    return 0, str(self.root)
                return 0, ("c" if self.case.get("behind") and rest[0] == "HEAD" else "b") * 40
            if command == "remote":
                wrong = self.case.get("wrong_remote") or self.case.get("wrong_push") and "--push" in rest
                return 0, f"https://github.com/{'upstream' if wrong else 'owner'}/project.git"
            if command == "status":
                return 0, " M personal.txt" if self.case.get("dirty") else ""
            if command == "branch":
                return 0, "feature" if self.case.get("wrong_branch") else "main"
            if command == "ls-files":
                files = list(INSTALL.WORKFLOW_FILES) + [".github/aw/actions-lock.json", ".gitattributes"]
                if self.case.get("ignored_workflow"):
                    files.remove(INSTALL.WORKFLOW_FILES[0])
                if self.case.get("unexpected_file"):
                    files.append("prompts.txt")
                return 0, "\n".join(files) if self.installed else ""
            if command in ("diff", "fetch", "switch", "add", "commit", "push"):
                return 0, ""
        if program != "gh":
            raise AssertionError(f"Unexpected program: {args}")
        if command == "auth":
            return 0, "Authenticated (fixture)"
        if command == "repo":
            return 0, json.dumps({"nameWithOwner": "owner/project", "defaultBranchRef": {"name": "main"}})
        if command == "secret":
            names = [] if self.case.get("missing_secret") else ["COPILOT_GITHUB_TOKEN"]
            if names and not self.case.get("public_nfv") and not self.case.get("missing_release"):
                names.append("NFV_RELEASE_TOKEN")
            return 0, json.dumps([{"name": name} for name in names])
        if command == "extension":
            if rest[0] == "install":
                self.extension_installed = True
                return 0, ""
            return 0, "gh aw\tgithub/gh-aw\tv0.88.7" if self.extension_installed else ""
        if command == "aw":
            if rest[0] == "version":
                return 0, "gh aw version " + ("v0.99.0" if self.case.get("wrong_compiler") else "v0.88.7")
            if rest[0] == "add":
                self.installed = True
                for path in (*INSTALL.WORKFLOW_FILES, ".github/aw/actions-lock.json"):
                    if self.case.get("missing_workflow") and path == INSTALL.WORKFLOW_FILES[0]:
                        continue
                    file = self.root / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    pin = "d" * 40 if self.case.get("wrong_package") else RELEASE
                    file.write_text(f"source: {INSTALL.PACKAGE}/workflow@{pin}\n", encoding="utf-8")
                rule = "* -text" if self.case.get("bad_attributes") else ".github/workflows/*.lock.yml linguist-generated=true"
                attributes = self.root / ".gitattributes"
                before = attributes.read_bytes() if attributes.exists() else b""
                if self.case.get("overwrite_attributes"):
                    before = b""
                attributes.write_bytes(before + (rule + "\n").encode())
                return 0, "Installed (fixture)"
            if rest[0] == "compile":
                return (1, "Compilation rejected") if self.case.get("compile_failure") else (0, "Compiled (fixture)")
        if command == "variable":
            action, name = rest[:2]
            if action == "set":
                self.variables[name] = rest[-1]
                return 0, ""
            return 0, "d" * 40 if self.case.get("wrong_pin") and name == "NFV_RELEASE_SHA" else self.variables[name]
        if command == "workflow":
            if self.case.get("unregistered"):
                return 1, "HTTP 404: Not Found"
            if rest[0] == "disable":
                if not self.case.get("disable_ineffective"):
                    self.state = "disabled_manually"
            elif rest[0] == "enable":
                if not self.case.get("enable_ineffective"):
                    self.state = "active"
            else:
                raise AssertionError(f"Unexpected workflow operation: {args}")
            return 0, ""
        if command == "pr":
            if rest[0] == "create":
                return 0, "https://github.com/owner/project/pull/7"
            return 0, json.dumps({
                "number": 7, "isDraft": not self.case.get("not_draft"),
                "state": "MERGED" if self.phase == "enable" and not self.case.get("unmerged") else "OPEN",
                "baseRefName": "main", "headRefName": "nfv-install-" + RELEASE[:12],
                "mergeCommit": {"oid": "b" * 40}, "url": "https://github.com/owner/project/pull/7",
            })
        if command == "api":
            endpoint = rest[-1]
            if endpoint == f"repos/{INSTALL.PACKAGE}":
                return 0, json.dumps({"private": not self.case.get("public_nfv")})
            if "/actions/variables/NFV_RELEASE_SHA" in endpoint:
                return 0, json.dumps({"value": "d" * 40 if self.case.get("wrong_pin") else RELEASE})
            if "/actions/workflows/" in endpoint:
                return 0, json.dumps({"state": self.state})
            if "/commits/" in endpoint:
                return 0, json.dumps({"sha": RELEASE})
            if "/compare/" in endpoint:
                return 0, json.dumps({"status": "diverged" if self.case.get("detached_merge") else "ahead"})
            if "/contents/" in endpoint:
                if self.case.get("missing_lock") and ".lock.yml" in endpoint:
                    return 1, "HTTP 404: Not Found"
                pin = "d" * 40 if self.case.get("wrong_package") else RELEASE
                return 0, json.dumps({
                    "type": "file", "encoding": "base64",
                    "content": base64.b64encode(f"source: {INSTALL.PACKAGE}/workflow@{pin}\n".encode()).decode(),
                })
        raise AssertionError(f"Unexpected command: {args}")


class InstallerTests(unittest.TestCase):
    def run_case(self, phase="install", **case):
        root = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        if case.get("existing_attributes"):
            (root / ".gitattributes").write_bytes(b"* text=auto\n*.py text eol=lf\n")
        if case.get("existing_install"):
            installed = root / INSTALL.WORKFLOW_FILES[0]
            installed.parent.mkdir(parents=True)
            installed.write_text("Existing installation\n", encoding="utf-8")
        argv = ["--repository", "owner/project"]
        argv += ["--model", "approved-model"] if phase == "install" else [
            "--verify-bootstrap" if phase == "guard" else "--enable-after-merge", "--pull-request", "7",
        ]
        installer = INSTALL.Installer(INSTALL.options(argv))
        installer.root = root
        fake = FakeCLI(root, phase, **case)
        stdout, stderr = io.StringIO(), io.StringIO()
        error = None
        with patch.object(INSTALL.subprocess, "run", side_effect=fake), \
                patch.object(INSTALL.shutil, "which", return_value="/tools/fixture"), \
                patch.dict(os.environ, {"GH_HOST": "github.com"}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                installer.execute()
            except INSTALL.InstallError as failure:
                error = str(failure)
        return error, fake.calls, stdout.getvalue()

    def test_install_pins_compiles_and_creates_only_a_guarded_draft(self):
        error, calls, _ = self.run_case()
        self.assertIsNone(error)
        self.assertIn(["gh", "aw", "add", f"{INSTALL.PACKAGE}/workflow@{RELEASE}"], calls)
        self.assertIn(["gh", "aw", "compile", "nfv-source-context", "nfv-pr-review", "--approve"], calls)
        self.assertEqual(sum("/commits/dev" in " ".join(call) for call in calls), 1)
        self.assertIn("--draft", next(call for call in calls if call[:3] == ["gh", "pr", "create"]))
        for name, value in (("NFV_RELEASE_SHA", RELEASE), ("NFV_MODEL", "approved-model")):
            self.assertIn(["gh", "variable", "set", name, "--repo", "owner/project", "--body", value], calls)
        self.assertIn(["gh", "workflow", "disable", INSTALL.WORKFLOW, "--repo", "owner/project"], calls)
        self.assertFalse(any(call[:3] in (["gh", "workflow", "enable"], ["gh", "workflow", "run"],
                                         ["gh", "pr", "merge"]) for call in calls))
        staged = next(call for call in calls if call[:2] == ["git", "add"])
        self.assertEqual(set(staged[3:]), set(INSTALL.WORKFLOW_FILES) |
                         {".github/aw/actions-lock.json", ".gitattributes"})

    def test_invalid_checkout_or_missing_secret_does_not_mutate_repository(self):
        reasons = {
            "dirty": "checkout has local changes", "wrong_remote": "origin fetch and push URLs",
            "wrong_push": "origin fetch and push URLs", "wrong_branch": "Start from the target default branch",
            "missing_secret": "Missing COPILOT_GITHUB_TOKEN", "missing_release": "Missing NFV_RELEASE_TOKEN",
            "behind": "local default branch differs", "wrong_compiler": "Use gh-aw v0.88.7",
            "existing_install": "NFV files already exist",
        }
        for option, reason in reasons.items():
            with self.subTest(option=option):
                error, calls, _ = self.run_case(**{option: True})
                self.assertIn(reason, error)
                self.assertFalse(any(call[0] == "git" and call[1] in ("switch", "add", "commit", "push")
                                     or call[:3] == ["gh", "variable", "set"] for call in calls))

    def test_compiler_is_installed_only_when_absent(self):
        error, calls, _ = self.run_case(missing_extension=True)
        self.assertIsNone(error)
        self.assertIn(["gh", "extension", "install", "github/gh-aw", "--pin", "v0.88.7"], calls)

    def test_public_nfv_needs_only_copilot_secret(self):
        error, _, _ = self.run_case(public_nfv=True)
        self.assertIsNone(error)

    def test_bad_generated_files_or_compile_failure_are_not_committed(self):
        reasons = {
            "unexpected_file": "Unexpected installer change", "bad_attributes": "Unexpected .gitattributes rule",
            "compile_failure": "Compilation rejected", "wrong_package": "Installed package and runtime pin differ",
            "ignored_workflow": "Generated workflow is excluded from the Git changes",
            "missing_workflow": "Installation did not produce",
        }
        for option, reason in reasons.items():
            with self.subTest(option=option):
                error, calls, _ = self.run_case(**{option: True})
                self.assertIn(reason, error)
                self.assertFalse(any(call[0] == "git" and call[1] in ("commit", "push") for call in calls))

    def test_unregistered_or_active_reviewer_leaves_draft_and_reports_block(self):
        for option in ("unregistered", "disable_ineffective"):
            with self.subTest(option=option):
                error, calls, _ = self.run_case(**{option: True})
                self.assertIn("Keep the installation PR draft", error)
                self.assertIn("--draft", next(call for call in calls if call[:3] == ["gh", "pr", "create"]))
                self.assertFalse(any(call[:3] == ["gh", "workflow", "enable"] for call in calls))

    def test_bootstrap_guard_can_be_retried_without_reinstalling(self):
        error, calls, _ = self.run_case(phase="guard")
        self.assertIsNone(error)
        self.assertTrue(any(call[:3] == ["gh", "workflow", "disable"] for call in calls))
        self.assertFalse(any(call[0] == "gh" and call[1] in ("aw", "variable") for call in calls))

    def test_enable_requires_merge_and_matching_package_and_pin(self):
        reasons = {
            "unmerged": "only after the installation PR is merged", "wrong_pin": "runtime pin does not match",
            "wrong_package": "Installed package and runtime pin differ", "detached_merge": "merge is not retained",
            "missing_secret": "Missing COPILOT_GITHUB_TOKEN", "missing_lock": "HTTP 404",
        }
        for option, reason in reasons.items():
            with self.subTest(option=option):
                error, calls, _ = self.run_case(phase="enable", **{option: True})
                self.assertIn(reason, error)
                self.assertFalse(any(call[:3] == ["gh", "workflow", "enable"] for call in calls))

    def test_enable_after_merge_never_dispatches_or_changes_checkout(self):
        error, calls, _ = self.run_case(phase="enable")
        self.assertIsNone(error)
        self.assertIn(["gh", "workflow", "enable", INSTALL.WORKFLOW, "--repo", "owner/project"], calls)
        self.assertFalse(any(call[0] == "git" and call[1] in ("fetch", "switch", "commit", "push")
                             or call[:3] in (["gh", "workflow", "run"], ["gh", "variable", "set"])
                             for call in calls))

    def test_enable_reports_failed_state_readback(self):
        error, _, _ = self.run_case(phase="enable", enable_ineffective=True)
        self.assertIn("NFV enablement was not confirmed", error)

    def test_variable_readback_failure_does_not_publish_or_create_pr(self):
        error, calls, _ = self.run_case(wrong_pin=True)
        self.assertIn("Variable read-back failed: NFV_RELEASE_SHA", error)
        self.assertFalse(any(call[:2] == ["git", "push"] or call[:3] == ["gh", "pr", "create"]
                             for call in calls))

    def test_existing_gitattributes_rules_are_preserved(self):
        error, _, _ = self.run_case(existing_attributes=True)
        self.assertIsNone(error)
        error, calls, _ = self.run_case(existing_attributes=True, overwrite_attributes=True)
        self.assertIn("Unexpected .gitattributes edits", error)
        self.assertFalse(any(call[:2] == ["git", "commit"] for call in calls))

    def test_non_draft_bootstrap_pr_is_rejected(self):
        error, calls, _ = self.run_case(phase="guard", not_draft=True)
        self.assertIn("requires an open draft installation PR", error)
        self.assertFalse(any(call[:3] == ["gh", "workflow", "enable"] for call in calls))

    def test_invalid_cli_combinations_fail_before_commands(self):
        for argv in (
            ["--repository", "owner/project"],
            ["--repository", "owner/project", "--enable-after-merge"],
            ["--repository", "owner/project", "--verify-bootstrap", "--pull-request", "0"],
            ["--repository", "https://github.com/owner/project", "--model", "approved"],
            ["--repository", "owner/project", "--model", "not a model"],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                INSTALL.options(argv)

    def test_native_runner_preserves_arguments_and_surfaces_errors(self):
        installer = INSTALL.Installer(INSTALL.options(["--repository", "owner/project", "--model", "approved"]))
        values = ["argument with spaces", "$(not a command)", "a'b", 'a"b']
        result = installer.run(sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values)
        self.assertEqual(json.loads(result), values)
        with self.assertRaisesRegex(INSTALL.InstallError, "explicit failure"):
            installer.run(sys.executable, "-c", "import sys; sys.stderr.write('explicit failure'); sys.exit(3)")


if __name__ == "__main__":
    unittest.main()
