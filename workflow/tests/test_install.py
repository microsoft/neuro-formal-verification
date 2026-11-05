# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

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
        self.mode = case.get("selected_mode", "post-merge")
        self.calls = []
        self.state = "disabled_manually" if phase == "enable" else "active"
        self.variables = {"NFV_RELEASE_SHA": RELEASE, "NFV_MODEL": "approved-model"}
        if phase == "upgrade":
            self.variables["NFV_RELEASE_SHA"] = "e" * 40
        self.installed = False
        self.extension_installed = not case.get("missing_extension")

    def source(self, path, pin):
        if path.endswith("nfv-pr-review.md"):
            source = "---\npermissions:\n  copilot-requests: none\njobs:\n  repository_context:\n    permissions:\n      copilot-requests: none\n---\nLegacy reviewer fixture\n"
        else:
            source = (SOURCE.parents[1] / path).read_text(encoding="utf-8")
        if self.case.get("organization_billing"):
            source = INSTALL.configure_billing(source, path, "organization")
        if self.case.get("mismatched_billing") and path.endswith(INSTALL.REVIEWERS[self.mode] + ".md"):
            source = INSTALL.configure_billing(source, path, "organization")
        if self.case.get("invalid_billing") and path.endswith(INSTALL.REVIEWERS[self.mode] + ".md"):
            source = source.replace("      copilot-requests: none", "      copilot-requests: write")
        if self.case.get("legacy_billing"):
            source = "\n".join(line for line in source.split("\n") if "copilot-requests:" not in line)
        package = "workflow/pr" if self.mode == "pr" and not self.case.get("legacy_package") else "workflow"
        return source.replace("---\n", f"---\nsource: {INSTALL.PACKAGE}/{package}@{pin}\n", 1)

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
                files = list(INSTALL.workflow_files(self.mode)) + [".github/aw/actions-lock.json", ".gitattributes"]
                if self.case.get("ignored_workflow"):
                    files.remove(INSTALL.WORKFLOW_FILES[0])
                if self.case.get("unexpected_file"):
                    files.append("prompts.txt")
                return 0, "\n".join(files) if self.installed else ""
            if command == "diff" and self.phase == "upgrade" and self.installed and "--name-only" in rest:
                return 0, "\n".join(set(INSTALL.workflow_files(self.case.get("previous_mode", "pr")))
                                     - set(INSTALL.workflow_files(self.mode)))
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
            if not self.case.get("public_nfv") and not self.case.get("missing_release"):
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
                for path in (*INSTALL.workflow_files(self.mode), ".github/aw/actions-lock.json"):
                    if path == INSTALL.DISPATCHER:
                        continue
                    if self.case.get("missing_workflow") and path == INSTALL.WORKFLOW_FILES[0]:
                        continue
                    file = self.root / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    pin = "d" * 40 if self.case.get("wrong_package") else RELEASE
                    text = self.source(path, pin) if path.endswith(".md") else "Compiled fixture\n"
                    file.write_text(text, encoding="utf-8")
                rule = "* -text" if self.case.get("bad_attributes") else ".github/workflows/*.lock.yml linguist-generated=true"
                attributes = self.root / ".gitattributes"
                before = attributes.read_bytes() if attributes.exists() else b""
                if self.case.get("overwrite_attributes"):
                    before = b""
                attributes.write_bytes(before + (rule + "\n").encode())
                return 0, "Installed (fixture)"
            if rest[0] == "compile":
                if self.case.get("expected_billing"):
                    for path in (path for path in INSTALL.workflow_files(self.mode) if path.endswith(".md")):
                        source = (self.root / path).read_text(encoding="utf-8")
                        if INSTALL.workflow_billing(source, path) != self.case["expected_billing"]:
                            raise AssertionError("Compilation used the wrong billing mode")
                return (1, "Compilation rejected") if self.case.get("compile_failure") else (0, "Compiled (fixture)")
        if command == "variable":
            action, name = rest[:2]
            if action == "set":
                self.variables[name] = rest[-1]
                return 0, ""
            return 0, "d" * 40 if self.case.get("wrong_pin") and name == "NFV_RELEASE_SHA" else self.variables[name]
        if command == "label":
            return 0, ""
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
            branch = f"nfv-{'upgrade' if self.phase == 'upgrade' else 'install'}-{self.mode}-{RELEASE[:12]}"
            if self.case.get("legacy_branch"):
                branch = "nfv-install-" + RELEASE[:12]
            return 0, json.dumps({
                "number": 7, "isDraft": not self.case.get("not_draft"),
                "state": "MERGED" if self.phase == "enable" and not self.case.get("unmerged") else "OPEN",
                "baseRefName": "main",
                "headRefName": branch,
                "mergeCommit": {"oid": "b" * 40}, "url": "https://github.com/owner/project/pull/7",
            })
        if command == "api":
            endpoint = rest[-1]
            if endpoint == f"repos/{INSTALL.PACKAGE}":
                return 0, json.dumps({"private": not self.case.get("public_nfv")})
            if endpoint == "repos/owner/project":
                return 0, json.dumps({"owner": {"type": "User" if self.case.get("personal_repo") else "Organization"}})
            if "/actions/variables/NFV_RELEASE_SHA" in endpoint:
                return 0, json.dumps({"value": "d" * 40 if self.case.get("wrong_pin") else RELEASE})
            if "/actions/workflows?" in endpoint:
                rows = [{"path": ".github/workflows/" + name, "state": self.state}
                        for name in INSTALL.Installer.selected_workflows(self, self.mode)]
                if self.case.get("other_active"):
                    other = "pr" if self.mode == "post-merge" else "post-merge"
                    rows.extend({"path": ".github/workflows/" + name, "state": "active"}
                                for name in INSTALL.Installer.selected_workflows(self, other))
                return 0, json.dumps({"workflows": rows, "total_count": len(rows)})
            if "/runs?status=" in endpoint:
                return 0, json.dumps({"workflow_runs": [], "total_count": int(self.case.get("active_runs", False))})
            if "/actions/workflows/" in endpoint:
                return 0, json.dumps({"state": self.state})
            if "/commits/" in endpoint:
                return 0, json.dumps({"sha": RELEASE})
            if "/compare/" in endpoint:
                return 0, json.dumps({"status": "diverged" if self.case.get("detached_merge") else "ahead"})
            if "/contents/" in endpoint:
                if "/contents/.github/workflows?" in endpoint:
                    paths = INSTALL.ALL_FILES if self.case.get("both_modes") else INSTALL.workflow_files(self.mode)
                    return 0, json.dumps([{"name": Path(path).name} for path in paths])
                if self.case.get("missing_lock") and ".lock.yml" in endpoint:
                    return 1, "HTTP 404: Not Found"
                pin = "d" * 40 if self.case.get("wrong_package") else RELEASE
                path = endpoint.split("/contents/", 1)[1].split("?", 1)[0]
                if path == "workflow/post-merge-dispatch.yml":
                    text = (SOURCE.parent / "post-merge-dispatch.yml").read_text(encoding="utf-8")
                elif path == INSTALL.DISPATCHER:
                    text = f"# NFV package: {INSTALL.PACKAGE}/workflow@{pin}\nNative workflow fixture\n"
                else:
                    text = self.source(path, pin) if path.endswith(".md") else "Compiled fixture\n"
                return 0, json.dumps({
                    "type": "file", "encoding": "base64",
                    "content": base64.b64encode(text.encode()).decode(),
                })
        raise AssertionError(f"Unexpected command: {args}")


