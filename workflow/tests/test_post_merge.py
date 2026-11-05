# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import copy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import MagicMock, patch

from nfv_workflow import post_merge
from nfv_workflow.github import Abstain, GitHub, identity
from nfv_workflow.publish import marker
from test_workflow import event


class API:
    def __init__(self):
        self.pr = event()["pull_request"]
        self.pr.update(changed_files=1, merged_at="2026-01-01T00:00:00Z")
        self.pr["base"]["ref"] = "main"
        self.commits = ["c" * 40]
        self.files = [{"filename": "src/main.py"}]
        self.issues = []
        self.submissions = []
        self.complete = True
        self.retained = True

    def request(self, path, data=None):
        if data is not None:
            self.submissions.append((path, data))
            return None
        if path == "repos/owner/project":
            return {"default_branch": "main"}
        if path == "repos/owner/project/pulls/7":
            return copy.deepcopy(self.pr)
        if path == "repos/owner/project/commits/main":
            return {"sha": "f" * 40}
        if "/compare/" in path:
            if path.endswith("b" * 40 + "..." + "f" * 40):
                return {"status": "ahead", "commits": [{"sha": value} for value in self.commits],
                        "total_commits": len(self.commits) + int(not self.complete)}
            return {"status": "ahead" if self.retained else "diverged"}
        raise AssertionError(f"Unexpected API read: {path}")

    def pages(self, path):
        if "/commits/" in path and path.endswith("/pulls"):
            return [copy.deepcopy(self.pr)]
        if "/issues?" in path:
            assert "state=all" in path and "creator=github-actions" in path
            return self.issues
        if path.endswith("/pulls/7/files"):
            return self.files
        raise AssertionError(f"Unexpected pagination: {path}")


class PostMergeTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.push = {"repository": {"full_name": "owner/project"}, "ref": "refs/heads/main",
                     "before": "b" * 40, "after": "f" * 40}
        self.request = {"repository": self.push["repository"],
                        "inputs": {"pull-request-number": "7", "merge-sha": "c" * 40}}

    def test_fork_merge_is_bound_to_default_history_but_captures_original_pr_head(self):
        captured = post_merge.review_event(self.request, self.api, "refs/heads/main")
        ident = identity(captured)
        self.assertEqual(ident["source_sha"], "a" * 40)
        self.assertEqual(ident["merge_sha"], "c" * 40)
        self.assertEqual(ident["source_repository"], "owner/project")
        self.assertEqual(captured["pull_request"]["head"]["repo"]["full_name"], "fork/project")

    def test_dispatch_rejects_wrong_ref_unmerged_wrong_base_merge_or_detached_history(self):
        for mutation in ("ref", "unmerged", "base", "merge", "history", "number"):
            api, raw, ref = API(), copy.deepcopy(self.request), "refs/heads/main"
            if mutation == "ref":
                ref = "refs/heads/feature"
            elif mutation == "unmerged":
                api.pr["merged"] = False
            elif mutation == "base":
                api.pr["base"]["ref"] = "release"
            elif mutation == "merge":
                raw["inputs"]["merge-sha"] = "d" * 40
            elif mutation == "history":
                api.retained = False
            else:
                raw["inputs"]["pull-request-number"] = "$(execute)"
            with self.subTest(mutation=mutation), self.assertRaises(Abstain):
                post_merge.review_event(raw, api, ref)
            self.assertFalse(api.submissions)

    def test_open_pr_events_cannot_start_post_merge_review(self):
        raw = event(False)
        raw.update(action="synchronize")
        raw["pull_request"]["state"] = "open"
        with self.assertRaises(Abstain):
            post_merge.review_event(raw, self.api, "refs/pull/7/merge")

    def test_push_dispatches_eligible_merge_exactly_once_even_with_many_associated_commits(self):
        self.api.commits = ["a" * 40, "c" * 40, "f" * 40]
        result = post_merge.dispatch(self.push, self.api)
        self.assertEqual(result, {"dispatched": [7], "skipped": []})
        self.assertEqual(self.api.submissions, [(
            "repos/owner/project/actions/workflows/nfv-pr-auditor.lock.yml/dispatches",
            {"ref": "main", "inputs": self.request["inputs"]})])

    def test_direct_pushes_and_unsupported_changes_do_not_start_inference(self):
        self.api.commits = ["f" * 40]
        result = post_merge.dispatch(self.push, self.api)
        self.assertEqual(result["dispatched"], [])
        self.assertFalse(self.api.submissions)
        self.api.commits = ["c" * 40]
        self.api.files = [{"filename": "README.md"}]
        result = post_merge.dispatch(self.push, self.api)
        self.assertEqual(result["skipped"], [{"number": 7, "reason": "no supported source changes"}])
        self.assertFalse(self.api.submissions)

    def test_old_closed_refutation_issue_suppresses_dispatch_using_stable_report_marker(self):
        captured = post_merge.review_event(self.request, self.api, "refs/heads/main")
        self.api.issues = [{"state": "closed", "closed_at": "2020-01-01T00:00:00Z",
                            "body": marker(identity(captured))}]
        result = post_merge.dispatch(self.push, self.api)
        self.assertEqual(result["dispatched"], [])
        self.assertEqual(result["skipped"][0]["reason"], "refutation already tracked")
        self.assertFalse(self.api.submissions)
        self.assertEqual(post_merge.eligibility(self.request, self.api, self.push["ref"]),
                         {"review": False, "reason": "refutation already tracked"})

    def test_one_push_dispatches_every_eligible_pr_in_a_batch(self):
        class Batch(API):
            def __init__(self):
                super().__init__()
                self.other = copy.deepcopy(self.pr)
                self.other.update(number=8, merge_commit_sha="e" * 40)
                self.other["head"]["sha"] = "d" * 40
                self.commits = ["c" * 40, "e" * 40]
            def request(self, path, data=None):
                if data is None and path.endswith("/pulls/8"):
                    return copy.deepcopy(self.other)
                return super().request(path, data)
            def pages(self, path):
                if "/commits/" + "e" * 40 + "/pulls" in path:
                    return [copy.deepcopy(self.other)]
                if path.endswith("/pulls/8/files"):
                    return self.files
                return super().pages(path)
        api = Batch()
        self.assertEqual(post_merge.dispatch(self.push, api)["dispatched"], [7, 8])
        self.assertEqual([data["inputs"]["pull-request-number"] for _, data in api.submissions], ["7", "8"])

    def test_incomplete_or_forced_push_and_incomplete_files_fail_without_dispatch(self):
        for case in ("forced", "commits", "files"):
            api, raw = API(), copy.deepcopy(self.push)
            if case == "forced":
                raw["forced"] = True
            elif case == "commits":
                api.complete = False
            else:
                api.pr["changed_files"] = 2
            with self.subTest(case=case), self.assertRaises(Abstain):
                post_merge.dispatch(raw, api)
            self.assertFalse(api.submissions)

    def test_event_command_writes_validated_checkout_environment_and_failures_are_explicit(self):
        root = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        raw, out, environment = root / "input.json", root / "event.json", root / "env"
        raw.write_text(json.dumps(self.request), encoding="utf-8")
        argv = ["event", "--event", str(raw), "--out", str(out)]
        with patch.object(post_merge, "GitHub", return_value=self.api), \
                patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main", "GITHUB_ENV": str(environment)}):
            self.assertEqual(post_merge.main(argv), 0)
        self.assertEqual(json.loads(out.read_text())["pull_request"]["head"]["sha"], "a" * 40)
        self.assertIn("NFV_BASE_SHA=" + "b" * 40, environment.read_text())
        self.assertIn("NFV_BASE_REPOSITORY=owner/project", environment.read_text())
        stderr = io.StringIO()
        with patch.object(post_merge, "GitHub", return_value=self.api), \
                patch.dict(os.environ, {"GITHUB_REF": "refs/heads/feature"}), redirect_stderr(stderr):
            self.assertEqual(post_merge.main(argv), 1)
        self.assertIn("trusted default branch", stderr.getvalue())

    def test_eligibility_command_returns_a_real_noop_before_any_inference(self):
        root = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        raw, output = root / "input.json", root / "outputs"
        raw.write_text(json.dumps(self.request), encoding="utf-8")
        self.api.files = [{"filename": "README.md"}]
        with patch.object(post_merge, "GitHub", return_value=self.api), \
                patch.dict(os.environ, {"GITHUB_REF": self.push["ref"], "GITHUB_OUTPUT": str(output)}):
            self.assertEqual(post_merge.main(["eligible", "--event", str(raw)]), 0)
        self.assertEqual(output.read_text(), "review=false\n")
        self.assertFalse(self.api.submissions)

    def test_github_dispatch_204_is_success_without_parsing_a_nonexistent_json_body(self):
        response = MagicMock()
        response.status = 204
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(GitHub().request("repos/owner/project/actions/workflows/x/dispatches",
                                              {"ref": "main"}))
        response.read.assert_not_called()
