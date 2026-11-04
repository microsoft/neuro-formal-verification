import base64
import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
from string import Template
import unittest
import uuid
from unittest.mock import patch

from nfv_workflow.check import Dafny, accepted, check
from nfv_workflow.cli import audit, audit_limits, awf_provider, submission, write_sources
from nfv_workflow.github import (Abstain, GitHub, added_lines, change_blocks, collect, identity,
                                 reanchor, removed_lines, sha, source_path)
from nfv_workflow.model import expression, render_model, select_candidates
from nfv_workflow.publish import comment, marker, publication_allowed, publish, render
from nfv_workflow.source_binding import bind_source, Parser


def event(merged=True):
    return {"action": "closed", "repository": {"full_name": "owner/project"},
            "pull_request": {"number": 7, "state": "closed", "merged": merged,
                             "merge_commit_sha": "c" * 40 if merged else None,
                             "closed_at": "2026-01-01T00:00:00Z",
                             "head": {"sha": "a" * 40, "repo": {"full_name": "fork/project"}},
                             "base": {"sha": "b" * 40, "repo": {"full_name": "owner/project"}}}}


def bundle():
    return {"identity": identity(event()), "signals": [
        {"id": "comment:1", "text": "The return value equals the input.", "url": ""}],
        "changed": {"src/f.cpp": [1, 2]},
        "sources": {"src/f.cpp": {"text": "int f(int x) {\n  return x + 1;\n}\n"}}}


def candidate():
    return {"path": "src/f.cpp", "lines": [2], "signal_id": "comment:1",
            "quote": "return value equals the input",
            "property": "The return value equals the input.",
            "explanation": "For input zero the model returns one, rather than zero.",
            "correspondence": "This applies only if mathematical integers model the source arithmetic.",
            "model": {"parameters": [{"name": "x", "type": "int"}], "result_type": "int",
                      "body": {"+": ["x", 1]}, "property": {"==": ["result", "x"]},
                      "witness": {"x": 0}, "preconditions": [], "assumptions": [], "environment": []}}


def success():
    return {"outcome": "verified", "exit_code": 0, "mode": "unbounded",
            "stdout": "Dafny program verifier finished with 2 verified, 0 errors", "stderr": ""}


def cross_file_fixture():
    obj, data = candidate(), bundle()
    obj["lines"] = [3]
    obj["model"]["body"] = {"call": ["helper", "x"]}
    obj["model"]["environment"] = [{
        "name": "helper", "parameters": [{"name": "x", "type": "int"}],
        "result_type": "int", "body": {"+": ["x", 1]},
        "source": {"path": "src/helper.cpp", "lines": [2, 3, 4]},
        "description": "Source-defined scalar increment."}]
    data["changed"]["src/f.cpp"] = [3]
    data["sources"] = {
        "src/f.cpp": {"text": '#include "helper.h"\nint f(int x) {\n  return helper(x);\n}\n'},
        "src/helper.h": {"text": "#pragma once\nint helper(int x);\n"},
        "src/helper.cpp": {"text": '#include "helper.h"\nint helper(int x) {\n  return x+1;\n}\n'}}
    data["source_inventory"] = sorted(data["sources"])
    data["source_inventory_complete"] = True
    return data, obj


class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.directory)
        try:
            self.directory.parent.rmdir()
        except OSError:
            pass


class SourceFilesTests(ScratchTest):
    def test_writes_sources_with_repository_line_numbers(self):
        write_sources({"sources": {"src/a.c": {"text": "x\ny", "line_start": 3}}}, self.directory)
        self.assertEqual((self.directory / "src" / "a.c").read_text(encoding="utf-8").splitlines(),
                         ["", "", "x", "y"])

    def test_rejects_paths_outside_directory(self):
        with self.assertRaises(Abstain):
            write_sources({"sources": {"../a.c": {"text": "x"}}}, self.directory)


class IdentityTests(unittest.TestCase):
    def test_closed_unmerged_rejected(self):
        with self.assertRaises(Abstain):
            identity(event(False))

    def test_merged_without_merge_sha_rejected(self):
        data = event()
        data["pull_request"]["merge_commit_sha"] = None
        with self.assertRaises(Abstain):
            identity(data)

    def test_merged_head_not_merge_result(self):
        ident = identity(event(True))
        self.assertEqual(ident["source_sha"], "a" * 40)
        self.assertEqual(ident["merge_sha"], "c" * 40)

    def test_deleted_fork_keeps_base_commit_lookup(self):
        data = event()
        data["pull_request"]["head"]["repo"] = None
        self.assertEqual(identity(data)["source_repository"], "owner/project")

    def test_nonclosed_event_rejected(self):
        data = event()
        data["action"] = "synchronize"
        with self.assertRaises(Abstain):
            identity(data)

    def test_open_pr_is_reviewed_at_its_head(self):
        data = event(False)
        data["action"] = "synchronize"
        data["pull_request"]["state"] = "open"
        ident = identity(data)
        self.assertEqual((ident["merged"], ident["merge_sha"], ident["source_sha"]),
                         (False, None, "a" * 40))

    def test_mutable_ref_rejected(self):
        with self.assertRaises(Abstain):
            sha("main")

    def test_paths_not_executable_or_traversing(self):
        for value in ["../x", "/a", "a\\b", "a/../b", "a\nb"]:
            with self.subTest(value=value), self.assertRaises(Abstain):
                source_path(value)

    def test_hunk_anchors(self):
        self.assertEqual(added_lines("@@ -1,2 +1,3 @@\n x\n-y\n+z\n+w"), [2, 3])


class GrammarTests(unittest.TestCase):
    def test_inferred_conditions_cannot_claim_source_certification(self):
        obj = candidate()
        obj["model"]["preconditions"] = [
            {"origin": "source", "description": "invented contract", "expression": True}]
        with self.assertRaises(Abstain):
            render_model(obj)

    def test_mechanical_negation_only(self):
        source = render_model(candidate())
        self.assertIn("assert !((result == x));", source)
        self.assertNotIn("ensures", source)
        self.assertNotIn("assume", source)
        self.assertIn("method NfvRefutation()\n{", source)

    def test_arbitrary_code_rejected(self):
        for node in ["x; assume false;", {"raw": "assume false;"}, {"include": "evil.dfy"}]:
            with self.subTest(node=node), self.assertRaises(Abstain):
                expression(node, ["x"])

    def test_unknown_callee_rejected(self):
        with self.assertRaises(Abstain):
            expression({"call": ["unknown", 1]}, [])

    def test_witness_must_be_concrete(self):
        obj = candidate()
        obj["model"]["witness"]["x"] = {"+": [1, 2]}
        with self.assertRaises(Abstain):
            render_model(obj)

    def test_witness_must_be_complete(self):
        obj = candidate()
        obj["model"]["witness"] = {}
        with self.assertRaises(Abstain):
            render_model(obj)

    def test_bad_boolean_witness_for_integer_rejected(self):
        obj = candidate()
        obj["model"]["witness"]["x"] = True
        with self.assertRaises(Abstain):
            render_model(obj)

    def test_depth_bounded(self):
        node = True
        for _ in range(30):
            node = {"not": node}
        with self.assertRaises(Abstain):
            expression(node, [])

    def test_duplicate_and_maximum_ten(self):
        items = [candidate(), candidate()]
        for index in range(12):
            obj = candidate()
            obj["property"] += str(index)
            obj["model"]["body"] = {"+": ["x", index]}
            items.append(obj)
        result = select_candidates(items, bundle())
        self.assertEqual(len(result), 10)
        self.assertEqual(len({x["id"] for x in result}), 10)

    def test_same_property_different_witness_is_deduplicated(self):
        obj = candidate()
        obj["property"] = "An equivalent paraphrase"
        obj["model"]["witness"]["x"] = 42
        self.assertEqual(len(select_candidates([candidate(), obj], bundle())), 1)

    def test_unrelated_anchor_rejected(self):
        obj = candidate()
        obj["lines"] = [3]
        with self.assertRaises(Abstain):
            select_candidates([obj], bundle())