class InstallerTests(unittest.TestCase):
    def run_case(self, phase="install", **case):
        trusted_ref = case.pop("trusted_ref", None)
        model = case.pop("model", "approved-model")
        billing = case.pop("billing", None)
        mode = case.pop("review_mode", None)
        case["selected_mode"] = mode or "post-merge"
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
        if phase in ("install", "upgrade"):
            if model is not None:
                argv += ["--model", model]
            if phase == "upgrade":
                argv += ["--upgrade"]
        else:
            argv += ["--verify-bootstrap" if phase == "guard" else "--enable-after-merge",
                     "--pull-request", "7"]
        if trusted_ref is not None:
            argv += ["--ref", trusted_ref]
        if billing is not None:
            argv += ["--billing", billing]
        installer = INSTALL.Installer(INSTALL.options(argv))
        installer.root = root
        fake = FakeCLI(root, phase, **case)
        if phase == "upgrade":
            for path in INSTALL.workflow_files(case.get("previous_mode", "pr")):
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                text = fake.source(path, "e" * 40) if path.endswith(".md") else "Installed fixture\n"
                file.write_text(text, encoding="utf-8")
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
        self.assertIn(["gh", "aw", "compile", "nfv-source-context", "nfv-post-merge-review", "--approve"], calls)
        self.assertEqual(sum("/commits/HEAD" in " ".join(call) for call in calls), 1)
        self.assertIn("--draft", next(call for call in calls if call[:3] == ["gh", "pr", "create"]))
        for name, value in (("NFV_RELEASE_SHA", RELEASE), ("NFV_MODEL", "approved-model")):
            self.assertIn(["gh", "variable", "set", name, "--repo", "owner/project", "--body", value], calls)
        self.assertIn(["gh", "workflow", "disable", INSTALL.WORKFLOW, "--repo", "owner/project"], calls)
        self.assertFalse(any(call[:3] in (["gh", "workflow", "enable"], ["gh", "workflow", "run"],
                                         ["gh", "pr", "merge"]) for call in calls))
        staged = next(call for call in calls if call[:2] == ["git", "add"])
        self.assertEqual(set(staged[3:]), set(INSTALL.WORKFLOW_FILES) |
                         {".github/aw/actions-lock.json", ".gitattributes"})

    def test_omitted_model_installs_and_reads_back_default(self):
        error, calls, _ = self.run_case(model=None)
        self.assertIsNone(error)
        self.assertIn(["gh", "variable", "set", "NFV_MODEL", "--repo", "owner/project",
                       "--body", "gpt-5.6-sol"], calls)
        self.assertIn(["gh", "variable", "get", "NFV_MODEL", "--repo", "owner/project"], calls)

    def test_explicit_organization_billing_needs_no_copilot_pat(self):
        error, calls, output = self.run_case(
            billing="organization", missing_secret=True, expected_billing="organization")
        self.assertIsNone(error)
        self.assertIn("organization billing", output)
        self.assertIn(["gh", "aw", "compile", "nfv-source-context", "nfv-post-merge-review", "--approve"], calls)

    def test_organization_billing_still_requires_private_release_access_and_org_owner(self):
        for case, reason in (({"missing_release": True}, "Missing NFV_RELEASE_TOKEN"),
                             ({"personal_repo": True}, "organization-owned")):
            with self.subTest(case=case):
                error, calls, _ = self.run_case(billing="organization", missing_secret=True, **case)
                self.assertIn(reason, error)
                self.assertFalse(any(call[:2] == ["git", "switch"] for call in calls))

    def test_public_organization_installation_needs_no_secrets(self):
        error, _, _ = self.run_case(
            billing="organization", missing_secret=True, public_nfv=True, expected_billing="organization")
        self.assertIsNone(error)

    def test_legacy_releases_keep_pat_billing_but_cannot_select_organization(self):
        error, _, _ = self.run_case(legacy_billing=True)
        self.assertIsNone(error)
        error, _, _ = self.run_case(phase="enable", legacy_billing=True)
        self.assertIsNone(error)
        error, calls, _ = self.run_case(billing="organization", legacy_billing=True)
        self.assertIn("does not support selecting organization billing", error)
        self.assertFalse(any(call[:2] == ["git", "commit"] for call in calls))

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

    def test_explicit_trusted_revision_overrides_default_branch(self):
        for ref, encoded in (("release/candidate", "release%2Fcandidate"), ("b" * 40, "b" * 40)):
            with self.subTest(ref=ref):
                error, calls, _ = self.run_case(trusted_ref=ref)
                self.assertIsNone(error)
                resolutions = [call[-1] for call in calls if call[:2] == ["gh", "api"]
                               and f"repos/{INSTALL.PACKAGE}/commits/" in call[-1]]
                self.assertEqual([f"repos/{INSTALL.PACKAGE}/commits/{encoded}"], resolutions)
                self.assertIn(["gh", "aw", "add", f"{INSTALL.PACKAGE}/workflow@{RELEASE}"], calls)

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

    def test_activation_reads_organization_billing_from_merged_workflows_without_pat(self):
        error, calls, output = self.run_case(phase="enable", organization_billing=True, missing_secret=True)
        self.assertIsNone(error)
        self.assertIn("organization billing", output)
        self.assertIn(["gh", "workflow", "enable", INSTALL.WORKFLOW, "--repo", "owner/project"], calls)

    def test_activation_rejects_mixed_billing_without_falling_back_to_pat(self):
        for case, reason in (({"mismatched_billing": True}, "disagree on Copilot billing"),
                             ({"invalid_billing": True}, "inconsistent Copilot billing"),
                             ({"organization_billing": True, "missing_release": True}, "Missing NFV_RELEASE_TOKEN"),
                             ({"organization_billing": True, "personal_repo": True}, "organization-owned")):
            with self.subTest(case=case):
                error, calls, _ = self.run_case(phase="enable", **case)
                self.assertIn(reason, error)
                self.assertFalse(any(call[:3] == ["gh", "workflow", "enable"] for call in calls))

    def test_enable_reports_failed_state_readback(self):
        error, _, _ = self.run_case(phase="enable", enable_ineffective=True)
        self.assertIn("NFV enablement was not confirmed", error)

    def test_legacy_pr_installation_must_upgrade_before_activation(self):
        error, calls, _ = self.run_case(
            phase="enable", review_mode="pr", legacy_package=True, legacy_branch=True)
        self.assertIn("Upgrade to post-merge", error)
        self.assertFalse(any(call[:3] == ["gh", "workflow", "enable"] for call in calls))
    def test_activation_refuses_both_mode_files_or_an_active_unselected_reviewer(self):
        for case in ("both_modes", "other_active"):
            with self.subTest(case=case):
                error, calls, _ = self.run_case(phase="enable", **{case: True})
                self.assertIn("NFV", error)
                self.assertFalse(any(call[:3] == ["gh", "workflow", "enable"] for call in calls))

    def test_upgrade_switches_mode_only_after_pause_and_drain_and_preserves_runtime_pin(self):
        error, calls, _ = self.run_case(
            phase="upgrade", model=None, organization_billing=True, expected_billing="organization")
        self.assertIsNone(error)
        paused = ["gh", "workflow", "disable", "nfv-pr-review.lock.yml", "--repo", "owner/project"]
        replace = ["gh", "aw", "add", f"{INSTALL.PACKAGE}/workflow@{RELEASE}", "--force"]
        self.assertLess(calls.index(paused), calls.index(replace))
        self.assertTrue(any("/runs?status=in_progress" in call[-1] for call in calls))
        self.assertFalse(any(call[:4] == ["gh", "variable", "set", "NFV_RELEASE_SHA"] for call in calls))
        staged = next(call for call in calls if call[:2] == ["git", "add"])
        self.assertIn(".github/workflows/nfv-pr-review.md", staged)
        self.assertIn(INSTALL.DISPATCHER, staged)

    def test_upgrade_blocks_while_runs_remain_without_replacing_files_or_pins(self):
        error, calls, _ = self.run_case(phase="upgrade", active_runs=True)
        self.assertIn("Active/queued NFV runs remain", error)
        self.assertFalse(any(call[:2] in (["git", "switch"], ["git", "commit"])
                             or call[:3] == ["gh", "aw", "add"]
                             or call[:3] == ["gh", "variable", "set"] for call in calls))

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
            ["--repository", "owner/project", "--enable-after-merge"],
            ["--repository", "owner/project", "--verify-bootstrap", "--pull-request", "0"],
            ["--repository", "https://github.com/owner/project", "--model", "approved"],
            ["--repository", "owner/project", "--model", "not a model"],
            ["--repository", "owner/project", "--model", ""],
            ["--repository", "owner/project", "--enable-after-merge", "--pull-request", "7",
             "--model", "gpt-5.6-sol"],
            ["--repository", "owner/project", "--enable-after-merge", "--pull-request", "7",
             "--billing", "organization"],
            ["--repository", "owner/project", "--billing", "automatic"],
            ["--repository", "owner/project", "--review-mode", "pr"],
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
