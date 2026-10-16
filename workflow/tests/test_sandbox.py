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


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.output = self.directory / "evidence"
        self.bundle_path = self.directory / "bundle.json"
        self.bundle = {"identity": {"head_sha": "a" * 40}}
        save(self.bundle_path, self.bundle)
        self.environment = {
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
        return {"errors": [], "findings": 0, "comment": str(self.output / "comment.md")}

    def submit(self, command, **kwargs):
        self.calls.append("submit")
        self.assertEqual(command, ["safeoutputs", "add_comment", "."])
        self.assertEqual(json.loads(kwargs["input"]), {"body": self.body})
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 60)
        return SimpleNamespace(returncode=0, stdout="queued", stderr="")

    def execute(self, extract=None, audit=None, submit=None):
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(sandbox, "propose", side_effect=extract or self.extract), \
                patch.object(sandbox, "audit", side_effect=audit or self.audit), \
                patch.object(sandbox.subprocess, "run", side_effect=submit or self.submit):
            return sandbox.review()

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
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sandbox, "review", side_effect=Abstain("incomplete")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(sandbox.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("NFV sandbox audit failed: incomplete", stderr.getvalue())

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_sdk_shim_preserves_python_output_and_failure_exit_status(self):
        package = self.directory / "nfv_workflow"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "sandbox.py").write_text("print('driver reached Python', flush=True)\nraise SystemExit(7)\n")
        script = Path(__file__).parents[1] / "sandbox_driver.cjs"
        result = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, timeout=15,
            env={**os.environ, "NFV_PYTHON": sys.executable, "PYTHONPATH": str(self.directory.resolve())},
            cwd=self.directory,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("driver reached Python", result.stdout)