class SourceBindingTests(unittest.TestCase):
    def test_cross_file_overload_abstains(self):
        data, obj = cross_file_fixture()
        data["sources"]["src/helper.h"]["text"] += "bool helper(bool x);\n"
        with self.assertRaises(Abstain):
            bind_source(obj, data)

    def test_cross_file_multiple_implementations_abstain(self):
        data, obj = cross_file_fixture()
        data["source_inventory"].append("src/helper.c")
        with self.assertRaises(Abstain):
            bind_source(obj, data)

    def test_cross_file_incomplete_inventory_abstains(self):
        data, obj = cross_file_fixture()
        data["source_inventory_complete"] = False
        with self.assertRaises(Abstain):
            bind_source(obj, data)

    def test_cross_file_invented_callee_body_abstains(self):
        data, obj = cross_file_fixture()
        obj["model"]["environment"][0]["body"] = {"+": ["x", 999]}
        with self.assertRaises(Abstain):
            bind_source(obj, data)

    def test_cross_file_mismatched_prototype_abstains(self):
        data, obj = cross_file_fixture()
        data["sources"]["src/helper.h"]["text"] = "bool helper(int x);\n"
        with self.assertRaises(Abstain):
            bind_source(obj, data)

    def test_arbitrary_wrong_body_is_rejected(self):
        obj = candidate()
        obj["model"]["body"] = {"+": ["x", 999]}
        with self.assertRaises(Abstain):
            bind_source(obj, bundle())

    def test_matching_source_has_concrete_result(self):
        self.assertEqual(bind_source(candidate(), bundle())["result"], 1)

    def test_overflow_is_not_a_source_finding(self):
        obj = candidate()
        obj["model"]["witness"]["x"] = 2**31 - 1
        with self.assertRaises(Abstain):
            bind_source(obj, bundle())

    def test_unsupported_control_flow_abstains(self):
        data = bundle()
        data["sources"]["src/f.cpp"]["text"] = "int f(int x) {\n if (x) return x; return x+1;\n}\n"
        with self.assertRaises(Abstain):
            bind_source(candidate(), data)

    def test_macros_and_unsigned_types_abstain(self):
        for source in ["#define x 0\nint f(int x) {\nreturn x+1;\n}",
                       "unsigned int f(int x) {\nreturn x+1;\n}"]:
            data = bundle()
            data["sources"]["src/f.cpp"]["text"] = source
            with self.subTest(source=source), self.assertRaises(Abstain):
                bind_source(candidate(), data)

    def test_literal_and_precedence_parsing(self):
        self.assertEqual(Parser("x + 1 * 2").parse(), {"+": ["x", {"*": [1, 2]}]})
        with self.assertRaises(Abstain):
            Parser("012").parse()

    def test_source_comments_cannot_create_a_function(self):
        data = bundle()
        data["sources"]["src/f.cpp"]["text"] = "/*\nint f(int x) {return x+1;}\n"
        with self.assertRaises(Abstain):
            bind_source(candidate(), data)

    def test_invented_quote_rejected(self):
        obj = candidate()
        obj["quote"] = "not in the real comment"
        with self.assertRaises(Abstain):
            select_candidates([obj], bundle())

    def test_line_wrapped_quote_accepted(self):
        data = bundle()
        data["signals"][0]["text"] = "The return value\nequals the input."
        self.assertEqual(len(select_candidates([candidate()], data)), 1)

    def test_environment_needs_captured_source(self):
        obj = candidate()
        obj["model"]["environment"] = [{"source": {"path": "missing.cpp", "lines": [1]}}]
        with self.assertRaises(Abstain):
            select_candidates([obj], bundle())


