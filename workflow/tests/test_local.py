# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from contextlib import redirect_stdout
from dataclasses import asdict
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from nfv_workflow import benchmark, cli, local
from nfv_workflow.general import Limits, run
from nfv_workflow.github import Abstain
from nfv_workflow.publish import local_comment, render
from test_benchmark import ProofVerifier
from test_general import Chat, fixture

ROOT = Path(__file__).parents[2]


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)

    def tearDown(self):
        def remove_readonly(function, path, info):
            error = info[1]
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.directory, onerror=remove_readonly)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.directory), *args],
                                       stderr=subprocess.STDOUT).decode().strip()

    def committed_change(self):
        self.git("init", "--quiet")
        self.git("config", "user.name", "NFV fixture")
        self.git("config", "user.email", "nfv-fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        (self.directory / ".gitattributes").write_text("*.py diff=nfv\n", encoding="utf-8")
        (self.directory / "subject.py").write_text("def f(x):\n    return x\n", encoding="utf-8", newline="\n")
        (self.directory / "helper.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Base fixture")
        base = self.git("rev-parse", "HEAD")
        (self.directory / "subject.py").write_text(
            "raise RuntimeError('subject code must not execute')\n"
            "def f(x):\n    return helper(x) + 1\n", encoding="utf-8", newline="\n")
        self.git("add", "subject.py")
        self.git("commit", "--quiet", "-m", "Change fixture")
        head = self.git("rev-parse", "HEAD")
        return base, head

    def task_directory(self, kind="review", status="running"):
        directory = self.directory / uuid.uuid4().hex
        directory.mkdir()
        config = {"schema": 1, "kind": kind, "model": "test-model",
                  "limits": asdict(Limits()), "runtime": "native", "timeout_seconds": 60,
                  "simplify_refutations": False, "check_fixes": False}
        local.save(directory / "run.json", config)
        local.state(directory, status, "starting")
        return directory, config

    def test_capture_freezes_commits_and_ignores_worktree_and_diff_drivers(self):
        base, head = self.committed_change()
        marker = self.directory / "executed"
        script = self.directory / "diff-driver.py"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
        driver = f'"{sys.executable}" "{script}"'
        self.git("config", "diff.nfv.command", driver)
        self.git("config", "diff.nfv.textconv", driver)
        committed = (self.directory / "subject.py").read_text()
        (self.directory / "subject.py").write_text("invalid uncommitted source", encoding="utf-8")
        data, _ = local.capture(self.directory, base, head, "The result equals x.")
        self.assertEqual(data["identity"]["source_kind"], "local-change")
        self.assertEqual(data["identity"]["source_sha"], head)
        self.assertEqual(data["sources"]["subject.py"]["text"], committed)
        self.assertEqual(data["changed"]["subject.py"], [1, 3])
        self.assertIn("helper.py", data["sources"])
        self.assertFalse(marker.exists())
        (self.directory / "subject.py").write_text("another uncommitted change", encoding="utf-8")
        self.assertEqual(local.capture(self.directory, base, head, "The result equals x.")[0], data)

    def test_capture_rejects_empty_changes_options_and_intent_without_launching(self):
        base, head = self.committed_change()
        for low, high, intent in ((head, head, "intent"), ("--help", head, "intent"),
                                  (base, head, ""), (base, head, "x" * 12001)):
            with self.subTest(low=low, intent=len(intent)), self.assertRaises(Abstain):
                local.capture(self.directory, low, high, intent)

    def test_local_report_uses_native_acceptance_and_keeps_publication_blocked(self):
        data, claim, translation = fixture()
        data["identity"]["source_kind"] = "local-change"
        report = asyncio.run(run(data, [claim], self.directory / "evidence",
                                 Chat([translation]), verifier=ProofVerifier()))
        body = local_comment(report, data)
        self.assertIn("**Input on which it fails:**", body)
        self.assertIn("**Sketch of the failure:**", body)
        self.assertIn("<details><summary>Details</summary>", body)
        self.assertIn("local review", body)
        self.assertNotIn("/blob/", body)
        self.assertNotIn("in PR #", body)
        self.assertNotIn("gh-aw-workflow-id:", body)
        with self.assertRaises(Abstain):
            render(report, data, "https://github.com/local/synthetic/actions/runs/1")
        for changed in (dict(report, completed=False), dict(report, errors=["provider failed"])):
            with self.assertRaises(Abstain):
                local_comment(changed, data)
        tampered = copy.deepcopy(report)
        tampered["findings"][0]["dafny"] += "// tampered\n"
        with self.assertRaises(Abstain):
            local_comment(tampered, data)

    def test_start_requires_authorization_before_capture_or_launch(self):
        with patch.object(local, "capture") as capture, patch.object(local, "launch") as launch, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["local", "start", "--model", "test-model"]), 1)
        self.assertIn("authorize-inference", output.getvalue())
        capture.assert_not_called()
        launch.assert_not_called()

    def test_review_worker_reuses_extraction_and_audit_and_preserves_input(self):
        directory, config = self.task_directory()
        data, claim, _ = fixture()
        data["identity"]["source_kind"] = "local-change"
        input_path = directory / "inputs" / "bundle.json"
        local.save(input_path, data)
        original = input_path.read_bytes()
        config["bundle_sha256"] = hashlib.sha256(original).hexdigest()
        with patch.object(local, "preflight"), patch.object(cli, "propose", new_callable=AsyncMock,
                                                           return_value=[claim]) as extract, \
                patch.object(cli, "audit") as audit, patch.dict(os.environ, {}, clear=False):
            local.execute(directory, config)
        self.assertEqual(input_path.read_bytes(), original)
        self.assertEqual(local.load(directory / "evidence" / "proposals.json"), [claim])
        self.assertTrue(extract.call_args.kwargs["intent_only"])
        self.assertTrue(audit.call_args.kwargs["local"])
        input_path.write_text("{}", encoding="utf-8")
        with patch.object(local, "preflight"), patch.object(cli, "propose") as extract, \
                self.assertRaisesRegex(Abstain, "input changed"):
            local.execute(directory, config)
        extract.assert_not_called()

    def test_worker_failure_does_not_produce_success_or_final_report(self):
        directory, _ = self.task_directory()
        with patch.object(local, "execute", side_effect=RuntimeError("provider unavailable")):
            self.assertEqual(local.worker(directory), 1)
        status = local.status(directory)
        self.assertEqual(status["status"], "failed")
        self.assertIn("provider unavailable", status["diagnostic"])
        self.assertFalse((directory / "evidence" / "comment.md").exists())
        with self.assertRaisesRegex(Abstain, "failed"):
            local.result(directory)

    def test_native_lifecycle_survives_parent_and_cancels_only_owned_process(self):
        directory, _ = self.task_directory()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        psutil = local.process_module()
        created = psutil.Process(process.pid).create_time()
        local.save(directory / "runtime.json", {"kind": "native", "pid": process.pid, "created_at": created})
        try:
            self.assertEqual(local.status(directory)["status"], "running")
            self.assertIsNone(local.owned_process({"pid": process.pid, "created_at": created - 1}))
            self.assertTrue(psutil.Process(process.pid).is_running())
            self.assertEqual(local.cancel(directory)["status"], "cancelled")
            process.wait(timeout=10)
            self.assertEqual(local.cancel(directory)["status"], "cancelled")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

    def test_unexpected_native_exit_is_failed_not_completed(self):
        directory, _ = self.task_directory()
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL)
        process.wait(timeout=10)
        local.save(directory / "runtime.json", {"kind": "native", "pid": process.pid, "created_at": -1})
        self.assertEqual(local.status(directory)["status"], "failed")
        with self.assertRaisesRegex(Abstain, "failed"):
            local.result(directory)

    def test_native_launcher_runs_worker_outside_subject_import_path(self):
        directory, config = self.task_directory(kind="unsupported")
        local.launch(directory, config)
        runtime = local.load(directory / "runtime.json")
        try:
            deadline = time.monotonic() + 20
            while local.status(directory)["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(local.status(directory)["status"], "failed")
            self.assertTrue((directory / "worker.log").is_file())
        finally:
            process = local.owned_process(runtime)
            if process is not None:
                local.stop_process(process)

    def test_wall_clock_limit_stops_worker_without_a_final_report(self):
        directory, config = self.task_directory()
        config["timeout_seconds"] = 1
        local.save(directory / "run.json", config)
        script = ("import sys, time; from pathlib import Path; from nfv_workflow import local; "
                  "local.execute = lambda *args: time.sleep(30); "
                  "sys.exit(local.worker(Path(sys.argv[1])))")
        process = subprocess.Popen([sys.executable, "-c", script, str(directory)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(process.wait(timeout=15), 124)
            self.assertEqual(local.status(directory)["phase"], "timeout")
            self.assertFalse((directory / "evidence" / "comment.md").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def test_result_requires_complete_report_and_does_not_restart_inference(self):
        directory, _ = self.task_directory(status="completed")
        local.save(directory / "evidence" / "report.json", {"completed": True, "errors": []})
        target = directory / "evidence" / "comment.md"
        target.write_text("generated report", encoding="utf-8")
        with patch.object(local, "execute") as execute:
            self.assertEqual(local.result(directory)["report"], str(target))
        execute.assert_not_called()
        local.save(directory / "evidence" / "report.json", {"completed": False, "errors": []})
        with self.assertRaisesRegex(Abstain, "incomplete"):
            local.result(directory)

    def test_docker_launch_freezes_image_and_exposes_only_runtime_secret_names(self):
        directory, config = self.task_directory()
        config.update(runtime="docker", image="sha256:" + "a" * 64)
        (directory / "inputs").mkdir()
        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "secret-value", "GH_TOKEN": "api-secret"}), \
                patch.object(local, "command", return_value=b"container-id\n") as command:
            local.launch(directory, config)
        args = command.call_args.args[0]
        self.assertIn("COPILOT_GITHUB_TOKEN", args)
        self.assertIn("GH_TOKEN", args)
        self.assertNotIn("secret-value", " ".join(args))
        self.assertNotIn("api-secret", " ".join(args))
        self.assertIn(config["image"], args)
        self.assertTrue(any("target=/run/nfv/inputs,readonly" in arg for arg in args))
        self.assertTrue(any("target=/run/nfv/run.json,readonly" in arg for arg in args))
        self.assertEqual(local.load(directory / "runtime.json")["container"], "container-id")

    def test_failed_runtime_record_stops_the_launched_container(self):
        directory, config = self.task_directory()
        config.update(runtime="docker", image="test-image")
        (directory / "inputs").mkdir()
        with patch.object(local, "save", side_effect=OSError("disk full")), \
                patch.object(local, "command", return_value=b"container-id\n") as command, \
                self.assertRaisesRegex(OSError, "disk full"):
            local.launch(directory, config)
        self.assertEqual(command.call_args.args[0], ["docker", "stop", "--time", "30", "container-id"])

    def test_benchmark_preparation_is_label_blinded_and_worker_uses_shared_loop(self):
        directory, config = self.task_directory(kind="benchmark")
        config["limits"]["minimize_assumptions"] = False
        info = benchmark.prepare(ROOT / "paper" / "data" / "nl2post_safe.json", directory / "prepared",
                                 "a" * 40, config["model"], "local/repository", "123", "1",
                                 Limits(**config["limits"]), count=5, execution="local")
        payload = local.load(directory / "prepared" / "shards" / "0.json")
        self.assertEqual(info["execution"], "local")
        for case in payload["cases"]:
            self.assertEqual(set(case), {"id", "bundle", "properties"})
            self.assertNotIn('"label":', json.dumps(case))
        with patch.object(local, "preflight"), patch.object(benchmark, "run_cases") as loop, \
                patch.object(benchmark, "summarize", return_value={"completed": True}) as summarize:
            local.execute(directory, config)
        self.assertEqual(loop.call_count, 1)
        self.assertEqual(loop.call_args.args[0], directory / "prepared" / "shards" / "0.json")
        summarize.assert_called_once()
        summary = benchmark.summarize(directory / "prepared", directory / "missing",
                                      directory / "partial", workers_succeeded=False)
        body = (directory / "partial" / "report.md").read_text()
        self.assertFalse(summary["completed"])
        self.assertIn("PARTIAL", body)
        self.assertNotIn("/actions/runs/", body)
        self.assertEqual(summary["evaluated"], 0)

    def test_run_id_cannot_escape_output_directory(self):
        for run_id in ("..", "..\\elsewhere", "../elsewhere", "not-a-run"):
            with self.assertRaises(Abstain):
                local.run_directory(self.directory, run_id)


if __name__ == "__main__":
    unittest.main()
