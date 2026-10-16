import asyncio
import copy
import json
import os
from pathlib import Path
import re
import shutil
import unittest
import uuid
from unittest.mock import patch

from test_general import Chat, FailingVerifier, fixture
from nfv_workflow import cli
from nfv_workflow.general import Limits, run
from nfv_workflow.github import Abstain
from nfv_workflow.presentation import refresh, validate, validate_artifact
from nfv_workflow.publish import render


class AcceptedVerifier(FailingVerifier):
    def __call__(self, source, directory):
        self.operations.append(("verify", source))
        if Path(directory).name == "verification":
            return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}
        return {"outcome": "verified", "exit_code": 0, "mode": "unbounded",
                "stdout": "Dafny program verifier finished with 2 verified, 0 errors", "stderr": ""}

    def run(self, source, directory, **kwargs):
        self.operations.append(("reachability", source))
        return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.bundle, self.claim, self.translation = fixture()
        self.bundle["identity"]["source_kind"] = "pull-request-head"
        self.url = "https://github.com/local/synthetic/actions/runs/1"
        path = self.claim["path"]
        self.value = {
            "inputs": {"n": {"kind": "expression", "expression": "n", "anchors": [
                {"path": path, "line": 1, "excerpt": "int f(int n) {"}]}},
            "failure": {"expression": "r + 1", "anchors": [
                {"path": path, "line": 4, "excerpt": "  return r + 1;"}]},
            "explanation": "For this input, r + 1 exceeds n, so the intended equality fails.",
        }

    def tearDown(self):
        shutil.rmtree(self.directory)
        try:
            self.directory.parent.rmdir()
        except OSError:
            pass

    def report(self, translation=None):
        verifier = AcceptedVerifier()
        report = asyncio.run(run(self.bundle, [self.claim], self.directory / "proof",
                                 Chat([translation or self.translation]), verifier=verifier))
        self.assertEqual(len(report["findings"]), 1, report)
        return report, verifier

    def audit(self, report, responses=None):
        report = copy.deepcopy(report)
        bundle_path, proposals_path = self.directory / "bundle.json", self.directory / "proposals.json"
        bundle_path.write_text(json.dumps(self.bundle), encoding="utf-8")
        proposals_path.write_text(json.dumps([self.claim]), encoding="utf-8")
        output = self.directory / "audit"
        async def saved_run(bundle, proposals, out, model, limits):
            (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
            return report
        async def agent_refresh(saved_report, bundle, model, timeout):
            if responses is None:
                raise AssertionError("Unexpected presentation inference")
            return await refresh(saved_report, bundle, Chat(responses), timeout)
        with patch.dict(os.environ, {
            "NFV_BUNDLE": str(bundle_path), "NFV_OUT": str(output), "NFV_MODEL": "approved-model",
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "local/synthetic",
            "GITHUB_RUN_ID": "1",
        }), patch.object(cli, "run_general", side_effect=saved_run), \
                patch.object(cli, "refresh_presentation", side_effect=agent_refresh) as refreshed:
            summary = cli.audit(proposals_path, Limits())
        return summary, refreshed

    def test_audit_refreshes_invalid_inline_presentation_without_rerunning_proof(self):
        invalid = copy.deepcopy(self.value)
        invalid["inputs"]["n"]["value"] = 2
        report, verifier = self.report({**self.translation, "presentation": invalid})
        before = copy.deepcopy((report, self.bundle, verifier.operations))
        summary, refreshed = self.audit(report, [self.value])
        self.assertEqual(refreshed.call_count, 1)
        self.assertEqual((report, self.bundle, verifier.operations), before)
        self.assertIn(self.value["explanation"], Path(summary["comment"]).read_text())
        output = Path(summary["comment"]).parent
        artifact = json.loads((output / "presentation.json").read_text())
        validate_artifact(artifact, report, self.bundle)
        self.assertTrue((output / "presentation-diagnostics.json").exists())
        self.assertEqual(json.loads((output / "report.json").read_text()), report)

    def test_audit_uses_valid_inline_presentation_without_another_agent_call(self):
        report, _ = self.report({**self.translation, "presentation": self.value})
        summary, refreshed = self.audit(report)
        self.assertEqual(refreshed.call_count, 0)
        self.assertIn(self.value["explanation"], Path(summary["comment"]).read_text())

    def test_audit_does_not_refresh_invalid_proof_evidence(self):
        report, _ = self.report()
        report["findings"][0]["dafny"] += "// tampered"
        with self.assertRaises(Abstain):
            self.audit(report)
        self.assertFalse((self.directory / "audit" / "presentation.json").exists())
        self.assertFalse((self.directory / "audit" / "comment.md").exists())

    def test_audit_preserves_evidence_when_presentation_refresh_fails(self):
        report, _ = self.report()
        with self.assertRaises(Abstain):
            self.audit(report, [{}])
        output = self.directory / "audit"
        self.assertEqual(json.loads((output / "report.json").read_text()), report)
        self.assertTrue((output / "presentation-diagnostics.json").exists())
        self.assertFalse((output / "comment.md").exists())

    def test_translator_metadata_renders_without_changing_proof(self):
        original, _ = self.report()
        mapped, _ = self.report({**self.translation, "presentation": self.value})
        for field in ("dafny", "dafny_sha256", "frozen_negative_model", "checked_model",
                      "source_map", "annotation_plan", "result"):
            self.assertEqual(original["findings"][0][field], mapped["findings"][0][field])
        body = render(mapped, self.bundle, self.url)
        self.assertIn(self.value["explanation"], body)
        self.assertIn("Source presentation (agent-inferred)", body)
        self.assertIn("The checked witness and proof evidence are unchanged.", body)
        self.assertLess(body.index("<summary>Minimized Dafny</summary>"),
                        body.index("Source presentation (agent-inferred)"))

    def test_refresh_is_agent_generated_and_preserves_all_input_evidence(self):
        report, verifier = self.report()
        before = copy.deepcopy((report, self.bundle, verifier.operations))
        chat = Chat([self.value])
        artifact = asyncio.run(refresh(report, self.bundle, chat))
        self.assertEqual((report, self.bundle, verifier.operations), before)
        self.assertEqual(len(chat.prompts), 1)
        self.assertIn('"source_map"', chat.prompts[0])
        self.assertIn('"witness": {"n": 2}', chat.prompts[0])
        self.assertIn("1| int f(int n) {", chat.prompts[0])
        self.assertEqual(artifact["provenance"]["calls"][0]["model"], "fake-model")
        body = render(report, self.bundle, self.url, presentation=artifact)
        self.assertIn("**Input on which it fails:** `n = 2`", body)
        self.assertNotIn(self.translation["explanation"], body)

    def test_empty_partial_unknown_and_unanchored_mappings_fail_closed(self):
        for mutation in (
            lambda value: value.update(inputs={}),
            lambda value: value["inputs"].update(extra=value["inputs"]["n"]),
            lambda value: value["inputs"]["n"].update(anchors=[]),
            lambda value: value["inputs"]["n"].update(expression="fabricated()"),
            lambda value: value["inputs"]["n"]["anchors"][0].update(line=999),
            lambda value: value["inputs"]["n"]["anchors"][0].update(excerpt="int f(int x) {"),
            lambda value: value["failure"].update(expression="ghostFlag"),
            lambda value: value.update(explanation="A ghost result is false."),
            lambda value: value.update(model="replaced"),
        ):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(self.value)
                mutation(value)
                with self.assertRaises(Abstain):
                    validate(value, self.translation, self.bundle)

    def test_abstract_inputs_are_explicit_and_escaped(self):
        value = copy.deepcopy(self.value)
        value["inputs"]["n"] = {"kind": "abstract", "label": "<state> @someone",
                                "reason": "No concrete source object is justified. ```"}
        report, _ = self.report()
        artifact = asyncio.run(refresh(report, self.bundle, Chat([value])))
        body = render(report, self.bundle, self.url, artifact)
        self.assertIn("abstract state: &lt;state&gt; @\u200bsomeone", body)
        visible = body[:body.index("<details>")]
        self.assertNotIn("No concrete source object is justified", visible)
        self.assertIn("No concrete source object is justified", body)
        self.assertNotIn("<state>", body)
        self.assertIn("\\`\\`\\`", body)
        self.assertEqual(body.count("<details>"), body.count("</details>"))

    def test_model_only_outputs_rejected_in_failure_prose(self):
        translation = copy.deepcopy(self.translation)
        translation["model"] = translation["model"].replace("returns (r: int)", "returns (ghostResult: int)")
        value = copy.deepcopy(self.value)
        value["explanation"] += " ghostResult is false."
        with self.assertRaisesRegex(Abstain, "model-only"):
            validate(value, translation, self.bundle)

    def test_per_element_predicates_are_anchored_and_remain_abstract(self):
        value = copy.deepcopy(self.value)
        value["inputs"]["n"]["kind"] = "per-element"
        with self.assertRaisesRegex(Abstain, "sequence witness"):
            validate(value, self.translation, self.bundle)
        translation = copy.deepcopy(self.translation)
        translation["witness"]["n"] = [2]
        validate(value, translation, self.bundle)
        report, _ = self.report()
        with patch("nfv_workflow.general.validate_finding"), \
                patch("nfv_workflow.presentation.signature_parameters", return_value=([], [])):
            report["findings"][0]["translation"]["witness"]["n"] = [2]
            report["findings"][0]["translation"]["presentation"] = value
            body = render(report, self.bundle, self.url)
        visible = body[:body.index("<details>")]
        self.assertIn("abstract state: per-element results of `n = [2]`", visible)
        self.assertIn("abstract per-element predicate results", body)

    def test_model_input_names_reverse_map_without_renaming_verifier_parameters(self):
        translation = copy.deepcopy(self.translation)
        translation["model"] = "\n".join(
            line if line.lstrip().startswith("//") else re.sub(r"\bn\b", "modelInput", line)
            for line in self.translation["model"].splitlines()) + "\n"
        translation["witness"] = {"modelInput": 2}
        translation["assumptions"][0]["clause"] = "modelInput >= 0"
        translation["original_ensures"] = ["r == modelInput"]
        value = copy.deepcopy(self.value)
        value["inputs"] = {"modelInput": value["inputs"]["n"]}
        translation["presentation"] = value
        report, _ = self.report(translation)
        body = render(report, self.bundle, self.url)
        visible = body[:body.index("<details>")]
        self.assertIn("**Input on which it fails:** `n = 2`", visible)
        self.assertNotIn("modelInput", visible)
        self.assertEqual(report["findings"][0]["translation"]["witness"], {"modelInput": 2})
        self.assertIn("modelInput", report["findings"][0]["dafny"])

    def test_invalid_agent_output_is_not_silently_dropped(self):
        report, _ = self.report()
        for answer in ({}, {"abstain": "No supported mapping"}, {**self.value, "inputs": {}}):
            with self.subTest(answer=answer), self.assertRaises(Abstain):
                asyncio.run(refresh(report, self.bundle, Chat([answer])))

    def test_changed_report_or_bundle_rejects_saved_presentation(self):
        report, _ = self.report()
        artifact = asyncio.run(refresh(report, self.bundle, Chat([self.value])))
        for target in ("report", "bundle", "artifact"):
            changed_report, changed_bundle, changed_artifact = copy.deepcopy((report, self.bundle, artifact))
            if target == "report":
                changed_report["properties"][0]["status"] = "edited"
            elif target == "bundle":
                changed_bundle["sources"][self.claim["path"]]["blob_sha"] = "d" * 40
            else:
                changed_artifact["findings"] = {}
            with self.subTest(target=target), self.assertRaises(Abstain):
                validate_artifact(changed_artifact, changed_report, changed_bundle)

    def test_artifact_metadata_cannot_change_after_recorded_agent_output(self):
        report, _ = self.report()
        artifact = asyncio.run(refresh(report, self.bundle, Chat([self.value])))
        identity = report["findings"][0]["candidate"]["id"]
        artifact["findings"][identity]["explanation"] += " Edited."
        with self.assertRaisesRegex(Abstain, "recorded agent output"):
            validate_artifact(artifact, report, self.bundle)

    def test_invalid_evidence_fails_before_agent_call(self):
        report, _ = self.report()
        report["findings"][0]["dafny"] += "// tampered"
        chat = Chat([self.value])
        with self.assertRaises(Abstain):
            asyncio.run(refresh(report, self.bundle, chat))
        self.assertEqual(chat.prompts, [])

    def test_inline_invalid_metadata_does_not_change_proof_acceptance(self):
        for value in ({}, None):
            with self.subTest(value=value):
                report, _ = self.report({**self.translation, "presentation": value})
                with self.assertRaises(Abstain):
                    render(report, self.bundle, self.url)

    def test_cli_writes_separate_artifact_and_deterministic_preview(self):
        report, _ = self.report()
        artifact = asyncio.run(refresh(report, self.bundle, Chat([self.value])))
        report_path, bundle_path = self.directory / "report.json", self.directory / "bundle.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        bundle_path.write_text(json.dumps(self.bundle), encoding="utf-8")
        before = (report_path.read_bytes(), bundle_path.read_bytes())

        async def fake_refresh(saved_report, saved_bundle, model, timeout):
            self.assertEqual(model, "approved-model")
            return artifact

        output = self.directory / "presentation"
        with patch.object(cli, "refresh_presentation", side_effect=fake_refresh), \
                patch("nfv_workflow.general.run", side_effect=AssertionError("No proof rerun")):
            status = cli.main(["present", "--report", str(report_path), "--bundle", str(bundle_path),
                               "--model", "approved-model", "--out", str(output),
                               "--evidence-url", self.url])
        self.assertEqual(status, 0)
        self.assertEqual(before, (report_path.read_bytes(), bundle_path.read_bytes()))
        self.assertEqual(json.loads((output / "presentation.json").read_text()), artifact)
        self.assertEqual((output / "comment.md").read_text(encoding="utf-8"),
                         render(report, self.bundle, self.url, artifact))
        self.assertNotIn(b"\r\n", (output / "comment.md").read_bytes())
        self.assertNotIn(b"\r\n", (output / "presentation.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
