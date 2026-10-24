import asyncio
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from nfv_workflow import cli, followups
from nfv_workflow.check import Dafny, accepted
from nfv_workflow.general import Limits, ToolFailure, run
from nfv_workflow.general_gates import clauses, with_postconditions
from nfv_workflow.github import Abstain
from nfv_workflow.outcomes import InferenceFailure
from nfv_workflow.publish import fenced, render, text
from test_analysis import Verifier as NativeVerifier, success
from test_general import Chat, fixture


class Verifier:
    def __init__(self, fail=(), tool_error=False):
        self.fail = set(fail)
        self.tool_error = tool_error
        self.operations = []

    def __call__(self, source, directory):
        return self.run(source, directory)

    def run(self, source, directory, filter_symbol=""):
        stage = Path(directory).name
        self.operations.append((stage, source, filter_symbol))
        if self.tool_error:
            return {"outcome": "tool_error", "stderr": "missing verifier"}
        domain = stage in ("input-domain", "witness-input-domain")
        if (stage in self.fail) != domain:
            return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}
        return success()


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.bundle, self.claim, translation = fixture()
        self.bundle["identity"]["source_kind"] = "pull-request-head"
        self.claim["property"] = "The result equals the iteration count and is nonnegative."
        translation["original_ensures"] = ["r == n", "r >= 0"]
        translation["model"] = with_postconditions(translation["model"], "Target",
                                                  translation["original_ensures"])
        self.report = asyncio.run(run(self.bundle, [self.claim], self.directory / "native",
                                      Chat([translation]), Limits(proof_rounds=0), NativeVerifier()))
        self.assertEqual(len(self.report["findings"]), 1, self.report)
        self.finding = self.report["findings"][0]
        base = followups.positive_model(self.finding)
        low, high = followups.body_span(base, "Target")
        before, after = base[low:high].rsplit("  r := r + 1;", 1)
        body = before + "  r := r;" + after
        self.candidate = {
            "simplification": {"property": "Result equals n.", "goal": "r == n"},
            "fix": {"description": "Return the accumulated count without adding one.",
                    "caveat": "Model correspondence and source compilation are not established.",
                    "old": "return r + 1;", "new": "return r;", "body": body},
        }
        self.url = "https://github.com/local/synthetic/actions/runs/123"

    def tearDown(self):
        shutil.rmtree(self.directory)

    def execute(self, candidate=None, options=None, verifier=None):
        self.chat = Chat([candidate or self.candidate] * 2)
        self.verifier = verifier or Verifier()
        return asyncio.run(followups.run(self.report, self.bundle, self.chat, self.directory,
                                         Limits(), options, self.verifier))

    def test_flags_default_true_and_are_independent_and_strict(self):
        self.assertEqual(followups.Options.from_env({}), followups.Options(True, True))
        self.assertEqual(followups.Options.from_env({"NFV_SIMPLIFY_REFUTATIONS": " false "}),
                         followups.Options(False, True))
        self.assertEqual(followups.Options.from_env({"NFV_CHECK_FIXES": "FALSE"}),
                         followups.Options(True, False))
        self.assertEqual(followups.Options.from_env({"NFV_CHECK_FIXES": ""}), followups.Options())
        for value in ("yes", "0", "1", "false; command"):
            with self.assertRaisesRegex(Abstain, "must be true or false"):
                followups.Options.from_env({"NFV_CHECK_FIXES": value})

    def test_both_checked_without_mutating_native_evidence(self):
        before = copy.deepcopy((self.report, self.bundle))
        artifact = self.execute()
        value = artifact["findings"][self.finding["candidate"]["id"]]
        self.assertEqual(value["simplification"]["status"], "checked")
        self.assertEqual(value["fix"]["status"], "checked")
        self.assertEqual((self.report, self.bundle), before)
        self.assertEqual(len(self.chat.prompts), 1)
        self.assertEqual(len(self.verifier.operations), 7)
        lemma = next(source for name, source, _ in self.verifier.operations if name == "entailment")
        suffix = lemma[lemma.index("lemma NfvProjectionCheck"):]
        self.assertIn("n: int, r: int", suffix)
        self.assertIn("((r == n) && (r >= 0)) ==> (r == n)", suffix)
        self.assertNotIn("n == 2", suffix)
        fixed = next(source for name, source, _ in self.verifier.operations if name == "full-property")
        self.assertEqual(clauses(fixed, "Target", "ensures"), ["r == n", "r >= 0"])
        self.assertNotIn("n == 2", fixed)
        witness = next(source for name, source, _ in self.verifier.operations if name == "witness")
        self.assertIn("n == 2", witness)
        self.assertIn("method NfvWitnessCheck()", witness)
        self.assertEqual(json.loads((self.directory / "followups.json").read_text()), artifact)

    def test_each_variant_can_succeed_independently(self):
        for key in ("simplification", "fix"):
            candidate = copy.deepcopy(self.candidate)
            candidate[key] = {"invalid": True}
            artifact = self.execute(candidate)
            value = artifact["findings"][self.finding["candidate"]["id"]]
            self.assertEqual(value[key]["status"], "not-established")
            other = "fix" if key == "simplification" else "simplification"
            self.assertEqual(value[other]["status"], "checked")
            self.assertTrue(value["attempts"][-1]["diagnostics"])
            self.assertEqual(len(self.chat.prompts), 2)

    def test_independently_disabled_features_do_not_run_checks(self):
        for options in (followups.Options(False, True), followups.Options(True, False),
                        followups.Options(False, False)):
            artifact = self.execute(options=options)
            result = artifact["findings"][self.finding["candidate"]["id"]]
            self.assertEqual(result["simplification"]["status"], "checked" if options.simplify else "disabled")
            self.assertEqual(result["fix"]["status"], "checked" if options.fixes else "disabled")
            self.assertEqual(len(self.verifier.operations), 3 * options.simplify + 4 * options.fixes)
            if not options.simplify and not options.fixes:
                self.assertEqual(self.chat.prompts, [])
                self.assertEqual(render(self.report, self.bundle, self.url, followups=artifact),
                                 render(self.report, self.bundle, self.url))

    def test_every_proof_and_input_domain_gate_is_required(self):
        for stage in ("entailment", "refutation", "input-domain", "full-property",
                      "witness", "witness-input-domain"):
            artifact = self.execute(verifier=Verifier(fail=[stage]))
            value = artifact["findings"][self.finding["candidate"]["id"]]
            key = "simplification" if stage in ("entailment", "refutation") else "fix"
            if stage == "input-domain":
                self.assertEqual(value["simplification"]["status"], "not-established")
            self.assertEqual(value[key]["status"], "not-established", stage)
            self.assertEqual(len(self.chat.prompts), 2)

    def test_source_edit_must_be_unique_and_inside_entry(self):
        for old in ("", "not captured"):
            candidate = copy.deepcopy(self.candidate)
            candidate["fix"]["old"] = old
            result = self.execute(candidate)["findings"][self.finding["candidate"]["id"]]
            self.assertEqual(result["fix"]["status"], "not-established")
            self.assertEqual(result["simplification"]["status"], "checked")
        shifted = copy.deepcopy(self.finding)
        shifted["translation"]["source_span"] = [2, 3]
        with self.assertRaises(Abstain):
            followups.source_patch(shifted, self.bundle, self.candidate["fix"])
        partial = copy.deepcopy(self.bundle)
        partial["sources"][self.claim["path"]]["line_start"] = 2
        with self.assertRaises(Abstain):
            followups.source_patch(self.finding, partial, self.candidate["fix"])

    def test_patch_preserves_missing_final_newline(self):
        bundle = copy.deepcopy(self.bundle)
        path = self.claim["path"]
        bundle["sources"][path]["text"] = bundle["sources"][path]["text"].rstrip("\n")
        patch_text, _, _ = followups.source_patch(self.finding, bundle, self.candidate["fix"])
        self.assertIn(" }\n\\ No newline at end of file\n", patch_text)

    def test_repair_cannot_change_contracts_helpers_or_add_unsafe_constructs(self):
        for body in ("\nassume false;\n", "\nwhile true\n decreases *\n{}\n",
                     "\n}\nmethod Other()\n{\n", "\n{:verify false} r := n;\n",
                     "\nassert false by { assume false; }\n"):
            with self.assertRaises(Abstain):
                followups.repaired_model(self.finding, body)
        original = followups.positive_model(self.finding)
        low, high = followups.body_span(original, "Target")
        fixed = followups.repaired_model(self.finding, self.candidate["fix"]["body"])
        left, right = followups.body_span(fixed, "Target")
        self.assertEqual(original[:low], fixed[:left])
        self.assertEqual(original[high:], fixed[right:])

    def test_braces_in_quoted_provenance_and_strings_do_not_change_body_boundary(self):
        model = 'method Target() returns (r: int)\n{\n// if { }\nvar c := \'{\';\nvar s := "}";\nr := 1;\n}\n'
        start, end = followups.body_span(model, "Target")
        self.assertEqual(model[start - 1], "{")
        self.assertEqual(model[end:], "}\n")

    def test_bad_projection_expressions_are_rejected_before_verifier(self):
        for goal in ("true", "false", "r == n\n", "r == n; assume false;",
                     "r == n // escape", "r == n /* escape", "NfvWitnessCheck()"):
            candidate = copy.deepcopy(self.candidate)
            candidate["simplification"]["goal"] = goal
            result = self.execute(candidate)["findings"][self.finding["candidate"]["id"]]
            self.assertEqual(result["simplification"]["status"], "not-established", goal)
            self.assertEqual(result["fix"]["status"], "checked")

    def test_artifact_tampering_stops_rendering(self):
        artifact = self.execute()
        identity = self.finding["candidate"]["id"]
        for field, value in (("model_sha256", "0" * 64), ("patch", "fake patch"),
                             ("body", "\nr := n + 1;\n"), ("new", "return 42;")):
            altered = copy.deepcopy(artifact)
            altered["findings"][identity]["fix"][field] = value
            with self.assertRaises(Abstain):
                render(self.report, self.bundle, self.url, followups=altered)
        for field, value in (("goal", "r >= 0"), ("entailment_sha256", "0" * 64)):
            altered = copy.deepcopy(artifact)
            altered["findings"][identity]["simplification"][field] = value
            with self.assertRaises(Abstain):
                render(self.report, self.bundle, self.url, followups=altered)
        for field, value in (("completed", False), ("bundle_sha256", "0" * 64), ("report_sha256", "0" * 64)):
            altered = copy.deepcopy(artifact)
            altered[field] = value
            with self.assertRaises(Abstain):
                render(self.report, self.bundle, self.url, followups=altered)

    def test_short_self_contained_render_keeps_counts_and_labels(self):
        artifact = self.execute()
        body = render(self.report, self.bundle, self.url, followups=artifact)
        self.assertIn("# NFV: 1 refuted, 0 verified in PR #1", body)
        self.assertIn("### P1\n", body)
        self.assertIn("**Intended property:** Result equals n.", body)
        self.assertNotIn(self.claim["property"], body)
        self.assertIn("refutation-with-fix (model-checked candidate)", body)
        self.assertIn("```diff\n", body)
        self.assertIn("-  return r + 1;", body)
        self.assertIn("+  return r;", body)
        self.assertIn("not applied, compiled or source-verified", body)
        self.assertNotIn("original property", body)
        self.assertLess(len(body), 11000)
        self.assertNotIn("trace", body.lower())
        self.assertIn("````diff\n", fenced("diff", "```\n"))
        self.assertEqual(text("@owner <script>"), "@\u200bowner &lt;script&gt;")

    def test_infrastructure_failure_is_explicit_and_does_not_publish_success(self):
        before = copy.deepcopy(self.report)
        with self.assertRaises(ToolFailure):
            self.execute(verifier=Verifier(tool_error=True))
        self.assertEqual(self.report, before)
        self.assertIs(json.loads((self.directory / "followups.json").read_text())["completed"], False)
        chat = AsyncMock()
        chat.ask.side_effect = InferenceFailure("HTTP 403")
        with self.assertRaises(InferenceFailure):
            asyncio.run(followups.run(self.report, self.bundle, chat, self.directory, Limits()))

    def test_invalid_environment_fails_before_inference_and_native_verification(self):
        with patch.dict("os.environ", {"NFV_CHECK_FIXES": "invalid"}), \
                patch.object(cli, "inference_chat") as chat, \
                patch("nfv_workflow.general.run", new_callable=AsyncMock) as analyze:
            with self.assertRaises(Abstain):
                asyncio.run(cli.run_general(self.bundle, [self.claim], self.directory, "fake", Limits()))
        chat.assert_not_called()
        analyze.assert_not_awaited()

    def test_no_findings_never_start_followup_inference(self):
        class Context:
            async def __aenter__(self):
                return Chat([])

            async def __aexit__(self, *args):
                pass
        report = {**self.report, "findings": []}
        stale = self.directory / "followups.json"
        stale.write_text("{}")
        with patch.object(cli, "inference_chat", return_value=Context()), \
                patch("nfv.shared.config.DAFNY_PATH", "installed"), \
                patch("nfv_workflow.general.run", new_callable=AsyncMock, return_value=report), \
                patch.object(followups, "run", new_callable=AsyncMock) as enrich:
            result = asyncio.run(cli.run_general(self.bundle, [self.claim], self.directory, "fake", Limits()))
        self.assertEqual(result, report)
        enrich.assert_not_awaited()
        self.assertFalse(stale.exists())

    def test_native_cli_runs_enabled_followups_after_acceptance(self):
        chat = Chat([])
        class Context:
            async def __aenter__(self):
                return chat

            async def __aexit__(self, *args):
                pass
        with patch.object(cli, "inference_chat", return_value=Context()), \
                patch("nfv.shared.config.DAFNY_PATH", "installed"), \
                patch("nfv_workflow.general.run", new_callable=AsyncMock, return_value=self.report), \
                patch.dict("os.environ", {"NFV_SIMPLIFY_REFUTATIONS": "", "NFV_CHECK_FIXES": ""}), \
                patch.object(followups, "run", new_callable=AsyncMock) as enrich:
            asyncio.run(cli.run_general(self.bundle, [self.claim], self.directory, "fake", Limits()))
        enrich.assert_awaited_once_with(self.report, self.bundle, chat, self.directory,
                                       Limits(), followups.Options(True, True))

    def test_present_consumes_checked_followups_without_proof_reruns(self):
        from nfv_workflow.presentation import refresh
        artifact = self.execute()
        path = self.claim["path"]
        mapping = {
            "inputs": {"n": {"kind": "expression", "expression": "n", "anchors": [
                {"path": path, "line": 1, "excerpt": "int f(int n) {"}]}},
            "failure": {"expression": "r + 1", "anchors": [
                {"path": path, "line": 4, "excerpt": "  return r + 1;"}]},
            "explanation": "The return r + 1 exceeds n.",
        }
        presentation = asyncio.run(refresh(self.report, self.bundle, Chat([mapping]), 30))
        report_path, bundle_path = self.directory / "report.json", self.directory / "bundle.json"
        report_path.write_text(json.dumps(self.report))
        bundle_path.write_text(json.dumps(self.bundle))
        args = ["present", "--bundle", str(bundle_path), "--report", str(report_path),
                "--model", "fake", "--out", str(self.directory / "shown"), "--evidence-url", self.url]
        with patch.object(cli, "refresh_presentation", new_callable=AsyncMock,
                          return_value=presentation) as refresh_agent, \
                patch.object(Dafny, "run", side_effect=AssertionError("Proof rerun")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        refresh_agent.assert_awaited_once()
        self.assertIn("refutation-with-fix", (self.directory / "shown" / "comment.md").read_text())
        artifact["completed"] = False
        (self.directory / "followups.json").write_text(json.dumps(artifact))
        with patch.object(cli, "refresh_presentation", new_callable=AsyncMock) as refresh_agent, \
                redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main(args), 1)
        refresh_agent.assert_not_awaited()

    def test_local_command_line_flags_are_independent(self):
        bundle_path = self.directory / "bundle.json"
        claims_path = self.directory / "properties.json"
        bundle_path.write_text(json.dumps(self.bundle))
        claims_path.write_text(json.dumps([self.claim]))
        with patch.object(cli, "run_general", new_callable=AsyncMock, return_value=self.report) as analyze, \
                patch.dict("os.environ", {"NFV_CHECK_FIXES": "false"}), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["run", "--bundle", str(bundle_path), "--properties", str(claims_path),
                                       "--model", "fake", "--out", str(self.directory),
                                       "--no-simplify-refutations", "--check-fixes"]), 0)
        self.assertEqual(analyze.call_args.kwargs["followup_options"], followups.Options(False, True))

    @unittest.skipUnless(shutil.which("dafny"), "Dafny integration requires installed Dafny")
    def test_real_dafny_projection_and_full_property_fix(self):
        value = self.execute(verifier=Dafny(30))["findings"][self.finding["candidate"]["id"]]
        self.assertEqual(value["simplification"]["status"], "checked", value["simplification"])
        self.assertEqual(value["fix"]["status"], "checked", value["fix"])

    @unittest.skipUnless(shutil.which("dafny"), "Dafny integration requires installed Dafny")
    def test_real_dafny_rejects_unentailed_or_nonfailing_projection(self):
        for index, goal in enumerate(("r == n + 1", "r >= 0", "n == 2 && r == n")):
            with self.subTest(goal=goal):
                candidate = {"goal": goal, "property": "A shorter claim."}
                checked = followups.check_simplification(self.finding, candidate,
                                                         self.directory / str(index), Dafny(30))
                self.assertEqual(checked["status"], "not-established")

    @unittest.skipUnless(shutil.which("dafny"), "Dafny integration requires installed Dafny")
    def test_real_fix_must_cover_all_inputs_not_only_the_counterexample(self):
        candidate = copy.deepcopy(self.candidate["fix"])
        candidate["body"] = candidate["body"].replace("r := r;", "r := 2;")
        candidate["new"] = "return 2;"
        checked = followups.check_fix(self.finding, self.bundle, candidate,
                                      self.directory / "overfit", Dafny(30))
        self.assertEqual(checked["status"], "not-established")
        self.assertTrue(accepted(checked["checks"]["witness"]))
        self.assertFalse(accepted(checked["checks"]["full_property"]))
