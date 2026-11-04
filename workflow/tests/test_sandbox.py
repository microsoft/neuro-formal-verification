import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from nfv_workflow import sandbox
from nfv_workflow.cli import save
from nfv_workflow.github import Abstain
from nfv_workflow.outcomes import InferenceFailure, OUTCOMES


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.output = self.directory / "evidence"
        self.bundle_path = self.directory / "bundle.json"
        self.bundle = {"identity": {"head_sha": "a" * 40}}
        save(self.bundle_path, self.bundle)
        self.environment = {
            "HOME": str(Path.home()), "USERPROFILE": str(Path.home()),
            "GITHUB_AW": "true", "AWF_REFLECT_ENABLED": "1",
            "COPILOT_PROVIDER_BASE_URL": "http://api-proxy:12345",
            "COPILOT_PROVIDER_TYPE": "openai",
            "COPILOT_SDK_URI": "http://127.0.0.1:3002", "COPILOT_CONNECTION_TOKEN": "per-run-test-token",
            "NFV_BUNDLE": str(self.bundle_path), "NFV_OUT": str(self.output),
            "NFV_MODEL": "test-model",
        }
        self.body = "A deterministic report.\n\nData: $(do-not-execute) & <text> \u03bb.\n"
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.directory)

    async def extract(self, bundle, model, intent_only, output):
        self.calls.append("extract")
        self.assertEqual(bundle, self.bundle)
        self.assertEqual(model, "test-model")
        self.assertTrue(intent_only)
        save(output / "extraction.json", {"completed": True})
        return []

    def audit(self, proposals, limits):
        self.calls.append("audit")
        self.assertEqual(json.loads(Path(proposals).read_text()), [])
        save(self.output / "report.json", {"completed": True})
        (self.output / "comment.md").write_text(self.body, encoding="utf-8", newline="\n")
        return {"errors": [], "findings": 0, "comment": str(self.output / "comment.md"),
                "outcomes": {outcome: 0 for outcome in OUTCOMES}}

    def submit(self, command, **kwargs):
        self.calls.append("submit")
        self.assertEqual(command, ["safeoutputs", "add_comment", "."])
        self.assertEqual(json.loads(kwargs["input"]), {"body": self.body})
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 60)
        return SimpleNamespace(returncode=0, stdout="queued", stderr="")

    def execute(self, extract=None, audit=None, submit=None, main=False):
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(sandbox, "propose", side_effect=extract or self.extract), \
                patch.object(sandbox, "audit", side_effect=audit or self.audit), \
                patch.object(sandbox.subprocess, "run", side_effect=submit or self.submit):
            if main:
                self.stdout, self.stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
                    return sandbox.main()
            return sandbox.review()

    def test_all_completed_property_outcomes_submit_the_unchanged_comment(self):
        for outcome in OUTCOMES:
            def completed(proposals, limits):
                summary = self.audit(proposals, limits)
                save(self.output / "report.json", {"completed": True, "errors": [],
                                                  "properties": [{"status": outcome}]})
                return {**summary, "findings": int(outcome == "refuted"),
                        "outcomes": {name: int(name == outcome) for name in OUTCOMES}}
            with self.subTest(outcome=outcome):
                self.assertEqual(self.execute(audit=completed, main=True), 0)
                status = json.loads((self.output / "review-status.json").read_text())
                self.assertEqual(status["status"], "completed")
                self.assertTrue(status["completed"])
                self.assertTrue(status["analysis_completed"])
                self.assertEqual(status["identity"], self.bundle["identity"])
                self.assertEqual(status["publication"], "submitted")
                self.assertEqual(status["outcomes"][outcome], 1)
                self.assertEqual(set(status["outcomes"]), set(OUTCOMES))
                self.assertEqual(self.calls, ["extract", "audit", "submit"])
                self.assertEqual((self.output / "comment.md").read_text(encoding="utf-8"), self.body)
                self.assertNotIn("diagnostic", status)
                shutil.rmtree(self.output)
                self.calls.clear()

    def test_driver_infrastructure_failures_have_separate_stage_bound_evidence(self):
        async def provider_failure(*args, **kwargs):
            self.calls.append("extract")
            raise InferenceFailure("Provider HTTP 403")
        def incomplete_audit(proposals, limits):
            summary = self.audit(proposals, limits)
            save(self.output / "report.json", {"completed": False, "errors": ["verifier unavailable"]})
            return {**summary, "errors": ["verifier unavailable"]}
        def failed_submission(command, **kwargs):
            self.submit(command, **kwargs)
            return SimpleNamespace(returncode=1, stdout="", stderr="MCP tool failed")
        for stage, options, analysis_completed, publication in (
                ("extraction", {"extract": provider_failure}, False, "not_attempted"),
                ("audit", {"audit": incomplete_audit}, False, "not_attempted"),
                ("publication", {"submit": failed_submission}, True, "unconfirmed")):
            with self.subTest(stage=stage):
                self.assertEqual(self.execute(**options, main=True), 1)
                status = json.loads((self.output / "review-status.json").read_text())
                self.assertEqual(status["status"], "infrastructure_failure")
                self.assertFalse(status["completed"])
                self.assertEqual(status["stage"], stage)
                self.assertEqual(status["identity"], self.bundle["identity"])
                self.assertEqual(status["analysis_completed"], analysis_completed)
                self.assertEqual(status["publication"], publication)
                self.assertTrue(status["diagnostic"])
                self.assertEqual(self.stdout.getvalue(), "")
                self.assertIn('"status": "infrastructure_failure"', self.stderr.getvalue())
                if stage != "publication":
                    self.assertNotIn("submit", self.calls)
                if stage == "extraction":
                    self.assertFalse((self.output / "report.json").exists())
                    self.assertNotIn("outcomes", status)
                else:
                    report = json.loads((self.output / "report.json").read_text())
                    self.assertEqual(report["completed"], analysis_completed)
                shutil.rmtree(self.output)
                self.calls.clear()

    def test_setup_failure_records_no_verdict_or_fabricated_snapshot(self):
        self.environment["COPILOT_SDK_URI"] = ""
        self.assertEqual(self.execute(main=True), 1)
        status = json.loads((self.output / "review-status.json").read_text())
        self.assertEqual(status["status"], "infrastructure_failure")
        self.assertEqual(status["stage"], "setup")
        self.assertNotIn("identity", status)
        self.assertNotIn("outcomes", status)
        self.assertFalse((self.output / "report.json").exists())
        self.assertFalse(self.calls)

    def test_retry_cannot_overwrite_the_first_attempt_status_or_resubmit(self):
        self.assertEqual(self.execute(main=True), 0)
        path = self.output / "review-status.json"
        original = path.read_bytes()
        self.assertEqual(self.execute(main=True), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.calls, ["extract", "audit", "submit"])
        self.assertIn("Could not retain NFV review status artifact", self.stderr.getvalue())

    def test_foreground_extraction_audit_and_verbatim_submission_in_order(self):
        summary = self.execute()
        self.assertEqual(self.calls, ["extract", "audit", "submit"])
        self.assertEqual(summary["publication"], "submitted")
        self.assertEqual(json.loads((self.output / "bundle.json").read_text()), self.bundle)
        self.assertEqual(json.loads((self.output / "publication.json").read_text())["returncode"], 0)
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(self.calls, ["extract", "audit", "submit"])

    def test_refuses_unsandboxed_unresolved_or_credential_bearing_execution(self):
        for key, value in (("GITHUB_AW", "false"), ("AWF_REFLECT_ENABLED", ""),
                           ("COPILOT_PROVIDER_BASE_URL", ""), ("COPILOT_PROVIDER_TYPE", "unknown"),
                           ("NFV_MODEL", ""), ("COPILOT_SDK_URI", ""), ("COPILOT_CONNECTION_TOKEN", ""),
                           ("COPILOT_GITHUB_TOKEN", "credential"),
                           ("GH_TOKEN", "credential"), ("GITHUB_TOKEN", "credential")):
            with self.subTest(key=key):
                with patch.dict(self.environment, {key: value}):
                    with self.assertRaises(Abstain):
                        self.execute()
                self.assertFalse(self.output.exists())
                self.assertFalse(self.calls)

    def test_incomplete_or_failed_extraction_never_starts_audit_or_publication(self):
        async def incomplete(*args, output, **kwargs):
            save(output / "extraction.json", {"completed": False})
            return []
        async def interrupted(*args, **kwargs):
            raise asyncio.CancelledError()
        for extract, error in ((incomplete, Abstain), (interrupted, asyncio.CancelledError)):
            with self.subTest(error=error):
                with patch.dict(self.environment, {"NFV_OUT": str(self.output / error.__name__)}):
                    with self.assertRaises(error):
                        self.execute(extract=extract)
                self.assertFalse(self.calls)

    def test_incomplete_or_tool_failed_audit_never_submits_a_comment(self):
        for completed, errors in ((False, []), (True, ["verifier unavailable"])):
            def invalid(proposals, limits):
                self.audit(proposals, limits)
                save(self.output / "report.json", {"completed": completed})
                return {"errors": errors}
            with self.subTest(completed=completed):
                with self.assertRaises(Abstain):
                    self.execute(audit=invalid)
                self.assertNotIn("submit", self.calls)
                self.assertTrue((self.output / "extraction.json").exists())
                shutil.rmtree(self.output)
                self.calls.clear()

    def test_failed_safe_output_is_retained_and_never_retried(self):
        def failed(command, **kwargs):
            self.submit(command, **kwargs)
            return SimpleNamespace(returncode=1, stdout="", stderr="MCP tool failed")
        with self.assertRaisesRegex(Abstain, "MCP tool failed"):
            self.execute(submit=failed)
        self.assertEqual(self.calls, ["extract", "audit", "submit"])
        saved = json.loads((self.output / "publication.json").read_text())
        self.assertEqual(saved["returncode"], 1)
        self.assertEqual(saved["stderr"], "MCP tool failed")

    def test_empty_report_is_not_submitted(self):
        self.body = "\n"
        with self.assertRaisesRegex(Abstain, "empty report"):
            self.execute()
        self.assertNotIn("submit", self.calls)

    def test_process_failure_is_nonzero_with_an_explicit_diagnostic(self):
        from nfv_workflow.outcomes import InferenceFailure
        stdout, stderr = io.StringIO(), io.StringIO()
        for error in (Abstain("incomplete"), InferenceFailure("Provider HTTP 403"),
                      RuntimeError("SDK transport unavailable")):
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(sandbox, "review", side_effect=error), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(sandbox.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("NFV sandbox audit failed: incomplete", stderr.getvalue())
        self.assertIn("NFV sandbox audit failed: Provider HTTP 403", stderr.getvalue())
        self.assertIn("NFV sandbox audit failed: SDK transport unavailable", stderr.getvalue())

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_sdk_shim_strips_platform_auth_preserving_sidecar_token_output_and_exit_status(self):
        package = self.directory / "nfv_workflow"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "sandbox.py").write_text(
            "import os\n"
            "assert not any(os.environ.get(key) for key in "
            "('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'))\n"
            "assert os.environ['COPILOT_CONNECTION_TOKEN'] == 'per-run-test-token'\n"
            "print('driver reached Python', flush=True)\nraise SystemExit(7)\n")
        script = Path(__file__).parents[1] / "sandbox_driver.cjs"
        result = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, timeout=15,
            env={**os.environ, "NFV_PYTHON": sys.executable, "PYTHONPATH": str(self.directory.resolve()),
                 "COPILOT_GITHUB_TOKEN": "test-platform-auth", "GH_TOKEN": "test-platform-auth",
                 "GITHUB_TOKEN": "test-platform-auth", "COPILOT_CONNECTION_TOKEN": "per-run-test-token"},
            cwd=self.directory,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("driver reached Python", result.stdout)