class VerifierTests(ScratchTest):
    def test_no_findings_on_no_candidates(self):
        report = check(bundle(), [], self.directory, lambda *_: self.fail("verifier called"))
        self.assertEqual(report["findings"], [])

    def test_positive_exit_not_enough(self):
        result = success()
        result["stdout"] = "0 verified, 0 errors"
        self.assertFalse(accepted(result))

    def test_all_nonrefutation_outcomes_abstain(self):
        for outcome in ["failed", "type_error", "timeout", "tool_error"]:
            result = success()
            result["outcome"] = outcome
            report = check(bundle(), [candidate()], self.directory, lambda *_: result)
            self.assertFalse(report["findings"])

    def test_minimization_is_rechecked(self):
        obj = candidate()
        obj["model"]["assumptions"] = [
            {"description": "x is nonnegative", "expression": {">=": ["x", 0]}}]
        calls = []
        def verifier(source, directory):
            calls.append(source)
            return success()
        report = check(bundle(), [obj], self.directory, verifier)
        self.assertEqual(len(calls), 3)
        self.assertEqual(report["findings"][0]["retained_assumptions"], [])
        self.assertIn("assert (x >= 0);", calls[-1])
        self.assertNotIn("requires (x >= 0)", calls[-1])

    def test_final_recheck_required(self):
        count = 0
        def verifier(*_):
            nonlocal count
            count += 1
            return success() if count == 1 else {"outcome": "timeout"}
        self.assertFalse(check(bundle(), [candidate()], self.directory, verifier)["findings"])

    def test_worker_strips_credentials(self):
        def invoke(cmd, **kwargs):
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertNotIn("COPILOT_GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("AZURE_SECRET", kwargs["env"])
            self.assertEqual(kwargs["env"]["NFV_Z3_PATH"], "trusted-bundled-z3")
            self.assertIn("nfv_workflow.worker", cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "intentional mock")
        with patch.dict(os.environ, {"GH_TOKEN": "sentinel", "COPILOT_GITHUB_TOKEN": "sentinel",
                                     "AZURE_SECRET": "sentinel",
                                     "NFV_Z3_PATH": "trusted-bundled-z3"}), patch(
                                         "nfv_workflow.check.subprocess.run", side_effect=invoke):
            self.assertEqual(Dafny()("", self.directory)["outcome"], "tool_error")

    def test_run_budget_abstains(self):
        report = check(bundle(), [candidate()], self.directory,
                       lambda *_: self.fail("verifier called"), budget_seconds=0)
        self.assertFalse(report["findings"])

    def test_invalid_proposal_is_error_not_finding(self):
        report = check(bundle(), [{"path": "../evil"}], self.directory)
        self.assertTrue(report["errors"])
        self.assertFalse(report["findings"])

    def test_submission_is_data_not_a_file_path(self):
        path = self.directory / "submission.json"
        path.write_text(json.dumps({"items": [{"type": "nfv_submit", "proposals": "[]"}]}))
        self.assertEqual(submission(path), [])
        path.write_text(json.dumps({"items": [{"type": "nfv_submit", "proposals": "/etc/passwd"}]}))
        with self.assertRaises(ValueError):
            submission(path)


class PublicationTests(ScratchTest):
    url = "https://github.com/owner/project/actions/runs/123/artifacts/456"

    def report(self):
        return check(bundle(), [candidate()], self.directory, lambda *_: success())

    def test_empty_does_not_call_api(self):
        class NoAPI:
            def graphql(self, *_):
                raise AssertionError("API called")
        report = check(bundle(), [], self.directory)
        self.assertEqual(publish(NoAPI(), report, bundle(), self.url, "General", True),
                         {"status": "no-findings"})

    def test_errors_suppress_discussion(self):
        report = self.report()
        report["errors"] = ["collection incomplete"]
        self.assertIsNone(render(report, bundle(), self.url))

    def test_open_pr_identity_publishes_without_merge_note(self):
        report, data = self.report(), bundle()
        data["identity"].update(merged=False, merge_sha=None)
        report["identity"] = data["identity"]
        body = render(report, data, self.url)
        self.assertIn("# NFV: 1 potential issue in PR #", body)
        self.assertNotIn("merged as", body)

    def test_merged_identity_without_merge_sha_cannot_publish(self):
        report, data = self.report(), bundle()
        data["identity"]["merge_sha"] = None
        report["identity"] = data["identity"]
        with self.assertRaises(Abstain):
            render(report, data, self.url)

    def test_clear_conditional_explanation(self):
        body = render(self.report(), bundle(), self.url)
        self.assertIn("# NFV: 1 potential issue in PR #", body)
        self.assertIn("[Neuro-formal verification (NFV)](https://arxiv.org/abs/2608.21516)", body)
        self.assertIn("Source overlay", body)
        self.assertIn("merged as `" + "c" * 40 + "`", body)
        self.assertIn(self.url, body)
        self.assertIn("a" * 40, body)

    def test_untrusted_prose_is_not_active_markdown(self):
        obj = candidate()
        obj["explanation"] = "<script> @everyone [click](https://evil.example) ```"
        report = check(bundle(), [obj], self.directory, lambda *_: success())
        body = render(report, bundle(), self.url)
        self.assertNotIn("<script>", body)
        self.assertNotIn("@everyone", body)
        self.assertNotIn("[click](https://evil.example)", body)

    def test_non_github_evidence_rejected(self):
        with self.assertRaises(Abstain):
            render(self.report(), bundle(), "https://evil.example")

    def test_deduplication_survives_rerun(self):
        report = self.report()
        class API:
            def graphql(self, query, variables):
                if "mutation" in query:
                    raise AssertionError("duplicate was published")
                return {"repository": {"id": "R", "discussionCategories": {
                    "nodes": [{"id": "C", "name": "General"}], "pageInfo": {"hasNextPage": False}},
                    "discussions": {"nodes": [{"body": marker(report["identity"]), "url": "existing"}],
                                    "pageInfo": {"hasNextPage": False}}}}
        result = publish(API(), report, bundle(), self.url, "General", True)
        self.assertEqual(result, {"status": "duplicate", "url": "existing"})

    def creating_api(self, label=True, label_error=False):
        calls = {"create": [], "label": [], "queries": []}
        class API:
            def graphql(self, query, variables):
                if "createDiscussion" in query:
                    calls["create"].append(variables)
                    return {"createDiscussion": {"discussion": {"id": "D", "url": "created"}}}
                if "addLabelsToLabelable" in query:
                    calls["label"].append(variables)
                    if label_error:
                        raise Abstain("GitHub GraphQL failed; no publication attempted")
                    return {"addLabelsToLabelable": {"clientMutationId": None}}
                calls["queries"].append(variables)
                return {"repository": {"id": "R", "label": {"id": "L"} if label else None,
                    "discussionCategories": {
                    "nodes": [{"id": "C", "name": "General"}], "pageInfo": {"hasNextPage": False}},
                    "discussions": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
        return API(), calls

    def test_exactly_one_creation_for_multiple_findings(self):
        report = self.report()
        report["findings"].append(copy.deepcopy(report["findings"][0]))
        api, calls = self.creating_api()
        self.assertEqual(publish(api, report, bundle(), self.url, "General", True)["status"], "created")
        self.assertEqual(len(calls["create"]), 1)
        self.assertTrue(calls["create"][0]["input"]["title"].startswith("NFV:"))

    def test_created_discussion_gets_nfv_formal_review_label(self):
        api, calls = self.creating_api()
        result = publish(api, self.report(), bundle(), self.url, "General", True)
        self.assertEqual(calls["queries"][0]["label"], "NFV-formal-review")
        self.assertEqual(calls["label"], [{"input": {"labelableId": "D", "labelIds": ["L"]}}])
        self.assertEqual(result["label"], "NFV-formal-review")
        self.assertNotIn("label_warning", result)

    def test_missing_label_still_publishes_with_warning(self):
        api, calls = self.creating_api(label=False)
        result = publish(api, self.report(), bundle(), self.url, "General", True)
        self.assertEqual(result["status"], "created")
        self.assertEqual(calls["label"], [])
        self.assertIn("does not exist", result["label_warning"])

    def test_label_failure_does_not_discard_created_discussion(self):
        api, calls = self.creating_api(label_error=True)
        result = publish(api, self.report(), bundle(), self.url, "General", True)
        self.assertEqual((result["status"], result["url"]), ("created", "created"))
        self.assertIn("Could not apply label", result["label_warning"])

    def test_immutable_commit_evidence_url_accepted_moving_branch_rejected(self):
        commit = "https://github.com/o/bench/tree/" + "a" * 40 + "/nfv/run-1"
        self.assertIn(commit, render(self.report(), bundle(), commit))
        with self.assertRaises(Abstain):
            render(self.report(), bundle(), "https://github.com/o/bench/tree/main/nfv/run-1")

    def test_discussion_target_repository_override(self):
        api, calls = self.creating_api()
        report = self.report()
        result = publish(api, report, bundle(), self.url, "General", True, target="other/bench")
        self.assertEqual(result["status"], "created")
        self.assertEqual((calls["queries"][0]["owner"], calls["queries"][0]["repo"]), ("other", "bench"))
        source_repo = report["identity"]["repository"]
        self.assertIn(f"https://github.com/{source_repo}/blob/", calls["create"][0]["input"]["body"])

    def test_preview_does_not_create_or_label(self):
        api, calls = self.creating_api()
        self.assertEqual(publish(api, self.report(), bundle(), self.url, "General", False)["status"], "preview")
        self.assertEqual((calls["create"], calls["label"]), ([], []))

    def test_staged_flag_prevents_publication(self):
        with patch.dict(os.environ, {"GH_AW_SAFE_OUTPUTS_STAGED": "true"}):
            self.assertFalse(publication_allowed(None))

    def test_actions_staged_mode_comes_from_immutable_workflow(self):
        class API:
            def request(self, path):
                self.path = path
                return {"encoding": "base64", "content": base64.b64encode(
                    b'          GH_AW_INFO_STAGED: "true"\n').decode()}
        api = API()
        with patch.dict(os.environ, {
                "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/project",
                "GITHUB_WORKFLOW_SHA": "d" * 40,
                "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/nfv.lock.yml@refs/heads/main",
                "GH_AW_SAFE_OUTPUTS_STAGED": ""}):
            self.assertFalse(publication_allowed(api))
            self.assertIn("?ref=" + "d" * 40, api.path)

    def test_unknown_actions_staged_mode_fails_closed(self):
        class API:
            def request(self, _):
                return {"encoding": "base64", "content": base64.b64encode(b"name: x").decode()}
        with patch.dict(os.environ, {
                "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/project",
                "GITHUB_WORKFLOW_SHA": "d" * 40,
                "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/nfv.lock.yml@refs/heads/main",
                "GH_AW_SAFE_OUTPUTS_STAGED": ""}), self.assertRaises(Abstain):
            publication_allowed(API())

    def test_tampered_source_binding_cannot_publish(self):
        report = self.report()
        report["findings"][0]["candidate"]["model"]["body"] = {"+": ["x", 999]}
        with self.assertRaises(Abstain):
            render(report, bundle(), self.url)

    def test_tampered_dafny_evidence_cannot_publish(self):
        report = self.report()
        report["findings"][0]["dafny"] = "method Fake() {}"
        with self.assertRaises(Abstain):
            render(report, bundle(), self.url)

    def test_synthetic_source_identity_cannot_publish(self):
        data = bundle()
        data["identity"]["source_kind"] = "synthetic"
        report = check(data, [candidate()], self.directory, lambda *_: success())
        with self.assertRaises(Abstain):
            render(report, data, self.url)

class CollectionTests(unittest.TestCase):
    def test_collects_python_module_context_without_importing_it(self):
        api = self.api()
        original = api.request
        def request(path):
            result = original(path)
            if "/compare/" in path:
                result["files"][0]["filename"] = "pkg/main.py"
            if "/git/trees/" in path:
                result["tree"] = [
                    {"path": "pkg/main.py", "type": "blob", "mode": "100644", "sha": "d" * 40},
                    {"path": "pkg/helper.py", "type": "blob", "mode": "100644", "sha": "e" * 40}]
            return result
        api.request = request
        api.blob = lambda repo, oid: (
            "from helper import inc\ndef f(x): return inc(x)\n" if oid == "d" * 40
            else "def inc(x): return x+1\n")
        result = collect(event(), api)
        self.assertIn("pkg/helper.py", result["sources"])
        self.assertIn("def inc", result["sources"]["pkg/helper.py"]["text"])

    def test_collects_rust_module_and_crate_use_context(self):
        api = self.api()
        original = api.request
        def request(path):
            result = original(path)
            if "/compare/" in path:
                result["files"][0]["filename"] = "src/lib.rs"
            if "/git/trees/" in path:
                result["tree"] = [
                    {"path": "src/lib.rs", "type": "blob", "mode": "100644", "sha": "d" * 40},
                    {"path": "src/math/mod.rs", "type": "blob", "mode": "100644", "sha": "e" * 40},
                    {"path": "src/math/ops.rs", "type": "blob", "mode": "100644", "sha": "f" * 40},
                    {"path": "vendor/unrelated.rs", "type": "blob", "mode": "100644", "sha": "1" * 40}]
            return result
        texts = {"d" * 40: "mod math;\nuse crate::math::ops::inc;\npub fn f(x: u32) -> u32 { inc(x) }\n",
                 "e" * 40: "pub mod ops;\n", "f" * 40: "pub fn inc(x: u32) -> u32 { x + 1 }\n",
                 "1" * 40: "pub fn other() {}\n"}
        api.request, api.blob = request, lambda repo, oid: texts[oid]
        result = collect(event(), api)
        self.assertIn("src/math/mod.rs", result["sources"])
        self.assertIn("src/math/ops.rs", result["sources"])
        self.assertNotIn("vendor/unrelated.rs", result["sources"])

    def test_collects_quoted_header_companion_implementation(self):
        api = self.api()
        original_request, original_blob = api.request, api.blob
        def request(path):
            result = original_request(path)
            if "/git/trees/" in path:
                result["tree"].extend([
                    {"path": "src/helper.h", "type": "blob", "mode": "100644", "sha": "1" * 40},
                    {"path": "src/helper.cpp", "type": "blob", "mode": "100644", "sha": "2" * 40}])
            return result
        def blob(repo, oid):
            if oid == "d" * 40:
                return '#include "helper.h"\nint f(int x) { return helper(x); }\n'
            if oid == "1" * 40:
                return "int helper(int x);\n"
            if oid == "2" * 40:
                return "int helper(int x) { return x+1; }\n"
            return original_blob(repo, oid)
        api.request, api.blob = request, blob
        result = collect(event(), api)
        self.assertIn("src/helper.cpp", result["sources"])
        self.assertIn("src/helper.h", result["sources"])
        self.assertTrue(result["source_inventory_complete"])

    def test_base_side_snapshot_anchors_removed_lines_and_reanchors(self):
        api = self.api()
        trees = []
        original = api.request
        def request(path):
            if "/git/trees/" in path:
                trees.append(path)
            return original(path)
        api.request = request
        base = collect(event(), api, side="base")
        ident = base["identity"]
        self.assertEqual((ident["source_kind"], ident["source_sha"]),
                         ("pull-request-base", ident["base_sha"]))
        self.assertIn(ident["base_sha"], trees[-1])
        self.assertEqual(base["changed"], {"src/f.cpp": [1]})
        props = [{"path": "src/f.cpp", "lines": [1], "property": "p"}]
        moved = reanchor(props, base)
        self.assertEqual(moved["properties"][0]["lines"], [1])
        self.assertEqual(moved["properties"][0]["head_lines"], [1])
        with self.assertRaises(Abstain):
            reanchor(props, collect(event(), self.api()))
        from nfv_workflow.publish import render
        with self.assertRaises(Abstain):
            render({"identity": ident, "findings": [{}]}, {"identity": ident},
                   "https://github.com/owner/project/actions/runs/1")

    def test_change_blocks_pair_removed_and_added_runs(self):
        patch = "@@ -10,4 +10,5 @@\n a\n-b\n+B\n+B2\n c\n+new\n d"
        self.assertEqual(change_blocks(patch), [([11], [11, 12]), ([], [14])])
        self.assertEqual(removed_lines(patch), [11])
        base = {"identity": {"source_kind": "pull-request-base"},
                "head_to_base": {"f.c": {"path": "f.c", "lines": {"11": [11], "12": [11]}}}}
        result = reanchor([{"path": "f.c", "lines": [12]}, {"path": "f.c", "lines": [14]}], base)
        self.assertEqual([x["lines"] for x in result["properties"]], [[11]])
        self.assertEqual(len(result["dropped"]), 1)

    def test_context_prefers_direct_includes_over_alphabetical_neighbours(self):
        api = self.api()
        original_request, original_blob = api.request, api.blob
        def request(path):
            result = original_request(path)
            if "/git/trees/" in path:
                result["tree"].extend(
                    [{"path": f"src/a{i:02}.h", "type": "blob", "mode": "100644", "sha": f"{i:040x}"}
                     for i in range(1, 60)]
                    + [{"path": "src/z/near.h", "type": "blob", "mode": "100644", "sha": "9" * 40}])
            return result
        def blob(repo, oid):
            if oid == "d" * 40:
                return '#include "z/near.h"\nint f() { return near(); }\n'
            return "int x;\n"
        api.request, api.blob = request, blob
        result = collect(event(), api)
        self.assertIn("src/z/near.h", result["sources"])

    def api(self):
        data = event()["pull_request"]
        data["changed_files"] = 1
        data["body"] = "return input"
        class API:
            def request(self, path):
                if "/compare/" in path:
                    return {"files": self.pages("fixture/files")}
                if "/git/trees/" in path:
                    return {"truncated": False, "tree": [
                        {"path": "src/f.cpp", "type": "blob", "mode": "100644", "sha": "d" * 40},
                        {"path": "src/f.h", "type": "blob", "mode": "100644", "sha": "e" * 40}]}
                return data
            def pages(self, path):
                if path.endswith("/files"):
                    return [{"filename": "src/f.cpp", "status": "modified",
                             "patch": "@@ -1 +1 @@\n-old\n+int f() { return 1; }"}]
                if path.endswith("/commits"):
                    return [{"sha": "a" * 40, "commit": {"message": "return input"}, "html_url": ""}]
                return [{"id": 1, "body": "return input", "html_url": ""}]
            def blob(self, repo, oid):
                return '#include "f.h"\nint f() { SASSERT(true); return 1; }\n'
        return API()

    def test_collects_description_comments_and_commits_without_reviews(self):
        result = collect(event(), self.api())
        self.assertEqual({x["kind"] for x in result["signals"]}, {"description", "comment", "commit"})
        self.assertIn("src/f.h", result["sources"])
        self.assertTrue(result["declarations_and_preconditions"])
        self.assertIn("SASSERT", result["callee_names"])

    def test_previous_nfv_bot_reports_are_not_developer_intent(self):
        api = self.api()
        original = api.pages
        marker = "<!-- gh-aw-workflow-id: nfv-pr-review -->"
        def pages(path):
            if path.endswith("/comments"):
                return [
                    {"id": 1, "body": "Old generated report\n" + marker,
                     "user": {"login": "github-actions[bot]"}},
                    {"id": 2, "body": "Human discussion\n" + marker, "user": {"login": "contributor"}},
                    {"id": 3, "body": "Other bot guidance", "user": {"login": "github-actions[bot]"}},
                    {"id": 4, "body": "Legacy report\n<!-- gh-aw-agentic-workflow: NFV PR reviewer, run: x -->",
                     "user": {"login": "github-actions[bot]"}},
                ]
            return original(path)
        api.pages = pages
        signals = collect(event(), api)["signals"]
        self.assertNotIn("comment:1", [item["id"] for item in signals])
        self.assertIn("comment:2", [item["id"] for item in signals])
        self.assertIn("comment:3", [item["id"] for item in signals])
        self.assertNotIn("comment:4", [item["id"] for item in signals])

    def test_mutated_pr_snapshot_rejected(self):
        api = self.api()
        original = api.request
        def request(path):
            result = original(path)
            if "head" in result:
                result["head"]["sha"] = "f" * 40
            return result
        api.request = request
        with self.assertRaises(Abstain):
            collect(event(), api)

    def test_blob_hash_is_verified(self):
        raw = b"int x;"
        digest = hashlib.sha1(b"blob 6\0" + raw).hexdigest()
        api = GitHub(token="")
        api.request = lambda _: {"size": 6, "encoding": "base64",
                                 "content": base64.b64encode(raw).decode()}
        self.assertEqual(api.blob("owner/project", digest), "int x;")
        with self.assertRaises(Abstain):
            api.blob("owner/project", "a" * 40)

    def test_pagination_exhaustion_is_not_partial_success(self):
        api = GitHub(token="")
        api.request = lambda _: [{}] * 100
        with self.assertRaises(Abstain):
            api.pages("repos/owner/project/issues", max_pages=2)

    def test_truncated_source_inventory_abstains(self):
        api = self.api()
        original = api.request
        def request(path):
            result = original(path)
            if "/git/trees/" in path:
                result["truncated"] = True
            return result
        api.request = request
        with self.assertRaises(Abstain):
            collect(event(), api)


class TemplateTests(unittest.TestCase):
    template = Path(__file__).parents[2] / ".github" / "workflows"
    package = Path(__file__).parents[1]

    def source(self):
        return (self.template / "nfv-pr-review.md").read_text(encoding="utf-8")

    def lock(self):
        return (self.template / "nfv-pr-review.lock.yml").read_text(encoding="utf-8")

    def test_prototype_maturity_preserves_nfv_identifiers(self):
        self.assertIn("a prototype implementation", self.source())
        self.assertIn('name: "NFV PR reviewer"\n', self.lock())
        source = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("prototype", source)
        self.assertNotIn("Prototyp:", source)

    def test_aw_package_installs_the_template(self):
        manifest = (self.package / "aw.yml").read_text(encoding="utf-8")
        self.assertIn('manifest-version: "1"', manifest)
        self.assertIn("  - .github/workflows/nfv-pr-review.md\n", manifest)
        self.assertIn("  - .github/workflows/nfv-source-context.md\n", manifest)
        self.assertTrue((self.package / "README.md").exists())

    def test_reviewer_triggers_on_ready_non_draft_prs_only(self):
        source, lock = self.source(), self.lock()
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", source)
        self.assertIn("if: github.event.pull_request.draft == false", source)
        self.assertNotIn("slash_command", source)
        self.assertNotIn("pull_request_target", lock)
        self.assertNotIn("roles: all", source)
        self.assertFalse((self.template / "agentic_commands.yml").exists())

    def test_path_filters_skip_the_documentation_and_submodule_failure_cases(self):
        from nfv_workflow.repository_context import SUFFIXES
        source, lock = self.source(), self.lock()
        patterns = re.findall(r'^\s+- "(\*\*\.[^"]+)"$', source, re.MULTILINE)
        expected = {"**." + "".join(f"[{char}{char.upper()}]" for char in suffix[1:])
                    for suffix in SUFFIXES}
        self.assertEqual(set(patterns), expected)
        for pattern in patterns:
            self.assertIn('      - "' + pattern + '"', lock)
        for paths in (["doc/openvmm-upstream-roadmap.md", "openvmm"],
                      [".github/skills/nvx-openvmm-rebase/SKILL.md",
                       ".github/copilot-instructions.md", ".gitmodules"]):
            self.assertFalse(any(fnmatch.fnmatchcase(path, pattern)
                                 for path in paths for pattern in patterns))
        for path in ("main.py", "src/lib.rs", "include/header.hpp", "web/view.tsx",
                     "src/MAIN.C", "src/mixed.CpP", "pkg/HELPER.PY"):
            self.assertTrue(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns), path)

    def test_source_context_binds_files_only_hints_and_guards_artifact_handoff(self):
        source = (self.template / "nfv-source-context.md").read_text(encoding="utf-8")
        lock = (self.template / "nfv-source-context.lock.yml").read_text(encoding="utf-8")
        self.assertIn("index = context_input(bundle)", source)
        self.assertIn("--manifest nfv-context.json --hints-only", source)
        self.assertIn('{"files": [{"path":', source)
        self.assertNotIn('"copy":', source)
        handoff = source.split("    handoff-context:", 1)[1].split("\nsteps:", 1)[0]
        self.assertEqual(handoff.count("if: ${{ needs.agent.result == 'success' }}"), 2)
        compiled_handoff = lock.split("\n  handoff_context:", 1)[1].split("\n  pre_activation:", 1)[0]
        self.assertEqual(compiled_handoff.count("if: ${{ needs.agent.result == 'success' }}"), 2)
        post = source.split("post-steps:", 1)[1].split("\n---\n", 1)[0]
        self.assertIn("name: nfv-source-context-validated\n", post)
        self.assertNotIn("download-artifact", post)
        self.assertLess(post.index("nfv-workflow context-manifest"), post.index("upload-artifact"))

    def test_billing_defaults_preserve_pat_auth_and_allow_an_org_billed_caller(self):
        source, lock = self.source(), self.lock()
        worker = (self.template / "nfv-source-context.md").read_text(encoding="utf-8")
        self.assertIn("  copilot-requests: none\n", source)
        self.assertIn("      copilot-requests: none\n", source)
        self.assertIn("  copilot-requests: none\n", worker)
        self.assertIn("      COPILOT_GITHUB_TOKEN:\n        required: false\n", worker)
        for compiled in (lock, (self.template / "nfv-source-context.lock.yml").read_text(encoding="utf-8")):
            self.assertNotRegex(compiled, r"(?m)^\s+copilot-requests: write$")
            self.assertIn("secrets.COPILOT_GITHUB_TOKEN", compiled)

    def test_evaluation_is_owned_by_a_deterministic_sandboxed_sdk_driver(self):
        source, lock = self.source(), self.lock()
        self.assertNotIn("  nfv_submit:", lock)
        self.assertNotIn("discussions: write", source)
        frontmatter, prompt = source.split("\n---\n", 1)
        setup = frontmatter.split("post-steps:", 1)[0]
        self.assertIn("copilot-sdk: true", setup)
        self.assertIn("driver: nfv-plugin/workflow/sandbox_driver.cjs", setup)
        self.assertIn("GH_AW_COPILOT_SDK_DRIVER: 1", lock)
        self.assertIn('GH_AW_HARNESS_MAX_RETRIES: "0"', source)
        self.assertIn("GH_AW_HARNESS_MAX_RETRIES: 0", lock)
        self.assertIn("sandbox_driver.cjs", lock)
        self.assertNotIn("nfv-workflow extract", setup)
        self.assertNotIn("nfv-workflow audit", setup)
        self.assertIn("supervised foreground process", prompt)
        self.assertIn("There is no outer model session", prompt)
        self.assertIn('"./nfv-plugin[inference]"', source)
        self.assertNotIn("COPILOT_GITHUB_TOKEN:", frontmatter)
        self.assertNotIn('GITHUB_AW: "false"', source)
        self.assertNotIn("  bash:", source)
        self.assertNotIn("  edit:", source)
        self.assertIn("COPILOT_SDK_URI: http://127.0.0.1:3002", lock)
        self.assertNotIn("COPILOT_CLI_PATH=", source)
        self.assertIn('echo "NFV_PYTHON=$(command -v python)"', source)

    def test_report_is_posted_as_a_single_pr_comment(self):
        source = self.source()
        self.assertIn("add-comment:\n    max: 1\n    hide-older-comments: true\n", source)
        self.assertNotIn("pull-request-review", source)
        self.assertNotIn("APPROVE", source)
        self.assertIn("allowed: [defaults, arxiv.org]", source)

    def test_independent_sessions_have_a_bounded_cache_miss_allowance(self):
        source, lock = self.source(), self.lock()
        reviewer = lock.split("\n  detection:", 1)[0]
        self.assertIn("max-turns: 500\n", source)
        self.assertIn("max-turn-cache-misses: 500\n", source)
        self.assertIn(r'\"maxRuns\":500', reviewer)
        self.assertIn(r'\"maxCacheMisses\":500', reviewer)
        self.assertNotIn(r'\"maxCacheMisses\":5,', reviewer)
        self.assertIn(r'\"maxAiCredits\":${GH_AW_MAX_AI_CREDITS}', reviewer)

    def test_publication_uses_job_scoped_token_without_shared_override(self):
        for name in ("nfv-pr-review", "nfv-source-context"):
            with self.subTest(workflow=name):
                source = (self.template / f"{name}.md").read_text(encoding="utf-8")
                lock = (self.template / f"{name}.lock.yml").read_text(encoding="utf-8")
                self.assertIn("  github-token: ${{ secrets.GITHUB_TOKEN }}\n", source)
                outputs = lock.split("\n  safe_outputs:", 1)[1]
                process = outputs.split("      - name: Process Safe Outputs\n", 1)[1].split("\n      - ", 1)[0]
                self.assertIn("github-token: ${{ secrets.GITHUB_TOKEN }}", process)
                self.assertNotIn("GH_AW_GITHUB_TOKEN", process)
                self.assertNotIn("continue-on-error:", process)
                if name == "nfv-pr-review":
                    self.assertIn("      issues: write\n", outputs)
                    self.assertIn("      pull-requests: write\n", outputs)

    def test_trusted_inputs_are_read_only_in_the_sandbox(self):
        source, lock = self.source(), self.lock()
        self.assertIn('bundle="$RUNNER_TEMP/gh-aw/nfv/bundle.json"', source)
        self.assertIn('tool_dir="$RUNNER_TEMP/gh-aw/nfv-tools"', source)
        self.assertIn('--mount "${RUNNER_TEMP}/gh-aw:${RUNNER_TEMP}/gh-aw:ro"', lock)
        self.assertIn("ref: ${{ vars.NFV_RELEASE_SHA }}", lock)
        self.assertIn("persist-credentials: false", source)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha }}", lock)

    def test_driver_uses_writable_evidence_separate_from_read_only_inputs(self):
        source, lock = self.source(), self.lock()
        self.assertIn('Path("nfv-input.json").write_text', source)
        self.assertIn(r'Path(\"nfv-input.json\").write_text', lock)
        self.assertNotIn("nfv-comment.md", source)
        self.assertIn('echo "NFV_OUT=/tmp/gh-aw/sandbox/agent/logs/nfv"', source)
        prompt = source.split("\n---\n", 1)[1]
        self.assertIn("Only the deterministic driver submits", prompt)
        self.assertIn("Never edit, summarize, add to", prompt)
        self.assertIn("Incomplete extraction", prompt)

    def test_post_steps_only_retain_evidence_and_never_run_analysis(self):
        source = self.source()
        block = source.split("post-steps:", 1)[1].split("\n---\n", 1)[0]
        self.assertNotIn("run:", block)
        self.assertNotIn("nfv-workflow", block)
        self.assertIn("if: always()\n    uses: actions/upload-artifact@", source)
        self.assertIn("name: nfv-evidence", source)
        self.assertIn("path: /tmp/gh-aw/sandbox/agent/logs/nfv", block)
        prompt = source.split("\n---\n", 1)[1]
        self.assertIn("never fabricate a", prompt)
        self.assertIn("report or publish incremental evidence", prompt)

    def test_attempt_status_is_separate_from_completed_property_outcomes(self):
        prompt = self.source().split("\n---\n", 1)[1]
        self.assertIn("Completed analyses submit all four property outcomes", prompt)
        self.assertIn("property type-check/audit failures", prompt)
        self.assertIn("review-status.json", prompt)
        self.assertIn("never fabricates a completed", prompt)

    def test_extraction_targets_implementation_not_invented_solver_responses(self):
        intent = (Path(__file__).parents[1] / "src/nfv_workflow/intent_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Prefer changed implementation functions", intent)
        self.assertIn("Do not select a test/caller expectation", intent)
        self.assertIn("treating that callee's result as an unconstrained input", intent)
        self.assertIn("caller's own logic", intent)
        self.assertIn("Do not decompose an intention into atomic", intent)
        self.assertIn("There is NO later property-splitting stage", intent)
        prompt = (Path(__file__).parents[1] / "src/nfv_workflow/general_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Conjunctions and multiple ensures are permitted", " ".join(prompt.split()))
        self.assertIn("negates their complete conjunction", " ".join(prompt.split()))

    def test_verifier_uses_hash_pinned_archive_and_explicit_bundled_z3(self):
        source = self.source()
        self.assertIn("a46a9ff7cdd720f7955854c78e95df13f4cfe6b80691b05f8654fe19e8267179", source)
        self.assertIn("-name 'z3-4.12.1'", source)
        self.assertIn('echo "NFV_Z3_PATH=${solvers[0]}" >> "$GITHUB_ENV"', source)
        self.assertIn('echo "NFV_DAFNY_PATH=$dafny_dir/dafny" >> "$GITHUB_ENV"', source)
        self.assertNotIn("setup-dafny-action", source)

    def test_separate_source_only_session_precedes_analysis(self):
        source, lock = self.source(), self.lock()
        worker = (self.template / "nfv-source-context.md").read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/nfv-source-context.lock.yml", source)
        self.assertIn("needs: [repository_context]", source)
        self.assertIn("repository_context", lock.split("  agent:", 1)[1].split("steps:", 1)[0])
        self.assertIn("name: nfv-source-context", source)
        self.assertIn("nfv-workflow enrich", source)
        self.assertIn("--sources-dir nfv-src", source)
        self.assertIn("workflow_call:", worker)
        self.assertIn("fetch-depth: 1", worker)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", worker)
        self.assertNotIn("head.sha }}", worker)
        self.assertNotIn("  bash:", worker)
        self.assertIn("report-failure-as-issue: false", worker)
        worker_lock = (self.template / "nfv-source-context.lock.yml").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"(?m)^\s+(?:contents|issues|pull-requests): write$", worker_lock), [])
        self.assertNotIn('"create_issue"', worker_lock)
        self.assertIn("Do not execute repository code, build, test", worker)
        self.assertIn("submodules: false", worker)
        self.assertIn("lfs: false", worker)
        self.assertTrue((self.template / "nfv-source-context.lock.yml").exists())

    def test_source_context_initializes_workspace_before_generated_git_setup(self):
        source = (self.template / "nfv-source-context.md").read_text(encoding="utf-8")
        lock = (self.template / "nfv-source-context.lock.yml").read_text(encoding="utf-8")
        marker = "Initialize empty workspace repository (no PR code is checked out)"
        self.assertLess(source.index(marker), source.index("Capture immutable PR changes as data"))
        self.assertLess(lock.index(marker), lock.index("Configure Git credentials"))


