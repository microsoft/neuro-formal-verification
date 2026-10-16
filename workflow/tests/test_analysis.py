import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil
import unittest
import uuid

from nfv_workflow.general import Limits, run, select_properties, validate_verification
from nfv_workflow.github import Abstain
from nfv_workflow.outcomes import EXHAUSTED, INVALID, OUTCOMES, REFUTED, VERIFIED
from nfv_workflow.outcomes import ResourcesExhausted
from nfv_workflow.publish import render
from nfv_workflow.repository_context import enrich, sanitize_manifest, validate_manifest
from nfv_workflow.source_context import scoped_context
from test_general import Chat, FailingVerifier, fixture


def success():
    return {"outcome": "verified", "exit_code": 0, "mode": "unbounded",
            "stdout": "2 verified, 0 errors", "stderr": ""}


class Verifier(FailingVerifier):
    def __init__(self, positive=False, feasible=True):
        super().__init__()
        self.positive, self.feasible = positive, feasible

    def __call__(self, source, directory):
        self.operations.append((Path(directory).name, source))
        if self.positive or Path(directory).name != "verification":
            return success()
        return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}

    def run(self, source, directory, **kwargs):
        return ({"outcome": "failed", "exit_code": 4, "mode": "unbounded"}
                if self.feasible else success())


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.bundle, self.claim, self.translation = fixture()
        self.bundle["identity"]["source_kind"] = "pull-request-head"

    def tearDown(self):
        shutil.rmtree(self.directory)

    def execute(self, responses, verifier):
        self.chat = Chat(responses)
        return asyncio.run(run(self.bundle, [self.claim], self.directory,
                               self.chat, Limits(proof_rounds=0), verifier))

    def test_positive_proof_needs_no_witness_and_binds_public_evidence(self):
        self.translation.pop("witness")
        verifier = Verifier(positive=True)
        report = self.execute([self.translation], verifier)
        self.assertEqual(report["properties"][0]["status"], VERIFIED)
        self.assertEqual(len(self.chat.prompts), 1)
        self.assertEqual([stage for stage, _ in verifier.operations], ["resolve", "verification"])
        validate_verification(report["verified"][0], self.bundle)
        url = "https://github.com/local/synthetic/actions/runs/123"
        body = render(report, self.bundle, url)
        self.assertIn("verified for the translated model", body)
        self.assertIn("not proofs of source-code correctness", body)
        self.assertNotIn("reachability", body.lower())
        altered = copy.deepcopy(report)
        altered["properties"][0]["property"] = "Something never checked"
        with self.assertRaises(Abstain):
            render(altered, self.bundle, url)
        altered = copy.deepcopy(report["verified"][0])
        altered["dafny"] += "// changed\n"
        with self.assertRaises(Abstain):
            validate_verification(altered, self.bundle)

    def test_failed_verification_requests_witness_before_refutation(self):
        self.translation.pop("witness")
        verifier = Verifier()
        report = self.execute([self.translation, {
            "witness": {"n": 2}, "explanation": "The return adds one after counting."}], verifier)
        self.assertEqual(report["properties"][0]["status"], REFUTED, report)
        stages = [name for name, _ in verifier.operations]
        self.assertEqual(stages, ["resolve", "verification", "proof-0", "final"])
        self.assertIn("Propose a concrete counterexample", self.chat.prompts[1])
        self.assertEqual(report["findings"][0]["translation"]["model"], self.translation["model"])
        body = render(report, self.bundle, "https://github.com/local/synthetic/actions/runs/123")
        self.assertEqual(body.count("## 1."), 1)
        self.assertIn("Outcome: refuted", body)

    def test_witness_stage_cannot_replace_the_frozen_model(self):
        self.translation.pop("witness")
        report = self.execute([self.translation, {
            "witness": {"n": 2}, "explanation": "Bad repair", "model": "method Changed() {}"}], Verifier())
        self.assertEqual(report["properties"][0]["status"], INVALID)
        self.assertIn("frozen", report["failures"][0]["diagnostic"])

    def test_witness_audit_failure_is_not_mislabeled_as_exhaustion(self):
        self.translation.pop("witness")
        report = self.execute([self.translation, {
            "failure": INVALID, "diagnostic": "Invalid captured helper specification"}], Verifier())
        self.assertEqual(report["properties"][0]["status"], INVALID)

    def test_unproved_models_have_only_the_resource_outcome(self):
        report = self.execute([self.translation], FailingVerifier())
        self.assertEqual(report["properties"][0]["status"], EXHAUSTED)
        self.assertEqual(report["failures"][0]["reason"], EXHAUSTED)
        self.assertEqual(set(OUTCOMES), {REFUTED, VERIFIED, EXHAUSTED, INVALID})

    def test_vacuous_positive_proof_is_not_verified(self):
        report = self.execute([self.translation], Verifier(positive=True, feasible=False))
        self.assertFalse(report["verified"])
        self.assertEqual(report["properties"][0]["status"], INVALID)

    def test_source_inference_requires_exact_changed_source_quote(self):
        inferred = {**self.claim, "origin": "source",
                    "signal_id": "source:" + self.claim["path"], "quote": "return r + 1;"}
        self.assertEqual(select_properties([inferred], self.bundle)[0]["origin"], "source")
        inferred["quote"] = "return r;"
        with self.assertRaises(Abstain):
            select_properties([inferred], self.bundle)

    def test_auditable_source_tag_errors_are_repaired_before_typechecking(self):
        initial = copy.deepcopy(self.translation)
        initial["model"] = initial["model"].replace(
            f"// src: {self.claim['path']}:4 | return r + 1;",
            "// synth: incorrect computation tag")
        report = self.execute([initial, {"model": self.translation["model"]}], Verifier())
        self.assertEqual(report["properties"][0]["status"], REFUTED, report)
        self.assertEqual(report["findings"][0]["counters"]["syntax_repairs"], 1)


class RepositoryContextTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.claim, _ = fixture("python")
        self.path = "lib/context.py"
        self.text = ("from dataclasses import dataclass\n"
                     "BUDGET_VARIABLES = {\n 'proof_rounds': 'NFV_PROOF_ROUNDS',\n}\n"
                     "@dataclass(frozen=True)\nclass Limits:\n rounds: int = 10\n"
                     " def __post_init__(self):\n  if self.rounds < 0: raise ValueError('rounds')\n"
                     "def helper(n):\n return n + 1\n")
        self.bundle["source_inventory"] = [*self.bundle["sources"], self.path]
        self.manifest = {"identity": self.bundle["identity"], "files": [
            {"path": self.path, "symbols": ["Limits", "BUDGET_VARIABLES", "helper"]}]}
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def api(self, mode="100644"):
        raw = self.text.encode()
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        text, path = self.text, self.path
        class API:
            calls = []
            def request(self, route):
                self.calls.append(route)
                return {"tree": [{"type": "blob", "mode": mode, "path": path, "sha": oid}]}
            def blob(self, repo, sha):
                self.calls.append(sha)
                return text
        return API(), oid

    def test_head_definitions_are_recaptured_and_reach_the_scoped_translator(self):
        api, oid = self.api()
        self.bundle["sources"][self.path] = {"text": "class Limits:\n pass\n", "blob_sha": "b" * 40}
        enriched = enrich(self.bundle, self.manifest, api)
        self.assertEqual(enriched["sources"][self.path], {"text": self.text, "blob_sha": oid})
        self.assertIn(self.bundle["identity"]["source_sha"], api.calls[0])
        self.assertEqual(self.bundle["sources"][self.path]["text"], "class Limits:\n pass\n")
        context = scoped_context(enriched, self.claim)
        source = context["sources"][self.path]["numbered_lines"]
        for snippet in ("@dataclass(frozen=True)", "rounds: int = 10",
                        "raise ValueError", "'proof_rounds'", "return n + 1"):
            self.assertIn(snippet, source)

    def test_an_incomplete_prefix_is_not_mistaken_for_a_complete_blob(self):
        api, oid = self.api()
        self.bundle["sources"][self.path] = {"text": self.text[:40], "blob_sha": oid}
        self.assertEqual(enrich(self.bundle, self.manifest, api)["sources"][self.path]["text"], self.text)

    def test_manifest_rejects_wrong_snapshot_paths_payloads_and_identifiers(self):
        bad = [
            {**self.manifest, "identity": {}},
            {**self.manifest, "files": [{"path": "../secret.py", "symbols": ["Limits"]}]},
            {**self.manifest, "files": [{"path": "missing.py", "symbols": ["Limits"]}]},
            {**self.manifest, "files": [{"path": self.path, "symbols": ["Limits()"]}]},
            {**self.manifest, "files": self.manifest["files"] * 17},
            {**self.manifest, "files": [{**self.manifest["files"][0], "spec": "trust me"}]},
        ]
        for manifest in bad:
            with self.subTest(manifest=manifest), self.assertRaises(Abstain):
                validate_manifest(manifest, self.bundle)
        api, _ = self.api(mode="120000")
        with self.assertRaises(Abstain):
            enrich(self.bundle, self.manifest, api)

    def test_cpp_type_and_constant_bodies_are_captured_from_hints(self):
        self.path = "lib/context.hpp"
        self.text = ("struct Limits {\n int rounds = 10;\n};\n"
                     "constexpr int TABLE[] = {\n 1, 2, 3\n};\n")
        self.bundle["source_inventory"].append(self.path)
        self.manifest["files"] = [{"path": self.path, "symbols": ["Limits", "TABLE"]}]
        api, _ = self.api()
        context = scoped_context(enrich(self.bundle, self.manifest, api), self.claim)
        self.assertIn("int rounds = 10", context["sources"][self.path]["numbered_lines"])
        self.assertIn("1, 2, 3", context["sources"][self.path]["numbered_lines"])

    def test_sanitizer_writes_only_bounded_identity_and_lookup_hints(self):
        candidate, output = self.directory / "candidate.json", self.directory / "context.json"
        candidate.write_text(json.dumps(self.manifest), encoding="utf-8")
        sanitize_manifest(self.bundle, candidate, output)
        self.assertEqual(set(json.loads(output.read_text(encoding="utf-8"))), {"identity", "files"})
        candidate.write_text(" " * 25001, encoding="utf-8")
        with self.assertRaises(Abstain):
            sanitize_manifest(self.bundle, candidate, output)

    def test_source_byte_budget_is_explicit(self):
        self.text = "x" * 2_000_001
        api, _ = self.api()
        with self.assertRaises(ResourcesExhausted):
            enrich(self.bundle, self.manifest, api)

    def test_nested_definition_does_not_consume_an_external_callee_slot(self):
        path = self.claim["path"]
        self.bundle["sources"][path]["text"] = (
            "def target(n):\n def nested():\n  return n\n return one(two(n))\n"
            "def one(n):\n return n + 1\n"
            "def two(n):\n return n + 2\n")
        self.claim["lines"] = [4]
        context = scoped_context(self.bundle, self.claim)
        self.assertEqual({fn["name"] for fn in context["related_functions"]}, {"one", "two"})
