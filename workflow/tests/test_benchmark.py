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
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from nfv_workflow import benchmark, cli, sandbox
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
        self.mutations = []

    def request(self, path):
        return {"has_discussions": self.enabled}

    def graphql(self, query, variables):
        if "discussionCategories" in query:
            return {"repository": {"id": "repo-id", "discussionCategories": {
                "nodes": [{"id": "category-id", "name": self.category}]}}}
        if "discussions(" in query:
            return {"repository": {"discussions": {"nodes": self.discussions,
                                                    "pageInfo": {"hasNextPage": False}}}}
        self.mutations.append((query, variables))
        if "createDiscussion" in query:
            self.discussions.append({"id": "discussion-id", "body": variables["body"],
                                     "author": {"login": "github-actions[bot]", "__typename": "Bot"}})
            return {"createDiscussion": {"discussion": {"url": "https://github.com/owner/nfv/discussions/1"}}}
        return {"updateDiscussion": {"discussion": {"url": "https://github.com/owner/nfv/discussions/1"}}}


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

    def native_report(self, positive=False, negative=True, optional=False, real=False):
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
        with redirect_stdout(io.StringIO()):
            report = asyncio.run(run(case["bundle"], case["properties"], self.directory / uuid.uuid4().hex,
                                     Chat([translation]), Limits(proof_rounds=0, minimize_assumptions=False),
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
        self.assertIn("five-entry pilot", api.mutations[0][1]["title"])
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
                               benchmark.Options(False, False), render_report=False)
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
            "PATH", "SYSTEMROOT", "USERPROFILE", "NFV_DAFNY_PATH", "NFV_Z3_PATH") if key in os.environ}}
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

    def test_publication_retry_updates_one_run_discussion(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        api = PublicationAPI()
        first = benchmark.publish(summary, api, REPO, "General")
        api.discussions[0]["author"]["login"] = "github-actions"
        second = benchmark.publish(summary, api, REPO, "General")
        self.assertEqual(first, second)
        self.assertEqual(len(api.discussions), 1)
        self.assertIn("createDiscussion", api.mutations[0][0])
        self.assertIn("updateDiscussion", api.mutations[1][0])
        self.assertIn("[PARTIAL]", api.mutations[1][1]["title"])
        self.assertIn("Accuracy 0.00%, Precision N/A", api.mutations[0][1]["title"])
        self.assertEqual(api.mutations[0][1]["title"], api.mutations[1][1]["title"])
        self.assertEqual(api.mutations[1][1]["body"], benchmark.render(summary))

    def test_discussion_title_includes_accuracy_and_incorrect_class_precision(self):
        summary = benchmark.summarize(self.prepared, self.evidence, self.directory / "stats")
        cases = [
            (206, True, 68, 1, 82, 9, "nightly benchmark", "72.82%", "90.11%"),
            (5, True, 1, 1, 2, 1, "five-entry pilot", "60.00%", "66.67%"),
            (206, False, 0, 0, 0, 1, "nightly benchmark", "0.00%", "0.00%"),
            (206, True, 1, 0, 0, 0, "nightly benchmark", "0.49%", "N/A"),
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
                api = PublicationAPI()
                benchmark.publish(summary, api, REPO, "General")
                title = api.mutations[0][1]["title"]
                prefix = "" if completed else "[PARTIAL] "
                self.assertEqual(
                    title,
                    f"{prefix}NFV {kind}: {summary['run']['started_at'][:10]} "
                    f"- Accuracy {accuracy}, Precision {precision} (run 123)")

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
        cron = re.search(r'cron: "(\d+) (\d+) \* \* \*"', coordinator)
        self.assertIsNotNone(cron)
        documentation = (ROOT / "workflow/BENCHMARK.md").read_text(encoding="utf-8")
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