class WorkspaceBootstrapTests(ScratchTest):
    def test_source_context_bootstrap_supports_local_git_without_checking_out_source(self):
        source = (TemplateTests.template / "nfv-source-context.md").read_text(encoding="utf-8")
        block = source.split("  - name: Initialize empty workspace repository", 1)[1]
        script = block.split("    run: |\n", 1)[1].split("  - name:", 1)[0]
        variables = {"GITHUB_WORKSPACE": str(self.directory.resolve()),
                     "GITHUB_SERVER_URL": "https://github.invalid", "GITHUB_REPOSITORY": "test/source"}
        for line in script.strip().splitlines():
            command = [Template(arg).substitute(variables) for arg in shlex.split(line.strip())]
            subprocess.run(command, check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.directory), "config", "--local", "nfv.bootstrap", "ok"],
                       check=True, capture_output=True, text=True)
        origin = subprocess.run(["git", "-C", str(self.directory), "remote", "get-url", "origin"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(origin.stdout.strip(), "https://github.invalid/test/source.git")
        tracked = subprocess.run(["git", "-C", str(self.directory), "ls-files"],
                                 check=True, capture_output=True, text=True)
        self.assertEqual(tracked.stdout, "")


class ExtractionInputTests(ScratchTest):
    def script(self):
        source = TemplateTests().source()
        block = source.split('      python - "$bundle" <<\'PY\'\n', 1)[1].split("\n      PY", 1)[0]
        return "\n".join(line[6:] for line in block.splitlines()) + "\n"

    def test_index_preserves_grounding_without_copying_source_or_environment(self):
        data = bundle()
        data["sources"]["src/f.cpp"]["text"] += "source-only marker\n" * 50000
        data["context_omissions"] = ["src/other.cpp: unavailable"]
        before = copy.deepcopy(data)
        captured = self.directory / "bundle.json"
        captured.write_text(json.dumps(data), encoding="utf-8")
        with patch.dict(os.environ, {"NFV_TEST_ENV_VALUE": "environment-only marker"}):
            result = subprocess.run([sys.executable, "-", str(captured.resolve())],
                                    input=self.script(), cwd=self.directory,
                                    capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        path = self.directory / "nfv-input.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual({"identity", "signals", "changed", "captured_paths", "context_omissions",
                          "repository_context", "function_catalog"}, set(index))
        for key in ("identity", "signals", "changed", "context_omissions"):
            self.assertEqual(data[key], index[key])
        self.assertEqual(sorted(data["sources"]), index["captured_paths"])
        self.assertLess(path.stat().st_size, 2000)
        self.assertNotIn("source-only marker", path.read_text(encoding="utf-8"))
        self.assertNotIn("environment-only marker", path.read_text(encoding="utf-8"))
        self.assertNotIn(b"\r\n", path.read_bytes())
        self.assertEqual(before, json.loads(captured.read_text(encoding="utf-8")))

    def test_missing_or_invalid_bundle_fails_without_empty_input(self):
        captured = self.directory / "bundle.json"
        for value in (None, "{invalid", json.dumps({"signals": []})):
            with self.subTest(value=value):
                if value is not None:
                    captured.write_text(value, encoding="utf-8")
                result = subprocess.run([sys.executable, "-", str(captured.resolve())],
                                        input=self.script(), cwd=self.directory,
                                        capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode)
                self.assertTrue(result.stderr)
                self.assertFalse((self.directory / "nfv-input.json").exists())


class AuditTests(ScratchTest):
    def test_infrastructure_errors_preserve_evidence_without_rendering_a_review(self):
        from unittest.mock import AsyncMock
        proposals, captured, out = (self.directory / name for name in ("p.json", "b.json", "out"))
        proposals.write_text("[]")
        captured.write_text(json.dumps(bundle()))
        for completed, errors in ((False, ["Model inference failed: HTTP 403"]),
                                  (False, []), (True, ["Dafny unavailable"])):
            report = {"completed": completed, "errors": errors}
            async def failed(*args):
                (out / "report.json").write_text(json.dumps(report))
                return report
            with self.subTest(completed=completed, errors=errors), \
                    patch.dict(os.environ, {"NFV_BUNDLE": str(captured), "NFV_OUT": str(out)}), \
                    patch("nfv_workflow.cli.run_general", new=AsyncMock(side_effect=failed)), \
                    patch("nfv_workflow.publish.comment") as render_comment:
                with self.assertRaisesRegex(Abstain, "NFV audit incomplete"):
                    audit(proposals, (6, 10, 60))
            render_comment.assert_not_called()
            self.assertEqual(json.loads((out / "report.json").read_text()), report)
            self.assertFalse((out / "comment.md").exists())

    def test_failed_restart_cannot_leave_stale_publishable_outputs(self):
        proposals, captured = self.directory / "p.json", self.directory / "b.json"
        proposals.write_text("[]")
        captured.write_text(json.dumps(bundle()))
        out = self.directory / "out"
        out.mkdir()
        names = ("comment.md", "report.json", "presentation.json", "presentation-diagnostics.json")
        for name in names:
            (out / name).write_text("stale output")
        with patch.dict(os.environ, {"NFV_BUNDLE": str(captured), "NFV_OUT": str(out)}), \
                patch("nfv_workflow.cli.run_general", side_effect=TimeoutError("client startup")):
            with self.assertRaises(TimeoutError):
                audit(proposals, (6, 10, 60))
        self.assertTrue(all(not (out / name).exists() for name in names))

    def test_requires_trusted_bundle(self):
        (self.directory / "p.json").write_text("[]")
        with patch.dict(os.environ, {"NFV_BUNDLE": "", "NFV_OUT": str(self.directory / "out")}):
            with self.assertRaises(Abstain):
                audit(self.directory / "p.json", (6, 10, 60))

    def test_no_properties_writes_no_review(self):
        (self.directory / "p.json").write_text("[]")
        (self.directory / "b.json").write_text(json.dumps(bundle()))
        out = self.directory / "out"
        with patch.dict(os.environ, {"NFV_BUNDLE": str(self.directory / "b.json"), "NFV_OUT": str(out),
                                     "GITHUB_REPOSITORY": "owner/project", "GITHUB_RUN_ID": "1"}):
            summary = audit(self.directory / "p.json", (6, 10, 60))
        self.assertEqual(summary["findings"], 0)
        body = Path(summary["comment"]).read_text(encoding="utf-8")
        self.assertIn("0 refuted, 0 verified", body)
        self.assertIn("resources exhausted: 0", body)
        self.assertIn("/actions/runs/1", body)
        self.assertTrue((out / "report.json").exists() or (out / "proposals.json").exists())

    def test_no_finding_comment_lists_formalized_properties(self):
        data = bundle()
        report = {"identity": data["identity"], "findings": [], "errors": [],
                  "abstentions": [{"id": "p1", "reason": "Abstain: no witness"}],
                  "properties": [{"id": "p1", "path": "src/f.cpp", "lines": [2], "quote": "never negative",
                                  "property": "result is never negative", "formalized": "result >= 0",
                                  "status": "abstained"}]}
        body = comment(report, data, "https://github.com/owner/project/actions/runs/1", 1)
        self.assertIn("## Intended properties checked", body)
        self.assertIn("**result is never negative** (`src/f.cpp:2`) \u2014 abstained", body)
        self.assertIn("Quoted claim: never negative", body)
        self.assertIn("```dafny\nensures result >= 0\n```", body)
        self.assertIn("p1: Abstain: no witness", body)

    def test_awf_provider_targets_the_api_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = awf_provider()
        self.assertEqual(provider, {"type": "openai", "wire_api": "responses",
                                    "base_url": "http://api-proxy:10002",
                                    "api_key": "dummy-byok-key-for-offline-mode"})
        with patch.dict(os.environ, {"COPILOT_API_URL": "http://172.30.0.30:10002"}, clear=True):
            self.assertEqual(awf_provider()["base_url"], "http://172.30.0.30:10002")
        with patch.dict(os.environ, {"COPILOT_PROVIDER_BASE_URL": "http://api-proxy:12345",
                                     "COPILOT_PROVIDER_TYPE": "anthropic"}, clear=True):
            self.assertEqual(awf_provider(), {"type": "anthropic", "base_url": "http://api-proxy:12345",
                                             "api_key": "dummy-byok-key-for-offline-mode"})
        with patch.dict(os.environ, {"COPILOT_PROVIDER_TYPE": "unknown"}, clear=True):
            with self.assertRaises(Abstain):
                awf_provider()

    def test_rejects_non_array_proposals(self):
        (self.directory / "p.json").write_text("{}")
        (self.directory / "b.json").write_text(json.dumps(bundle()))
        with patch.dict(os.environ, {"NFV_BUNDLE": str(self.directory / "b.json"),
                                     "NFV_OUT": str(self.directory / "out")}):
            with self.assertRaises(Abstain):
                audit(self.directory / "p.json", (6, 10, 60))

    def test_budget_comes_from_trusted_environment(self):
        from nfv_workflow.general import Limits
        self.assertEqual(audit_limits({}), Limits())
        self.assertEqual(audit_limits({"NFV_SYNTAX_ROUNDS": "", "NFV_PROOF_ROUNDS": " "}), Limits())
        limits = audit_limits({"NFV_SYNTAX_ROUNDS": "10", "NFV_PROOF_ROUNDS": "20",
                               "NFV_VERIFIER_TIMEOUT": "120", "NFV_PROPERTY_SECONDS": "2700"})
        self.assertEqual((limits.syntax_rounds, limits.proof_rounds, limits.verifier_timeout,
                          limits.entry_seconds, limits.model_timeout), (10, 20, 120, 2700, 240))
        for bad in ({"NFV_PROOF_ROUNDS": "31"}, {"NFV_SYNTAX_ROUNDS": "-1"},
                    {"NFV_VERIFIER_TIMEOUT": "0"}, {"NFV_PROPERTY_SECONDS": "1e3"}):
            with self.assertRaises(Abstain):
                audit_limits(bad)

    def test_workflow_exports_budget_variables(self):
        text = (Path(__file__).parents[2] / ".github" / "workflows" / "nfv-pr-review.md").read_text(encoding="utf-8")
        for name in ("NFV_SYNTAX_ROUNDS", "NFV_PROOF_ROUNDS", "NFV_VERIFIER_TIMEOUT", "NFV_PROPERTY_SECONDS"):
            self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", text)
            self.assertIn(f'echo "{name}=${name}"', text)

    def test_workflow_exports_independent_default_on_followup_flags(self):
        text = (Path(__file__).parents[2] / ".github" / "workflows" / "nfv-pr-review.md").read_text(encoding="utf-8")
        for name in ("NFV_SIMPLIFY_REFUTATIONS", "NFV_CHECK_FIXES"):
            self.assertIn(f"{name}: ${{{{ vars.{name} || 'true' }}}}", text)
            self.assertIn(f'echo "{name}=${name}"', text)


@unittest.skipUnless(shutil.which("dafny"), "Dafny integration requires installed Dafny")
class RealDafnyTests(ScratchTest):
    def test_source_bound_cross_file_leaf_invocation(self):
        data, obj = cross_file_fixture()
        report = check(data, [obj], self.directory)
        self.assertEqual(len(report["findings"]), 1, report)
        binding = report["findings"][0]["source_binding"]
        self.assertEqual(binding["result"], 1)
        contract = binding["source_derived_callee_contracts"][0]
        self.assertEqual(contract["declaration"], "src/helper.h")
        self.assertEqual(contract["definition"], "src/helper.cpp")
        self.assertEqual(contract["origin"], "source-derived-definition")

    def test_source_matched_same_file_callee(self):
        obj = candidate()
        obj["lines"] = [5]
        obj["model"]["body"] = {"call": ["helper", "x"]}
        obj["model"]["environment"] = [{
            "name": "helper", "parameters": [{"name": "x", "type": "int"}],
            "result_type": "int", "body": {"+": ["x", 1]},
            "source": {"path": "src/f.cpp", "lines": [1, 2, 3]},
            "description": "The preceding helper increments its scalar input."}]
        data = bundle()
        data["changed"]["src/f.cpp"] = [5]
        data["sources"]["src/f.cpp"]["text"] = (
            "int helper(int x) {\n  return x+1;\n}\n"
            "int f(int x) {\n  return helper(x);\n}\n")
        report = check(data, [obj], self.directory)
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["source_binding"]["result"], 1)

    def test_real_negative_check_and_minimization(self):
        obj = candidate()
        obj["model"]["assumptions"] = [
            {"description": "nonnegative x", "expression": {">=": ["x", 0]}}]
        report = check(bundle(), [obj], self.directory)
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["retained_assumptions"], [])

    def test_true_original_property_is_not_a_refutation(self):
        obj = candidate()
        obj["model"]["body"] = "x"
        report = check(bundle(), [obj], self.directory)
        self.assertFalse(report["findings"], report)
        self.assertEqual(report["abstentions"][0]["reason"], "failed")

    def test_contradictory_domain_cannot_refute_vacuously(self):
        obj = candidate()
        obj["model"]["preconditions"] = [
            {"description": "impossible input", "expression": {"<": ["x", 0]}}]
        self.assertFalse(check(bundle(), [obj], self.directory)["findings"])

    def test_division_by_zero_is_abstention(self):
        obj = candidate()
        obj["model"]["body"] = {"/": ["x", 0]}
        self.assertFalse(check(bundle(), [obj], self.directory)["findings"])

    def test_necessary_domain_assumption_is_retained(self):
        obj = candidate()
        obj["model"] = {
            "parameters": [{"name": "x", "type": "int"}, {"name": "y", "type": "int"}],
            "result_type": "int", "body": {"/": ["x", "y"]},
            "property": {"==": ["result", 0]}, "witness": {"x": 1, "y": 1},
            "assumptions": [{"description": "divisor is positive",
                             "expression": {">": ["y", 0]}}],
            "preconditions": [], "environment": []}
        data = bundle()
        data["sources"]["src/f.cpp"]["text"] = "int f(int x, int y) {\n  return x / y;\n}\n"
        report = check(data, [obj], self.directory)
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["retained_assumptions"], [0])


if __name__ == "__main__":
    unittest.main()
