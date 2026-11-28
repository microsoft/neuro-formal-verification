# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import copy
from dataclasses import asdict, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from nfv_workflow import benchmark, cli, sandbox
from nfv_workflow.followups import Options
from nfv_workflow.general import Limits, run, select_properties
from nfv_workflow.github import Abstain
from nfv_workflow.outcomes import EXHAUSTED, INVALID, REFUTED, VERIFIED, InferenceFailure
from test_general import Chat, fixture


ROOT = Path(__file__).parents[2]
RELEASE = "a" * 40
REPO = "owner/nfv"


class ProofVerifier:
    def __init__(self, positive=False, negative=True):
        self.positive, self.negative = positive, negative
        self.calls = []

    def resolve(self, source, directory):
        return {"outcome": "verified", "exit_code": 0, "mode": "typecheck"}

    def __call__(self, source, directory):
        self.calls.append(Path(directory).name)
        proved = self.positive if Path(directory).name == "verification" else self.negative
        return {"outcome": "verified" if proved else "failed", "exit_code": 0 if proved else 4,
                "mode": "unbounded", "stdout": "2 verified, 0 errors" if proved else "not proved",
                "stderr": ""}

    def run(self, source, directory, **kwargs):
        self.calls.append(Path(directory).name)
        return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}


class PublicationAPI:
    def __init__(self):
        self.enabled = True
        self.category = "General"
        self.discussions = []
        self.comments = {}
        self.created_at = {}
        self.mutations = []
        self.fail_dashboard = False

    def request(self, path):
        if "/actions/runs/" in path:
            return {"created_at": self.created_at.get(path.rsplit("/", 1)[1], "2026-11-13T03:00:00Z")}
        return {"has_discussions": self.enabled}

    def dashboard_body(self):
        dashboard = next(item for item in self.discussions
                         if f"<!-- nfv-benchmark-dashboard:{REPO} -->" in item["body"].splitlines())
        return self.comments[dashboard["id"]][-1]["body"]

    def graphql(self, query, variables):
        if "discussionCategories" in query:
            return {"repository": {"id": "repo-id", "discussionCategories": {
                "nodes": [{"id": "category-id", "name": self.category}]}}}
        if "discussions(" in query:
            return {"repository": {"discussions": {"nodes": self.discussions,
                                                    "pageInfo": {"hasNextPage": False}}}}
        if "comments(" in query:
            return {"node": {"comments": {"nodes": self.comments.get(variables["id"], []),
                                         "pageInfo": {"hasNextPage": False}}}}
        if self.fail_dashboard and (variables.get("title") == benchmark.DASHBOARD_TITLE
                                    or variables.get("id") == "discussion-2"):
            raise Abstain("Publication interrupted")
        self.mutations.append((query, variables))
        if "createDiscussion" in query:
            number = len(self.discussions) + 1
            discussion = {"id": f"discussion-{number}", "body": variables["body"],
                          "title": variables["title"],
                          "url": f"https://github.com/owner/nfv/discussions/{number}",
                          "author": {"login": "github-actions[bot]", "__typename": "Bot"}}
            self.discussions.append(discussion)
            return {"createDiscussion": {"discussion": discussion}}
        if "addDiscussionComment" in query:
            comments = self.comments.setdefault(variables["id"], [])
            comment = {"id": f"comment-{variables['id']}-{len(comments) + 1}",
                       "body": variables["body"],
                       "url": (f"https://github.com/owner/nfv/discussions/{variables['id'].rsplit('-', 1)[1]}"
                               f"#discussioncomment-{len(comments) + 1}"),
                       "author": {"login": "github-actions[bot]", "__typename": "Bot"}}
            comments.append(comment)
            return {"addDiscussionComment": {"comment": comment}}
        raise AssertionError(query)


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        cls.shared.mkdir(parents=True)
        cls.dataset = ROOT / "paper/data/nl2post_safe.json"
        cls.dataset_hash = hashlib.sha256(cls.dataset.read_bytes()).hexdigest()
        cls.prepared = cls.shared / "prepared"
        cls.run_info = benchmark.prepare(cls.dataset, cls.prepared, RELEASE, "test-model", REPO, "123", "1")
        cls.manifest, cls.inputs = benchmark.prepared_cases(cls.prepared)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared)

    def setUp(self):
        self.directory = self.shared / uuid.uuid4().hex
        self.directory.mkdir()
        self.environment = {"GITHUB_AW": "true", "AWF_REFLECT_ENABLED": "1",
                            "COPILOT_SDK_URI": "http://127.0.0.1:3002",
                            "COPILOT_CONNECTION_TOKEN": "per-run-test-token",
                            "COPILOT_PROVIDER_BASE_URL": "http://api-proxy:10002",
                            "COPILOT_PROVIDER_TYPE": "openai", "NFV_MODEL": "test-model"}
        self.payload = self.prepared / "shards/0.json"
        self.evidence = self.directory / "evidence"

    def tearDown(self):
        shutil.rmtree(self.directory)

    def abstention_report(self, case, status=INVALID):
        claim = select_properties(case["properties"], case["bundle"])[0]
        return {"schema": 3, "completed": True, "identity": case["bundle"]["identity"],
                "limits": self.run_info["limits"], "errors": [], "findings": [], "verified": [],
                "properties": [{"id": claim["id"], "status": status}],
                "failures": [{"id": claim["id"], "reason": status, "diagnostic": "test abstention"}]}

    def write_shard(self, shard, predictions=None):
        payload = cli.load(self.prepared / "shards" / f"{shard}.json")
        directory = self.evidence / f"shard-{shard}"
        result = {"schema": 1, "run": self.run_info, "shard": shard, "completed": True,
                  "errors": [], "rows": []}
        for case in payload["cases"]:
            label = (predictions or {}).get(case["id"], "abstain")
            status = {"correct": VERIFIED, "incorrect": REFUTED, "abstain": INVALID}[label]
            cli.save(directory / case["id"] / "input.json", case)
            cli.save(directory / case["id"] / "report.json", self.abstention_report(case, status))
            result["rows"].append({"id": case["id"], "prediction": label,
                                   "status": status, "elapsed_seconds": 1})
        cli.save(directory / "results.json", result)
        return directory

    def native_report(self, positive=False, negative=True, optional=False, real=False, invalid_metadata=False):
        bundle, _, translation = fixture("python")
        code = next(iter(bundle["sources"].values()))["text"]
        property_text = "assert return_value == n + 1" if positive else "assert return_value == n"
        case = benchmark.make_case({"code": code, "postcondition": property_text,
                                    "docstring": "Input n is nonnegative.", "entry_point": "f"},
                                   RELEASE, REPO)
        translation["model"] = translation["model"].replace("src/example.py", "subject.py")
        translation["required_context"] = ["subject.py"]
        for note in translation["source_notes"]:
            note["path"] = "subject.py"
        if positive:
            translation["model"] = translation["model"].replace("ensures r == n", "ensures r == n + 1")
        if optional:
            translation["model"] = translation["model"].replace(
                "// synth: modeled contract or structural syntax\n  ensures",
                "// synth: optional domain restriction\n  requires n <= 100\n"
                "// synth: modeled contract or structural syntax\n  ensures")
            translation["assumptions"].append({"clause": "n <= 100", "description": "Small inputs",
                                               "origin": "inferred", "optional": True})
        if real:
            from nfv_workflow.check import Dafny
            verifier = Dafny(30)
        else:
            verifier = ProofVerifier(positive, negative)
        if invalid_metadata:
            translation.pop("correspondence")
        with redirect_stdout(io.StringIO()):
            report = asyncio.run(run(case["bundle"], case["properties"], self.directory / uuid.uuid4().hex,
                                     Chat([translation]), Limits(proof_rounds=0, minimize_assumptions=False,
                                                                 repair_model_validation=not invalid_metadata),
                                     verifier))
        return case, report, verifier

    def backup_shard(self, shard, keep_primary=False):
        primary = self.write_shard(shard)
        backup = self.directory / "agent-backups" / f"{shard:08x}-agent" / "sandbox/agent/logs/nfv-bench"
        shutil.copytree(primary, backup)
        if not keep_primary:
            shutil.rmtree(primary)
        return backup

    def test_all_206_inputs_are_blinded_and_bound_to_whole_assertions(self):
        self.assertEqual(len(self.inputs), 206)
        self.assertEqual(self.run_info["limits"], asdict(replace(Limits(), minimize_assumptions=False)))
        self.assertEqual(self.run_info["model"], "test-model")
        source_entries = {entry["entry_id"]: entry for entry in cli.load(self.dataset)["entries"]}
        for record in self.manifest["cases"]:
            case = self.inputs[record["id"]]
            self.assertEqual(set(case), {"id", "bundle", "properties"})
            encoded = json.dumps(case)
            for leaked in ("__reference", "__mutant", '"code_type"', '"killing_test_present"', '"entry_id"', '"label"'):
                self.assertNotIn(leaked, encoded)
            entry = source_entries[record["entry_id"]]
            self.assertEqual(case["properties"][0]["property"], entry["postcondition"])
            self.assertIn(entry["docstring"], case["properties"][0]["quote"])
            self.assertEqual(case["bundle"]["sources"]["subject.py"]["text"], entry["code"])
            self.assertEqual(len(select_properties(case["properties"], case["bundle"])), 1)
            self.assertNotIn("repository_context", case["bundle"])
        self.assertEqual(hashlib.sha256(self.dataset.read_bytes()).hexdigest(), self.dataset_hash)
        self.assertEqual(len(cli.load(self.prepared / "shards/25.json")["cases"]), 6)

    def test_preparation_shuffles_class_leaking_dataset_order(self):
        with patch.object(benchmark.random.SystemRandom, "shuffle") as shuffle:
            benchmark.prepare(self.dataset, self.directory / "fresh", RELEASE, "test-model", REPO, 1, 1)
        shuffle.assert_called_once()
        self.assertEqual(len(shuffle.call_args.args[0]), 206)

    def test_five_entry_pilot_runs_exactly_five_cases_in_one_shard(self):
        self.prepared = self.directory / "pilot"
        self.run_info = benchmark.prepare(
            self.dataset, self.prepared, RELEASE, "test-model", REPO, 123, 1, count=5)
        manifest, inputs = benchmark.prepared_cases(self.prepared)
        self.assertEqual((self.run_info["total"], self.run_info["shards"]), (5, 1))
        self.assertEqual(len(manifest["cases"]), 5)
        self.assertEqual(len(inputs), 5)
        self.assertEqual(len(list((self.prepared / "shards").glob("*.json"))), 1)
        self.assertNotIn('"label"', json.dumps(list(inputs.values())))
        self.assertEqual(hashlib.sha256(self.dataset.read_bytes()).hexdigest(), self.dataset_hash)
        payload = self.prepared / "shards/0.json"
        output = self.evidence / "shard-0"

        def fake_audit(path, limits, options, *, render_report):
            case = cli.load(Path(path).parent / "input.json")
            cli.save(Path(path).parent / "report.json", self.abstention_report(case))

        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(benchmark, "audit", side_effect=fake_audit) as audit, \
                patch.object(sandbox, "submit"), redirect_stdout(io.StringIO()):
            result = benchmark.worker(payload, output)
        self.assertEqual(audit.call_count, 5)
        self.assertTrue(result["completed"])
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        self.assertTrue(summary["completed"])
        self.assertEqual((summary["evaluated"], summary["not_evaluated"]), (5, 0))
        rendered = benchmark.render(summary)
        self.assertIn("Complete five-entry pilot", rendered)
        self.assertIn("Evaluated **5/5**", rendered)
        self.assertNotIn("/206", rendered)
        self.assertIn("not a full-dataset accuracy estimate", rendered)
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        self.assertIn("five-entry pilot", api.comments["discussion-1"][0]["body"])
        with self.assertRaises(Abstain):
            benchmark.select(self.prepared, 1, self.directory / "invalid.json",
                             RELEASE, "test-model", "123-1")

    def test_pilot_accuracy_and_missing_results_use_five_as_denominator(self):
        self.prepared = self.directory / "pilot"
        self.run_info = benchmark.prepare(
            self.dataset, self.prepared, RELEASE, "test-model", REPO, 123, 1, count=5)
        manifest, _ = benchmark.prepared_cases(self.prepared)
        missing = benchmark.summarize(self.prepared, self.evidence, self.directory / "missing")
        self.assertFalse(missing["completed"])
        self.assertEqual(missing["not_evaluated"], 5)
        self.assertEqual(len(missing["errors"]), 1)
        predictions = {case["id"]: case["label"] if index < 3 else "abstain"
                       for index, case in enumerate(manifest["cases"])}
        self.write_shard(0, predictions)
        with patch.object(benchmark, "prediction", side_effect=lambda report, case: predictions[case["id"]]):
            summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["accuracy"], 3 / 5)
        self.assertEqual(summary["coverage"], 3 / 5)
        self.assertEqual(summary["answered_accuracy"], 1)
        self.assertIn("(3/5)", benchmark.render(summary))

    def test_cli_preparation_defaults_to_full_sweep_and_emits_pilot_matrix(self):
        for entries, total, shards in ((None, 206, 26), ("5", 5, 1)):
            with self.subTest(entries=entries):
                prepared = self.directory / f"prepared-{total}"
                outputs = self.directory / f"outputs-{total}"
                args = ["prepare", "--dataset", str(self.dataset), "--out", str(prepared),
                        "--release-sha", RELEASE, "--model", "test-model", "--repository", REPO,
                        "--run-id", "123", "--attempt", "1"]
                if entries is not None:
                    args += ["--entries", entries]
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(benchmark.main(args), 0)
                manifest, _ = benchmark.prepared_cases(prepared)
                self.assertEqual(manifest["run"]["total"], total)
                emitted = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
                self.assertEqual(json.loads(emitted["matrix"]), {"shard": list(range(shards))})

    def test_invalid_pilot_sizes_and_tampered_dimensions_are_rejected(self):
        for count in (0, 1, 6, 205, 207, True, "5"):
            with self.subTest(count=count), self.assertRaises(Abstain):
                benchmark.prepare(self.dataset, self.directory / "invalid", RELEASE,
                                  "test-model", REPO, 1, 1, count=count)
        prepared = self.directory / "pilot"
        benchmark.prepare(self.dataset, prepared, RELEASE, "test-model", REPO, 1, 1, count=5)
        original = cli.load(prepared / "manifest.json")
        for mutate in (lambda item: item["run"].update(shards=26),
                       lambda item: item["run"].update(total=6),
                       lambda item: item["cases"][0].update(label="unknown"),
                       lambda item: item["cases"].append(copy.deepcopy(item["cases"][0]))):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            cli.save(prepared / "manifest.json", manifest)
            with self.subTest(mutate=mutate), self.assertRaises(Abstain):
                benchmark.prepared_cases(prepared)

    def test_invalid_configuration_fails_before_preparing_cases(self):
        for limits, model, release in ((Limits(entry_seconds=3600), "test-model", RELEASE),
                                       (Limits(), "", RELEASE), (Limits(), "bad\nmodel", RELEASE),
                                       (Limits(), "test-model", "dev")):
            with self.subTest(limits=limits, model=model, release=release), self.assertRaises(Abstain):
                benchmark.prepare(self.dataset, self.directory / "invalid", release, model, REPO, 1, 1, limits)
        with self.assertRaises(ValueError):
            Limits(minimize_assumptions="false")
        self.assertTrue(Limits().minimize_assumptions)

    def test_select_rejects_configuration_drift_and_does_not_copy_labels(self):
        selected = self.directory / "selected.json"
        benchmark.select(self.prepared, 0, selected, RELEASE, "test-model", "123-1")
        self.assertEqual(cli.load(selected), cli.load(self.payload))
        self.assertNotIn('"label"', selected.read_text(encoding="utf-8"))
        with self.assertRaises(Abstain):
            benchmark.select(self.prepared, 0, selected, RELEASE, "other-model", "123-1")
        with self.assertRaises(Abstain):
            benchmark.select(self.prepared, 26, selected, RELEASE, "test-model", "123-1")

    def test_validation_repair_policy_is_frozen_and_used_by_worker(self):
        prepared = self.directory / "no-validation-repair"
        with patch.dict(os.environ, {"NFV_REPAIR_MODEL_VALIDATION": "false"}):
            benchmark.prepare(self.dataset, prepared, RELEASE, "test-model", REPO, 123, 1, count=5)
        manifest, _ = benchmark.prepared_cases(prepared)
        self.assertIs(manifest["run"]["limits"]["repair_model_validation"], False)
        with self.assertRaises(ValueError):
            Limits(repair_model_validation="false")
        seen = []
        def fake_audit(path, limits, options, *, render_report):
            seen.append(limits.repair_model_validation)
            case = cli.load(Path(path).with_name("input.json"))
            report = self.abstention_report(case)
            report["limits"] = manifest["run"]["limits"]
            report["properties"][0]["experimental_search"] = {
                "accepted": False, "search_outcome": EXHAUSTED,
                "validation_diagnostic": "Missing source correspondence",
                "evidence": "experimental-search.json",
            }
            cli.save(Path(path).with_name("report.json"), report)
        with patch.dict(os.environ, {**self.environment, "NFV_REPAIR_MODEL_VALIDATION": "true"}, clear=True), \
                patch.object(benchmark, "audit", side_effect=fake_audit), \
                patch.object(sandbox, "submit"):
            result = benchmark.worker(prepared / "shards" / "0.json", self.evidence)
        self.assertTrue(result["completed"])
        self.assertEqual(seen, [False] * 5)
        summary = benchmark.summarize(prepared, self.evidence, self.directory / "stats")
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["decisive"], 0)
        self.assertEqual(summary["accuracy"], 0)
        self.assertEqual(summary["coverage"], 0)
        self.assertEqual(summary["experimental_search"], {
            "accepted": False, "entries": 5, "outcomes": {EXHAUSTED: 5},
            "included_in_decisive_predictions": True,
        })
        self.assertIn("Unvalidated experimental search", benchmark.render(summary))
        self.assertIn("included in validation-disabled benchmark predictions", benchmark.render(summary))
        self.assertEqual(summary["scoring_mode"], "validation-disabled")
        result["rows"][0]["experimental_search"]["validation_diagnostic"] = "changed diagnostic"
        cli.save(self.evidence / "results.json", result)
        with self.assertRaisesRegex(Abstain, "experimental search disagrees"):
            benchmark.summarize(prepared, self.evidence, self.directory / "tampered-stats")

    def test_prepared_input_tampering_is_rejected(self):
        copied = self.directory / "copied"
        shutil.copytree(self.prepared, copied)
        path = copied / "shards/0.json"
        payload = cli.load(path)
        payload["cases"][0]["properties"][0]["property"] = "assert True"
        cli.save(path, payload)
        with self.assertRaisesRegex(Abstain, "hash"):
            benchmark.prepared_cases(copied)

    def test_verification_short_circuits_refutation(self):
        case, report, verifier = self.native_report(positive=True)
        self.assertEqual(benchmark.prediction(report, case), "correct")
        self.assertEqual(verifier.calls, ["verification", "input-domain"])
        self.assertFalse(report["findings"])

    def test_refutation_follows_failed_verification_without_any_minimization(self):
        case, report, verifier = self.native_report(optional=True)
        self.assertEqual(benchmark.prediction(report, case), "incorrect")
        finding = report["findings"][0]
        self.assertEqual(finding["retained_assumptions"], [0, 1])
        self.assertEqual(finding["minimization"], [])
        self.assertFalse(any(call.startswith("remove-") for call in verifier.calls))
        self.assertEqual(verifier.calls[0], "verification")
        self.assertIn("proof-0", verifier.calls)
        self.assertIn("reach-final", verifier.calls)

    def test_neither_proof_produces_abstention_not_incorrect(self):
        case, report, _ = self.native_report(negative=False)
        self.assertEqual(benchmark.prediction(report, case), "abstain")
        self.assertEqual(report["properties"][0]["status"], EXHAUSTED)
        self.assertFalse(report["findings"])

    def test_experimental_success_is_not_an_accepted_prediction(self):
        for positive in (True, False):
            with self.subTest(positive=positive):
                case, report, verifier = self.native_report(positive=positive, invalid_metadata=True)
                self.assertEqual(benchmark.prediction(report, case), "abstain")
                self.assertEqual(report["properties"][0]["status"], INVALID)
                self.assertEqual(report["properties"][0]["experimental_search"]["search_outcome"],
                                 "verified" if positive else "refuted")
                self.assertIn("verification", verifier.calls)
                self.assertFalse(report["findings"])
                self.assertFalse(report["verified"])

    def test_validation_disabled_scoring_includes_only_checked_frozen_searches(self):
        for positive in (True, False):
            with self.subTest(positive=positive):
                case, report, _ = self.native_report(positive=positive, invalid_metadata=True)
                claim_id = report["properties"][0]["id"]
                artifact = next(path for path in self.directory.rglob("experimental-search.json")
                                if cli.load(path)["candidate"]["id"] == claim_id
                                and cli.load(path)["search_outcome"] == ("verified" if positive else "refuted"))
                directory = artifact.parent.parent
                self.assertEqual(benchmark.scored_prediction(report, case, directory),
                                 "correct" if positive else "incorrect")
                original = cli.load(artifact)
                for mutate in (
                        lambda item: item["result"].update(exit_code=4),
                        lambda item: item.update(dafny=item["dafny"] + "\nassume false;"),
                        lambda item: item["translation"].update(model=item["translation"]["model"] + "\n"),
                        lambda item: item.update(candidate={}),
                        lambda item: item.update(accepted=True)):
                    evidence = copy.deepcopy(original)
                    mutate(evidence)
                    cli.save(artifact, evidence)
                    with self.assertRaises(Abstain):
                        benchmark.scored_prediction(report, case, directory)
                cli.save(artifact, original)
                with patch.object(benchmark, "prediction", return_value="abstain"):
                    report["limits"]["repair_model_validation"] = True
                    self.assertEqual(benchmark.scored_prediction(report, case, directory), "abstain")

    def test_completed_full_run_selection_includes_validation_disabled_scoring(self):
        summary = {"completed": True, "evaluated": 206, "errors": [],
                   "publication_run_created_at": "2026-11-28T00:00:00Z",
                   "run": {**self.run_info, "total": 206}, "scoring_mode": "validation-disabled"}
        self.assertEqual(len(benchmark.completed_full_records([(summary, "https://example.invalid")])), 1)
        summary.pop("scoring_mode")
        self.assertEqual(len(benchmark.completed_full_records([(summary, "https://example.invalid")])), 1)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_real_dafny_accepts_both_branches_with_assumption_minimization_disabled(self):
        for positive in (True, False):
            with self.subTest(positive=positive):
                case, report, _ = self.native_report(positive=positive, optional=True, real=True)
                self.assertEqual(benchmark.prediction(report, case),
                                 "correct" if positive else "incorrect", report)
                if not positive:
                    self.assertEqual(report["findings"][0]["retained_assumptions"], [0, 1])
                    self.assertEqual(report["findings"][0]["minimization"], [])

    def test_changed_proof_or_property_is_not_a_decisive_prediction(self):
        case, original, _ = self.native_report(positive=True)
        for mutate in (
                lambda report: report["verified"][0]["result"].update(exit_code=4),
                lambda report: report["verified"][0]["input_domain_result"].update(outcome="verified"),
                lambda report: report["verified"][0]["candidate"].update(property="assert True"),
                lambda report: report.update(completed=False)):
            report = copy.deepcopy(original)
            mutate(report)
            with self.subTest(mutate=mutate), self.assertRaises(Abstain):
                benchmark.prediction(report, case)

    def test_audit_statistics_mode_skips_report_presentation_inference(self):
        case, report, _ = self.native_report()
        directory = self.directory / "audit"
        cli.save(self.directory / "bundle.json", case["bundle"])
        cli.save(self.directory / "properties.json", case["properties"])
        with patch.dict(os.environ, {"NFV_BUNDLE": str(self.directory / "bundle.json"),
                                     "NFV_OUT": str(directory), "NFV_MODEL": "test-model"}), \
                patch.object(cli, "run_general", new=AsyncMock(return_value=report)), \
                patch.object(cli, "refresh_presentation") as present:
            result = cli.audit(self.directory / "properties.json", Limits(),
                               Options(False, False), render_report=False)
        self.assertEqual(result["outcomes"][REFUTED], 1)
        present.assert_not_called()
        self.assertNotIn("comment", result)
        self.assertFalse((directory / "comment.md").exists())

    def test_worker_checkpoints_each_case_and_uses_shared_audit_with_disabled_followups(self):
        def fake_audit(path, limits, options, *, render_report):
            self.assertFalse(limits.minimize_assumptions)
            self.assertFalse(options.simplify)
            self.assertFalse(options.fixes)
            self.assertFalse(render_report)
            directory = Path(path).parent
            case = cli.load(directory / "input.json")
            cli.save(directory / "report.json", self.abstention_report(case))
        output = self.directory / "worker"
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(benchmark, "audit", side_effect=fake_audit) as audit, \
                patch.object(sandbox, "submit") as submit, redirect_stdout(io.StringIO()):
            result = benchmark.worker(self.payload, output)
            self.assertEqual(audit.call_count, 8)
            with self.assertRaises(FileExistsError):
                benchmark.worker(self.payload, output)
        self.assertTrue(result["completed"])
        self.assertEqual(len(result["rows"]), 8)
        self.assertEqual(cli.load(output / "results.json"), result)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[1], "benchmark_checkpoint")

    def test_worker_inference_failure_keeps_partial_evidence_and_stops_the_shard(self):
        output = self.directory / "worker"
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(benchmark, "audit", side_effect=InferenceFailure("Provider access denied")) as audit, \
                patch.object(sandbox, "submit") as submit, self.assertRaises(InferenceFailure):
            benchmark.worker(self.payload, output)
        self.assertEqual(audit.call_count, 1)
        result = cli.load(output / "results.json")
        self.assertFalse(result["completed"])
        self.assertEqual(result["rows"], [])
        self.assertIn("Provider access denied", result["errors"][0])
        submit.assert_not_called()

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_worker_malformed_proof_repair_abstains_and_runs_next_case(self):
        case, native, _ = self.native_report()
        translation = copy.deepcopy(native["findings"][0]["translation"])
        translation["model"] = translation["model"].replace(
            "// synth: modeled contract or structural syntax\n    invariant r == i\n", "")
        later = {**case, "id": case["id"][:-1] + ("0" if case["id"][-1] != "0" else "1")}
        limits = Limits(proof_rounds=1, minimize_assumptions=False)
        payload = self.directory / "two-cases.json"
        cli.save(payload, {"run": {**self.run_info, "limits": asdict(limits)},
                           "shard": 0, "cases": [case, later]})
        def malformed(prompt):
            request = json.loads(prompt)
            after = next(int(line.split(":")[0]) for line in request["frozen_model_lines"]
                         if "while i < n" in line)
            return {"annotations": [{"after": after, "text": "invariant i == 0 ? r == 0 : r == i"}]}
        chat = Chat([translation, malformed, {"abstain": "next benchmark case reached translation"}])
        async def offline_provider(bundle, proposals, directory, model, actual_limits, **kwargs):
            return await run(bundle, proposals, directory, chat, actual_limits)
        environment = {**self.environment, **{key: os.environ[key] for key in (
            "PATH", "SYSTEMROOT", "USERPROFILE", "NFV_DAFNY_PATH", "NFV_Z3_PATH",
            "PYTHONPATH") if key in os.environ}}
        output = self.directory / "worker"
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(cli, "run_general", side_effect=offline_provider) as provider, \
                patch.object(sandbox, "submit") as submit, redirect_stdout(io.StringIO()):
            result = benchmark.worker(payload, output)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["errors"], [])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual([row["prediction"] for row in result["rows"]], ["abstain", "abstain"])
        self.assertIn("Refutation model did not type-check",
                      cli.load(output / case["id"] / "report.json")["failures"][0]["diagnostic"])
        self.assertIn("next benchmark case reached translation",
                      cli.load(output / later["id"] / "report.json")["failures"][0]["diagnostic"])
        submit.assert_called_once()

    def test_worker_rejects_raw_platform_credentials(self):
        with patch.dict(os.environ, {**self.environment, "GH_TOKEN": "not-a-real-credential"}, clear=True), \
                patch.object(benchmark, "audit") as audit, self.assertRaises(Abstain):
            benchmark.worker(self.payload, self.directory / "worker")
        audit.assert_not_called()

    def test_complete_all_abstain_sweep_has_all_206_in_the_denominator(self):
        for shard in range(26):
            self.write_shard(shard)
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["evaluated"], 206)
        self.assertEqual(summary["accuracy"], 0)
        self.assertEqual(summary["coverage"], 0)
        self.assertIsNone(summary["answered_accuracy"])
        self.assertIsNone(summary["recorded_usage"])
        self.assertEqual(summary["matrix"]["correct"]["abstain"], 103)
        self.assertEqual(summary["matrix"]["incorrect"]["abstain"], 103)
        self.assertIn("N/A", benchmark.render(summary))
        interrupted = benchmark.summarize(self.prepared, self.evidence, self.directory / "interrupted",
                                         workers_succeeded=False)
        self.assertFalse(interrupted["completed"])
        self.assertEqual(interrupted["evaluated"], 206)
        self.assertIn("handoff", interrupted["errors"][0])

    def test_accuracy_coverage_and_confusion_matrix_use_supplied_labels(self):
        predictions = {}
        for index, case in enumerate(self.manifest["cases"]):
            predictions[case["id"]] = (case["label"] if index < 100 else
                                       ("incorrect" if case["label"] == "correct" else "correct")
                                       if index < 150 else "abstain")
        for shard in range(26):
            self.write_shard(shard, predictions)
        with patch.object(benchmark, "prediction", side_effect=lambda report, case: predictions[case["id"]]):
            summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["matched"], 100)
        self.assertEqual(summary["decisive"], 150)
        self.assertEqual(summary["accuracy"], 100 / 206)
        self.assertEqual(summary["coverage"], 150 / 206)
        self.assertEqual(summary["answered_accuracy"], 100 / 150)
        self.assertEqual(summary["predictions"]["abstain"], 56)

    def test_missing_shards_publish_explicit_partial_statistics(self):
        self.write_shard(0)
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["evaluated"], 8)
        self.assertEqual(summary["not_evaluated"], 198)
        self.assertEqual(len(summary["rows"]), 206)
        self.assertEqual(summary["abstention_reasons"]["not evaluated"], 198)
        self.assertIn("PARTIAL", benchmark.render(summary))
        self.assertEqual(summary["predictions"]["abstain"], 206)

    def test_foreign_duplicate_or_falsely_decisive_results_fail_scoring(self):
        directory = self.write_shard(0)
        path = directory / "results.json"
        original = cli.load(path)
        for mutate in (lambda result: result["run"].update(model="other-model"),
                       lambda result: result["rows"].append(copy.deepcopy(result["rows"][0])),
                       lambda result: result["rows"][0].update(prediction="incorrect")):
            result = copy.deepcopy(original)
            mutate(result)
            cli.save(path, result)
            with self.subTest(mutate=mutate), self.assertRaises(Abstain):
                benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")

    def test_backup_checkpoints_recover_all_206_without_inference(self):
        for shard in range(26):
            self.backup_shard(shard)
        with patch.object(benchmark, "audit") as audit:
            summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                          fallback=self.directory / "agent-backups")
        audit.assert_not_called()
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["evaluated"], 206)
        self.assertEqual(summary["not_evaluated"], 0)
        self.assertEqual(summary["recovered_shards"], list(range(26)))
        self.assertIn("26 shard checkpoints", benchmark.render(summary))
        interrupted = benchmark.summarize(self.prepared, self.evidence, self.directory / "interrupted",
                                         workers_succeeded=False, fallback=self.directory / "agent-backups")
        self.assertFalse(interrupted["completed"])
        self.assertEqual(interrupted["evaluated"], 206)
        self.assertIn("handoff", interrupted["errors"][0])

    def test_identical_primary_and_backup_are_not_counted_twice(self):
        backup = self.backup_shard(0, keep_primary=True)
        primary = self.evidence / "shard-0"
        key = cli.load(primary / "results.json")["rows"][0]["id"]
        for directory in (primary, backup):
            cli.save(directory / key / "model-transcript.json", [{"api_calls": 2}])
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                      fallback=self.directory / "agent-backups")
        self.assertEqual(summary["evaluated"], 8)
        self.assertEqual(summary["recorded_responses"], 1)
        self.assertEqual(summary["recorded_usage"]["api_calls"], 2)
        self.assertNotIn("recovered_shards", summary)

    def test_conflicting_primary_and_backup_fail_scoring(self):
        backup = self.backup_shard(0, keep_primary=True)
        path = backup / "results.json"
        result = cli.load(path)
        result["rows"][0]["elapsed_seconds"] = 2
        cli.save(path, result)
        with self.assertRaisesRegex(Abstain, "Conflicting"):
            benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                fallback=self.directory / "agent-backups")

    def test_matching_checkpoints_do_not_hide_changed_backup_audit(self):
        backup = self.backup_shard(0, keep_primary=True)
        key = cli.load(backup / "results.json")["rows"][0]["id"]
        path = backup / key / "report.json"
        report = cli.load(path)
        report["failures"][0]["diagnostic"] = "changed"
        cli.save(path, report)
        with self.assertRaisesRegex(Abstain, "Conflicting"):
            benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                fallback=self.directory / "agent-backups")

    def test_foreign_backup_is_rejected_even_with_a_valid_primary(self):
        backup = self.backup_shard(0, keep_primary=True)
        path = backup / "results.json"
        result = cli.load(path)
        result["run"]["model"] = "foreign-model"
        cli.save(path, result)
        with self.assertRaisesRegex(Abstain, "Foreign"):
            benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                fallback=self.directory / "agent-backups")

    def test_backup_inputs_budgets_and_proofs_still_require_native_acceptance(self):
        backup = self.backup_shard(0)
        result_path = backup / "results.json"
        result = cli.load(result_path)
        key = result["rows"][0]["id"]
        input_path, report_path = backup / key / "input.json", backup / key / "report.json"
        original_input, original_report = cli.load(input_path), cli.load(report_path)
        for field in ("input", "budget", "proof"):
            value, report = copy.deepcopy(original_input), copy.deepcopy(original_report)
            changed_result = copy.deepcopy(result)
            if field == "input":
                value["properties"][0]["property"] = "assert True"
            elif field == "budget":
                report["limits"]["proof_rounds"] = 0
            else:
                report["properties"][0]["status"] = VERIFIED
                changed_result["rows"][0].update(status=VERIFIED, prediction="correct")
            cli.save(input_path, value)
            cli.save(report_path, report)
            cli.save(result_path, changed_result)
            with self.subTest(field=field), self.assertRaises(Abstain):
                benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                    fallback=self.directory / "agent-backups")

    def test_incomplete_backup_stays_partial_and_missing_entries_are_not_invented(self):
        backup = self.backup_shard(0)
        result = cli.load(backup / "results.json")
        result["completed"] = False
        result["rows"].pop()
        cli.save(backup / "results.json", result)
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                      fallback=self.directory / "agent-backups")
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["evaluated"], 7)
        self.assertEqual(summary["not_evaluated"], 199)
        self.assertIn("Shard 0 is incomplete", summary["errors"])

    def test_duplicate_backups_and_primary_checkpoints_are_rejected(self):
        backup = self.backup_shard(0)
        duplicate = self.directory / "agent-backups" / "duplicate-agent" / "sandbox/agent/logs/nfv-bench"
        shutil.copytree(backup, duplicate)
        with self.assertRaisesRegex(Abstain, "Duplicate"):
            benchmark.summarize(self.prepared, self.evidence, self.directory / "stats",
                                fallback=self.directory / "agent-backups")
        primary = self.write_shard(0)
        shutil.copytree(primary, self.evidence / "duplicate")
        with self.assertRaisesRegex(Abstain, "duplicate"):
            benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")

    def test_missing_backups_stay_partial_and_cli_wires_fallback(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "empty",
                                      fallback=self.directory / "absent")
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["evaluated"], 0)
        self.backup_shard(0)
        with redirect_stdout(io.StringIO()):
            status = benchmark.main(["summarize", "--prepared", str(self.prepared),
                                     "--evidence", str(self.evidence), "--fallback",
                                     str(self.directory / "agent-backups"), "--out",
                                     str(self.directory / "cli")])
        self.assertEqual(status, 0)
        self.assertEqual(cli.load(self.directory / "cli/summary.json")["evaluated"], 8)

    def test_unreported_usage_is_unavailable_not_zero(self):
        directory = self.write_shard(0)
        key = cli.load(directory / "results.json")["rows"][0]["id"]
        cli.save(directory / key / "model-transcript.json", [{"api_calls": 1, "input_tokens": 4}])
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        self.assertEqual(summary["recorded_usage"]["api_calls"], 1)
        self.assertIsNone(summary["recorded_usage"]["nano_aiu"])
        self.assertIn("nano_aiu=unavailable", benchmark.render(summary))

    def write_credit_usage(self, shard=0, inference=(1.25, 2.5), detection=(0.5,)):
        directory = self.directory / "usage" / f"{shard:08x}-usage"
        for stage, values in (("agent", inference), ("detection", detection)):
            path = directory / stage / "token_usage.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps({
                "event": "token_usage", "request_id": f"{shard}-{stage}-{index}",
                "ai_credits_this_response": value, "ai_credits_total": 999999,
            }) + "\n" for index, value in enumerate(values)), encoding="utf-8")
        return directory

    def test_credit_accounting_sums_unique_responses_not_cumulative_or_cache_values(self):
        self.write_credit_usage()
        cli.save(self.directory / "usage" / "aic-usage-cache" / "cache.json", {"aic": 999999})
        credits = benchmark.credit_usage(self.directory / "usage", 1)
        self.assertTrue(credits["complete"])
        self.assertEqual(credits["inference"], 3.75)
        self.assertEqual(credits["threat_detection"], 0.5)
        self.assertEqual(credits["total"], 4.25)
        self.write_credit_usage(1, (10,), (2,))
        self.assertEqual(benchmark.credit_usage(self.directory / "usage", 2)["total"], 16.25)
        with self.assertRaises(Abstain):
            benchmark.credit_usage(self.directory / "usage", 1)

    def test_single_downloaded_usage_artifact_can_be_flattened(self):
        artifact = self.write_credit_usage()
        expected = benchmark.credit_usage(artifact.parent, 1)
        usage = artifact.parent
        for stage in ("agent", "detection"):
            shutil.move(str(artifact / stage), str(usage / stage))
        artifact.rmdir()
        self.assertEqual(benchmark.credit_usage(usage, 1), expected)
        partial = benchmark.credit_usage(usage, 2)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["shards_observed"], 1)
        self.assertIsNone(partial["total"])
        (usage / "detection" / "token_usage.jsonl").unlink()
        self.assertIsNone(benchmark.credit_usage(usage, 1)["threat_detection"])
        self.write_credit_usage(1)
        with self.assertRaisesRegex(Abstain, "Mixed flattened and named"):
            benchmark.credit_usage(usage, 2)

    def test_missing_credit_fields_and_shards_are_unavailable_not_zero(self):
        self.write_credit_usage(inference=(None,))
        credits = benchmark.credit_usage(self.directory / "usage", 1)
        self.assertFalse(credits["complete"])
        self.assertIsNone(credits["inference"])
        self.assertIsNone(credits["total"])
        self.assertEqual(credits["threat_detection"], 0.5)
        self.assertEqual(credits["recorded_subtotal"], 0.5)
        credits = benchmark.credit_usage(self.directory / "usage", 26)
        self.assertFalse(credits["complete"])
        self.assertIsNone(credits["threat_detection"])
        self.assertEqual(credits["shards_observed"], 1)
        credits = benchmark.credit_usage(self.directory / "absent", 26)
        self.assertIsNone(credits["total"])

    def test_existing_empty_ledger_records_no_requests_but_missing_file_is_unknown(self):
        directory = self.write_credit_usage(detection=())
        credits = benchmark.credit_usage(self.directory / "usage", 1)
        self.assertTrue(credits["complete"])
        self.assertEqual(credits["threat_detection"], 0)
        self.assertEqual(credits["total"], 3.75)
        (directory / "detection" / "token_usage.jsonl").unlink()
        credits = benchmark.credit_usage(self.directory / "usage", 1)
        self.assertFalse(credits["complete"])
        self.assertIsNone(credits["threat_detection"])
        self.assertIsNone(credits["total"])

    def test_duplicate_and_invalid_credit_records_fail_explicitly(self):
        for value in (-1, True, "2", float("nan"), float("inf")):
            self.write_credit_usage(inference=(value,))
            with self.subTest(value=value), self.assertRaises(Abstain):
                benchmark.credit_usage(self.directory / "usage", 1)
        directory = self.write_credit_usage()
        shutil.copytree(directory, directory.with_name("ffffffff-usage"))
        with self.assertRaisesRegex(Abstain, "duplicate"):
            benchmark.credit_usage(self.directory / "usage", 2)

    def test_accounting_refresh_preserves_partial_verdicts_and_original_summary(self):
        original = benchmark.summarize(self.prepared, self.evidence, self.directory / "original")
        path = self.directory / "original" / "summary.json"
        original_bytes = path.read_bytes()
        for shard in range(26):
            self.write_credit_usage(shard)
        with redirect_stdout(io.StringIO()):
            status = benchmark.main(["account", "--summary", str(path),
                                     "--usage", str(self.directory / "usage"),
                                     "--out", str(self.directory / "accounted")])
        self.assertEqual(status, 0)
        updated = cli.load(self.directory / "accounted" / "summary.json")
        self.assertEqual({key: value for key, value in updated.items() if key != "ai_credits"}, original)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(updated["ai_credits"]["total"], 110.5)
        rendered = (self.directory / "accounted" / "report.md").read_text(encoding="utf-8")
        self.assertIn("PARTIAL sweep", rendered)
        self.assertIn("Recorded AI credits (AIC; not an invoice)", rendered)
        self.assertIn("total=110.50000", rendered)
        provenance = cli.load(self.directory / "accounted" / "accounting-source.json")
        self.assertEqual(provenance["summary_sha256"], hashlib.sha256(original_bytes).hexdigest())
        updated["ai_credits"]["complete"] = False
        updated["ai_credits"]["total"] = None
        self.assertIn("Missing credits are unavailable, not zero", benchmark.render(updated))

    def test_publication_replay_reuses_immutable_archive_and_dashboard_comments(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        first = benchmark.publish(summary, api, REPO, "General")
        api.discussions[0]["author"]["login"] = "github-actions"
        original = copy.deepcopy((api.discussions, api.comments))
        writes = len(api.mutations)
        second = benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(first, second)
        self.assertEqual(len(api.discussions), 2)
        self.assertEqual(len(api.comments["discussion-1"]), 1)
        self.assertIn("createDiscussion", api.mutations[0][0])
        self.assertEqual(len(api.mutations), writes)
        self.assertEqual((api.discussions, api.comments), original)
        self.assertEqual(len(api.comments["discussion-2"]), 1)
        self.assertTrue(all("updateDiscussion" not in query for query, _ in api.mutations))
        self.assertIn("PARTIAL sweep", api.comments["discussion-1"][0]["body"])
        saved = benchmark.saved_statistics(api.comments["discussion-1"][0], REPO)
        self.assertEqual(saved["run"]["release_sha"], RELEASE)
        self.assertNotIn("rows", saved)
        for field in ("matrix", "predictions", "abstention_reasons", "errors",
                      "accuracy", "answered_accuracy", "coverage", "finished_at", "recorded_usage"):
            self.assertEqual(saved[field], summary[field])
        self.assertIn("**Verdict counts:** correct=0, incorrect=0, abstain=206.",
                      api.comments["discussion-1"][0]["body"])
        self.assertIn("**Operational failures:**", api.comments["discussion-1"][0]["body"])
        dashboard = api.dashboard_body()
        self.assertIn("No completed full run", dashboard)
        self.assertIn(f"[{RELEASE[:10]}](https://github.com/{REPO}/commit/{RELEASE})", dashboard)
        self.assertIn("NFV SHA", dashboard)

    def test_dashboard_and_card_show_latest_full_run_without_scoring_tag(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary.update(completed=True, evaluated=206, not_evaluated=0, errors=[],
                       accuracy=137 / 206, answered_accuracy=137 / 143, coverage=143 / 206,
                       scoring_mode="validation-disabled")
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        saved = benchmark.saved_statistics(api.comments["discussion-1"][0], REPO)
        original = copy.deepcopy((api.discussions, api.comments, api.mutations))
        benchmark.publish(saved, api, REPO, "General")
        self.assertEqual((api.discussions, api.comments, api.mutations), original)
        dashboard = api.dashboard_body()
        latest = dashboard.split("## Recent runs")[0]
        self.assertIn("| Complete (206) | 66.50% | 95.80% | 69.42% |", latest)
        self.assertNotIn("validation-disabled", dashboard)
        target = self.directory / "benchmark.svg"
        benchmark.benchmark_card(api, REPO, target)
        self.assertIn("Accuracy: 66.50%", target.read_text())
        self.assertIn("Precision (decisive accuracy): 95.80%", target.read_text())

    def test_dashboard_refresh_template_is_publication_only(self):
        source = (ROOT / ".github/workflows/nfv-benchmark-pages.yml").read_text(encoding="utf-8")
        refresh = source.split("\n  refresh_dashboard:", 1)[1].split("\n  build:", 1)[0]
        self.assertIn("github.event_name == 'workflow_dispatch'", refresh)
        self.assertIn("inputs.refresh_dashboard", refresh)
        self.assertIn("discussions: write", refresh)
        self.assertIn('GH_TOKEN: ${{ github.token }}', refresh)
        self.assertIn("archived_statistics(api, repo)", refresh)
        self.assertIn('publish(selected_records(records)[0][0], api, repo, "General")', refresh)
        self.assertNotIn("COPILOT", refresh)
        self.assertNotIn("deploy-pages", refresh)
        self.assertIn("!inputs.refresh_dashboard", source.split("\n  build:", 1)[1])

    def test_dashboard_precision_matches_archived_decisive_accuracy(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        cases = [
            (206, True, 68, 1, 82, 9, "nightly benchmark", "72.82%", "93.75%"),
            (206, True, 64, 0, 78, 8, "nightly benchmark", "68.93%", "94.67%"),
            (5, True, 1, 1, 2, 1, "five-entry pilot", "60.00%", "60.00%"),
            (206, False, 0, 0, 0, 1, "nightly benchmark", "0.00%", "0.00%"),
            (206, True, 1, 0, 0, 0, "nightly benchmark", "0.49%", "100.00%"),
            (206, True, 0, 0, 0, 0, "nightly benchmark", "0.00%", "N/A"),
        ]
        for total, completed, tn, fn, tp, fp, kind, accuracy, precision in cases:
            with self.subTest(total=total, completed=completed, precision=precision):
                summary["run"]["total"] = total
                summary["completed"] = completed
                summary["matrix"] = {
                    "correct": {"correct": tn, "incorrect": fp, "abstain": 0},
                    "incorrect": {"correct": fn, "incorrect": tp, "abstain": total - tn - fn - tp - fp},
                }
                summary["accuracy"] = (tn + tp) / total
                decisive = tn + fn + tp + fp
                summary["coverage"] = decisive / total
                summary["answered_accuracy"] = (tn + tp) / decisive if decisive else None
                api = PublicationAPI()
                benchmark.publish(summary, api, REPO, "General")
                body = api.comments["discussion-1"][0]["body"]
                self.assertIn(
                    "**Accuracy among decisive results:** "
                    + ("N/A." if precision == "N/A" else f"{precision}."), body)
                self.assertIn(f"| {accuracy} | {precision} |", api.dashboard_body())

    def test_card_renders_archived_metrics_safely_without_writes_or_inference(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary.update(completed=True, evaluated=206, not_evaluated=0, errors=[],
                       accuracy=150 / 206, answered_accuracy=150 / 160, coverage=160 / 206)
        summary["run"]["model"] = 'model <script>alert("x")</script> & "test"'
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        original = copy.deepcopy((api.discussions, api.comments, api.mutations))
        target = self.directory / "site" / "benchmark.svg"
        with patch.object(benchmark, "GitHub", return_value=api), redirect_stdout(io.StringIO()):
            self.assertEqual(benchmark.main(["card", "--repository", REPO, "--out", str(target)]), 0)
        first = target.read_bytes()
        benchmark.benchmark_card(api, REPO, target)
        self.assertEqual(target.read_bytes(), first)
        root = ElementTree.fromstring(first)
        labels = " ".join(root.itertext())
        self.assertIn("2026-11-13 UTC", labels)
        self.assertIn(summary["run"]["model"], labels)
        self.assertIn("Accuracy: 72.82%", labels)
        self.assertIn("Precision (decisive accuracy): 93.75%", labels)
        self.assertNotIn("Coverage", labels)
        displayed = root.findall("{http://www.w3.org/2000/svg}text")
        self.assertEqual(len(displayed), 3)
        self.assertEqual(displayed[-1].text,
                         "Accuracy: 72.82%    Precision (decisive accuracy): 93.75%")
        self.assertNotIn("Model-conditional", labels)
        self.assertEqual(root.attrib["viewBox"], "0 0 800 112")
        self.assertEqual(len(root.findall(".//{http://www.w3.org/2000/svg}script")), 0)
        self.assertIn("&lt;script&gt;", first.decode())
        self.assertEqual((api.discussions, api.comments, api.mutations), original)

    def test_card_preserves_full_success_across_pilots_failures_attempts_and_replays(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary.update(completed=True, evaluated=206, not_evaluated=0, errors=[])
        summary["run"]["sweep_id"] = "123-2"
        summary["run"]["model"] = "retained-full-success"
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        target = self.directory / "benchmark.svg"
        benchmark.benchmark_card(api, REPO, target)
        original = target.read_bytes()
        for run_id, attempt, total, completed in (
                ("123", 1, 206, True), ("123", 3, 206, False),
                ("124", 1, 5, True), ("125", 1, 206, False),
                ("122", 1, 206, True)):
            newer = copy.deepcopy(summary)
            newer["run"].update(run_id=run_id, sweep_id=f"{run_id}-{attempt}",
                                total=total, model="must-not-replace")
            newer.update(completed=completed, evaluated=total)
            api.created_at[run_id] = (
                "2026-11-01T00:00:00Z" if run_id == "122" else
                "2026-11-13T03:00:00Z" if run_id == "123" else "2026-11-14T00:00:00Z")
            benchmark.publish(newer, api, REPO, "General")
            benchmark.benchmark_card(api, REPO, target)
            self.assertEqual(target.read_bytes(), original)
        refreshed = copy.deepcopy(summary)
        refreshed["run"].update(sweep_id="123-4", model="new-success")
        benchmark.publish(refreshed, api, REPO, "General")
        benchmark.publish(summary, api, REPO, "General")
        benchmark.benchmark_card(api, REPO, target)
        self.assertIn("new-success", target.read_text())
        self.assertIn("new-success", api.dashboard_body().split("## Recent runs")[0])

    def test_card_handles_no_decisive_results_and_fails_closed_on_missing_or_invalid_archives(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        target = self.directory / "benchmark.svg"
        target.write_text("last deployed card")
        for total, completed in ((206, False), (5, True)):
            summary["run"]["total"] = total
            summary["completed"] = completed
            summary["evaluated"] = total
            benchmark.publish(summary, api, REPO, "General")
            with self.assertRaisesRegex(Abstain, "No completed full"):
                benchmark.benchmark_card(api, REPO, target)
            self.assertEqual(target.read_text(), "last deployed card")
        summary["run"]["total"] = 206
        summary.update(completed=True, evaluated=206, errors=[])
        benchmark.publish(summary, api, REPO, "General")
        benchmark.benchmark_card(api, REPO, target)
        self.assertIn("Precision (decisive accuracy): N/A", target.read_text())
        original = target.read_bytes()
        api.comments["discussion-1"][-1]["author"] = {"login": "human", "__typename": "User"}
        with self.assertRaisesRegex(Abstain, "non-Actions"):
            benchmark.benchmark_card(api, REPO, target)
        self.assertEqual(target.read_bytes(), original)

    def test_pages_workflow_is_default_branch_only_and_separates_deployment_privileges(self):
        workflow = (ROOT / ".github/workflows/nfv-benchmark-pages.yml").read_text(encoding="utf-8")
        build, deploy = workflow.split("\n  deploy:", 1)
        self.assertIn('workflows: ["NFV nightly micro-benchmark"]', build)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", build)
        self.assertIn("github.ref == 'refs/heads/dev'", build)
        self.assertIn("vars.NFV_BENCH_PAGES_ENABLED == 'true'", build)
        self.assertIn("ref: dev", build)
        self.assertIn("persist-credentials: false", build)
        self.assertIn('card --repository "$GITHUB_REPOSITORY" --out _site/benchmark.svg', build)
        self.assertIn("discussions: read", build)
        self.assertNotIn("pages: write", build)
        self.assertNotIn("id-token: write", build)
        self.assertIn("needs: build", deploy)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)
        self.assertIn("name: github-pages", deploy)
        self.assertNotIn("checkout", deploy)
        self.assertNotIn("pull_request", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("contents: write", workflow)
        for action in re.findall(r"uses: ([^\s]+)", workflow):
            self.assertRegex(action, r"^actions/[\w-]+@[0-9a-f]{40}$")
        result = subprocess.run(
            [sys.executable, "-S", "-m", "nfv_workflow.benchmark", "card", "--help"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "workflow" / "src")},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_publication_preserves_custom_titles_and_bodies_and_appends_dashboard(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        archive, dashboard = api.discussions
        self.assertEqual(dashboard["title"], "Nightly nfv-micro-bench evaluation dashboard")
        self.assertEqual(archive["title"], "Nightly nfv-micro-bench evaluation archive - November 2026")
        archive["title"] = "Custom monthly archive"
        dashboard["title"] = "Custom pinned dashboard"
        original = copy.deepcopy(api.discussions)
        summary["run"] = {**summary["run"], "run_id": "124", "sweep_id": "124-1"}
        api.created_at["124"] = "2026-11-14T03:00:00Z"
        benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.discussions), 2)
        self.assertEqual(archive["title"], "Custom monthly archive")
        self.assertEqual(dashboard["title"], "Custom pinned dashboard")
        self.assertEqual(api.discussions, original)
        self.assertIn("/actions/runs/124", api.dashboard_body())
        self.assertEqual(len(api.comments[archive["id"]]), 2)
        self.assertEqual(len(api.comments[dashboard["id"]]), 2)
        self.assertTrue(all("updateDiscussion" not in query for query, _ in api.mutations))

    def test_dashboard_links_exact_revision_and_keeps_incomplete_credits_unavailable(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary["ai_credits"] = {"complete": False, "total": None, "inference": 1.25,
                                "threat_detection": None, "shards_observed": 1,
                                "shards_expected": 26, "recorded_subtotal": 1.25}
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        self.assertIn("| unavailable | test-model |", api.dashboard_body())
        self.assertEqual(benchmark.saved_statistics(api.comments["discussion-1"][0], REPO)["ai_credits"],
                         summary["ai_credits"])
        summary["ai_credits"].update(complete=True, total=3.75, threat_detection=2.5)
        benchmark.publish(summary, api, REPO, "General")
        self.assertIn("| 3.75000 | test-model |", api.dashboard_body())
        self.assertIn(RELEASE, api.comments["discussion-1"][0]["body"])
        self.assertEqual(len(api.comments["discussion-1"]), 2)

    def test_archive_index_survives_year_boundary_and_backfills_cannot_replace_latest(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary.update(completed=True, evaluated=206, not_evaluated=0, errors=[])
        api = PublicationAPI()

        def publish(run_id, date, total=206, completed=True):
            run = {**summary["run"], "run_id": run_id, "sweep_id": f"{run_id}-1",
                   "total": total, "release_sha": run_id[-1] * 40}
            current = {**summary, "run": run, "completed": completed, "evaluated": total}
            api.created_at[run_id] = date
            benchmark.publish(current, api, REPO, "General")

        publish("123", "2026-11-30T23:59:59Z")
        publish("124", "2026-12-01T00:00:00Z")
        publish("125", "2027-01-01T00:00:00Z")
        publish("126", "2027-01-02T00:00:00Z", total=5)
        publish("127", "2027-01-03T00:00:00Z", completed=False)
        publish("122", "2026-11-01T00:00:00Z")
        dashboard = api.dashboard_body()
        latest = dashboard.split("## Recent runs")[0]
        self.assertIn("5555555555", latest)
        self.assertNotIn("6666666666", latest)
        self.assertNotIn("7777777777", latest)
        self.assertNotIn("2222222222", latest)
        for month in ("2026-11", "2026-12", "2027-01"):
            self.assertIn(f"[{month}]", dashboard)
        self.assertEqual(len(api.discussions), 4)
        self.assertEqual(sum(map(len, api.comments.values())), 12)

    def test_run_creation_utc_month_is_stable_across_retry_and_offset(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        api.created_at["123"] = "2026-12-01T00:30:00+02:00"
        benchmark.publish(summary, api, REPO, "General")
        retry = copy.deepcopy(summary)
        retry["run"].update(started_at="2027-01-01T00:00:00Z", sweep_id="123-2")
        benchmark.publish(retry, api, REPO, "General")
        self.assertIn("November 2026", api.discussions[0]["title"])
        self.assertEqual(len(api.comments["discussion-1"]), 2)
        self.assertIn("[2026-11-30]", api.dashboard_body())

    def test_old_attempt_and_partial_retry_do_not_downgrade_archived_success(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary.update(completed=True, evaluated=206, not_evaluated=0, errors=[])
        summary["run"] = {**summary["run"], "sweep_id": "123-2"}
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        original = api.comments["discussion-1"][0]["body"]
        for attempt, completed in (("123-1", True), ("123-3", False)):
            retry = copy.deepcopy(summary)
            retry["run"]["sweep_id"] = attempt
            retry.update(completed=completed, accuracy=0.99)
            benchmark.publish(retry, api, REPO, "General")
            self.assertEqual(api.comments["discussion-1"][0]["body"], original)
        self.assertEqual(len(api.comments["discussion-1"]), 3)
        latest = api.dashboard_body().split("## Recent runs")[0]
        self.assertIn(api.comments["discussion-1"][0]["url"], latest)
        self.assertNotIn("99.00%", latest)

    def test_archive_survives_dashboard_failure_and_retry_deduplicates(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        api.fail_dashboard = True
        with self.assertRaisesRegex(Abstain, "interrupted"):
            benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.comments["discussion-1"]), 1)
        self.assertIn("matrix", benchmark.saved_statistics(api.comments["discussion-1"][0], REPO))
        api.fail_dashboard = False
        benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.discussions), 2)
        self.assertEqual(len(api.comments["discussion-1"]), 1)

    def test_interrupted_dashboard_append_reuses_archive_and_preserves_previous_snapshot(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        original = copy.deepcopy((api.discussions, api.comments["discussion-2"]))
        summary["run"] = {**summary["run"], "run_id": "124", "sweep_id": "124-1"}
        api.fail_dashboard = True
        with self.assertRaisesRegex(Abstain, "interrupted"):
            benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.comments["discussion-1"]), 2)
        self.assertEqual((api.discussions, api.comments["discussion-2"]), original)
        api.fail_dashboard = False
        benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.comments["discussion-1"]), 2)
        self.assertEqual(len(api.comments["discussion-2"]), 2)
        writes = len(api.mutations)
        benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.mutations), writes)

    def test_rescoring_clock_changes_and_legacy_archive_replay_do_not_append(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        comment = api.comments["discussion-1"][0]
        comment["body"] = "\n".join(line for line in comment["body"].splitlines()
                                   if not line.startswith("<!-- nfv-benchmark-attempt:")) + "\n"
        original = copy.deepcopy(api.comments)
        summary.update(finished_at="2026-12-01T00:00:00Z", elapsed_seconds=100000)
        writes = len(api.mutations)
        benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(api.comments, original)
        self.assertEqual(len(api.mutations), writes)

    def test_new_successful_attempt_wins_but_replaying_older_statistics_does_not(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        summary.update(completed=True, evaluated=206, not_evaluated=0, errors=[])
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        newer = copy.deepcopy(summary)
        newer["run"]["sweep_id"] = "123-2"
        newer["accuracy"] = 0.75
        benchmark.publish(newer, api, REPO, "General")
        comment = api.comments["discussion-1"][1]
        self.assertIn("Publication attempt: `123-2`", comment["body"])
        for current in (summary, newer):
            benchmark.publish(current, api, REPO, "General")
            latest = api.dashboard_body().split("## Recent runs")[0]
            self.assertIn(comment["url"], latest)
            self.assertIn("75.00%", latest)
        self.assertEqual(len(api.comments["discussion-1"]), 2)
        recent = api.dashboard_body().split("## Recent runs")[1].split("## Monthly archives")[0]
        self.assertEqual(recent.count("/actions/runs/123"), 1)

    def test_tampered_or_duplicate_attempt_statistics_fail_without_writes(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        comment = api.comments["discussion-1"][0]
        original = comment["body"]
        comment["body"] = original.replace(f"{REPO}:123-1:", f"{REPO}:123-2:")
        writes = len(api.mutations)
        with self.assertRaisesRegex(Abstain, "attempt marker"):
            benchmark.publish(summary, api, REPO, "General")
        comment["body"] = original
        api.comments["discussion-1"].append(copy.deepcopy(comment))
        with self.assertRaisesRegex(Abstain, "Duplicate benchmark attempt"):
            benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.mutations), writes)

    def test_legacy_reports_are_linked_but_never_rewritten(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        legacy = {"id": "old", "body": benchmark.render(summary), "title": "Old statistics",
                  "url": "https://github.com/owner/nfv/discussions/99",
                  "author": {"login": "github-actions", "__typename": "Bot"}}
        api.discussions.append(copy.deepcopy(legacy))
        benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(api.discussions[0], legacy)
        self.assertIn("[Old statistics]", api.dashboard_body())

    def test_human_archive_comment_marker_and_duplicate_dashboard_are_rejected(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        benchmark.publish(summary, api, REPO, "General")
        api.comments["discussion-1"][0]["author"] = {"login": "human", "__typename": "User"}
        writes = len(api.mutations)
        with self.assertRaisesRegex(Abstain, "non-Actions"):
            benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.mutations), writes)
        api.comments["discussion-1"][0]["author"] = {"login": "github-actions", "__typename": "Bot"}
        snapshot = api.comments["discussion-2"][0]
        snapshot["author"] = {"login": "human", "__typename": "User"}
        with self.assertRaisesRegex(Abstain, "non-Actions"):
            benchmark.publish(summary, api, REPO, "General")
        snapshot["author"] = {"login": "github-actions", "__typename": "Bot"}
        api.comments["discussion-2"].append(copy.deepcopy(snapshot))
        with self.assertRaisesRegex(Abstain, "Duplicate"):
            benchmark.publish(summary, api, REPO, "General")
        api.comments["discussion-2"].pop()
        api.discussions.append(copy.deepcopy(api.discussions[1]))
        with self.assertRaisesRegex(Abstain, "Duplicate"):
            benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(len(api.mutations), writes)

    def test_invalid_run_attempt_fails_without_publication(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        for attempt in ("124-1", "123-0", "123-x"):
            summary["run"]["sweep_id"] = attempt
            with self.subTest(attempt=attempt), self.assertRaisesRegex(Abstain, "run attempt"):
                benchmark.publish(summary, api, REPO, "General")
            self.assertFalse(api.mutations)

    def test_publication_pagination_reads_all_pages_and_fails_closed_at_limit(self):
        class Pages:
            def graphql(self, query, variables):
                cursor = variables["cursor"]
                return {"repository": {"discussions": {
                    "nodes": [cursor], "pageInfo": {"hasNextPage": cursor is None, "endCursor": "next"}}}}
        self.assertEqual(benchmark.publication_pages(Pages(), "", {}, "discussions"), [None, "next"])
        class Endless:
            def graphql(self, query, variables):
                return {"node": {"comments": {"nodes": [], "pageInfo": {
                    "hasNextPage": True, "endCursor": "next"}}}}
        with self.assertRaisesRegex(Abstain, "pagination exhausted"):
            benchmark.publication_pages(Endless(), "", {}, "comments", "node")

    def test_unavailable_discussions_or_category_fail_without_writes(self):
        for enabled, category in ((False, "General"), (True, "missing")):
            api = PublicationAPI()
            api.enabled = enabled
            with self.subTest(enabled=enabled, category=category), self.assertRaises(Abstain):
                benchmark.publication_target(api, REPO, category)
            self.assertFalse(api.mutations)

    def test_publisher_does_not_overwrite_human_discussions(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        api.discussions = [{"id": "human-id", "body": benchmark.render(summary),
                            "author": {"login": "human"}}]
        with self.assertRaises(Abstain):
            benchmark.publish(summary, api, REPO, "General")
        self.assertFalse(api.mutations)
        api.discussions[0]["author"] = {"login": "github-actions", "__typename": "User"}
        with self.assertRaises(Abstain):
            benchmark.publish(summary, api, REPO, "General")
        self.assertFalse(api.mutations)

    def test_cli_errors_are_explicit_and_nonzero(self):
        error = io.StringIO()
        with redirect_stderr(error):
            status = benchmark.main(["summarize", "--prepared", str(self.directory / "missing"),
                                     "--evidence", str(self.evidence), "--out", str(self.directory / "stats")])
        self.assertEqual(status, 1)
        self.assertIn("failed", error.getvalue())

    def test_standalone_templates_preserve_personal_billing_and_bound_shard_concurrency(self):
        coordinator = (ROOT / ".github/workflows/nfv-micro-bench.yml").read_text(encoding="utf-8")
        worker = (ROOT / ".github/workflows/nfv-micro-bench-worker.md").read_text(encoding="utf-8")
        for source in (coordinator, worker):
            self.assertIn("persist-credentials: false", source)
        self.assertIn("GH_TOKEN: ${{ github.token }}", coordinator)
        self.assertIn("COPILOT_GITHUB_TOKEN is required; no organization-billing fallback", coordinator)
        cron = re.search(r'cron: "(\d+) (\d+) \* \* \*"', coordinator)
        self.assertIsNotNone(cron)
        documentation = (ROOT / "workflow/MAINTAINER.md").read_text(encoding="utf-8")
        self.assertIn(f"**{int(cron[2]):02}:{int(cron[1]):02} UTC daily**", documentation)
        self.assertIn("vars.NFV_BENCH_ENABLED == 'true'", coordinator)
        self.assertIn("max-parallel: 10", coordinator)
        self.assertIn("fail-fast: false", coordinator)
        self.assertIn("fromJSON(needs.prepare.outputs.matrix)", coordinator)
        self.assertIn('commits/dev', coordinator)
        self.assertIn('release_sha="$WORKFLOW_SHA"', coordinator)
        self.assertIn("WORKFLOW_SHA: ${{ github.sha }}", coordinator)
        self.assertIn("options: [\"206\", \"5\"]", coordinator)
        self.assertIn("inputs.entries || '206'", coordinator)
        self.assertIn('--entries "$NFV_BENCH_ENTRIES"', coordinator)
        self.assertIn('options: ["repository", "true", "false"]', coordinator)
        self.assertIn("inputs.repair_model_validation != 'repository'", coordinator)
        self.assertIn("inputs.repair_model_validation || vars.NFV_REPAIR_MODEL_VALIDATION", coordinator)
        self.assertIn('pattern: "*-usage"', coordinator)
        self.assertIn("--usage usage", coordinator)
        self.assertIn('--fallback agent-backups', coordinator)
        self.assertIn('pattern: "*-agent"', coordinator)
        self.assertIn("merge-multiple: false", coordinator)
        self.assertIn('https://www.githubstatus.com/api/v2/summary.json', coordinator)
        self.assertIn('[.components[] | select(.name == "Actions") | .status] == ["operational"]', coordinator)
        self.assertLess(coordinator.index("Require healthy GitHub Actions"),
                        coordinator.index("Prepare label-blinded cases"))
        self.assertIn("steps.retain_statistics.outcome == 'failure'", coordinator)
        self.assertNotIn("pull_request", coordinator)
        self.assertIn("copilot-requests: none", coordinator)
        self.assertIn("discussions: write", coordinator)
        self.assertIn("inputs.sweep_id }}-${{ inputs.shard", worker)
        self.assertIn("model: ${{ inputs.model }}", worker)
        self.assertNotIn("nfv-source-context", worker)
        self.assertIn("NFV_CHECK_FIXES=false", worker)
        self.assertIn("NFV_SIMPLIFY_REFUTATIONS=false", worker)
        self.assertIn("copilot-requests: none", worker)
        self.assertNotIn("discussions: write", worker)
        self.assertIn("benchmark-checkpoint:", worker)
        self.assertIn("continue-on-error: true", worker)
        self.assertEqual(worker.count("timeout-minutes: 5"), 2)
        self.assertIn("steps.retain_benchmark_evidence.outcome == 'failure'", worker)
        self.assertIn('$(dirname "$GH_AW_AGENT_OUTPUT")/sandbox/agent/logs/nfv-bench/results.json', worker)
        self.assertIn(".run.sweep_id == $sweep and .shard == $shard", worker)
        compiled = (ROOT / ".github/workflows/nfv-micro-bench-worker.lock.yml").read_text(encoding="utf-8")
        self.assertIn("Retry benchmark evidence upload without rerunning inference", compiled)
        self.assertIn("id: retain_benchmark_evidence", compiled)
        agent = compiled.split("\n  agent:", 1)[1]
        group = next(line.strip() for line in agent.splitlines() if line.strip().startswith("group:"))
        self.assertEqual(group, 'group: "nfv-bench-inference-${{ inputs.sweep_id }}-${{ inputs.shard }}"')
        self.assertIn("toJSON(inputs)", compiled)
        self.assertNotIn("issues: write", compiled)
        self.assertNotIn('"tools":["create_issue"', compiled)
        package = (ROOT / "workflow/aw.yml").read_text(encoding="utf-8")
        self.assertNotIn("micro-bench", package)


if __name__ == "__main__":
    unittest.main()
