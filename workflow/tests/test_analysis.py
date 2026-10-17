import asyncio
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from nfv_workflow.general import Limits, run, select_properties, validate_verification
from nfv_workflow.github import Abstain
from nfv_workflow.outcomes import EXHAUSTED, INVALID, OUTCOMES, REFUTED, VERIFIED
from nfv_workflow.outcomes import InferenceFailure, ResourcesExhausted
from nfv_workflow.publish import render
from nfv_workflow.repository_context import enrich, sanitize_manifest, validate_manifest
from nfv_workflow.general_gates import BACKEND, SRC, clauses, validate_translation, with_postconditions
from nfv_workflow.source_context import function_catalog, quoted_context, scoped_context
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
        self.assertIs(report["completed"], True)
        self.assertEqual(report["properties"][0]["status"], VERIFIED)
        self.assertEqual(len(self.chat.prompts), 1)
        self.assertEqual([stage for stage, _ in verifier.operations], ["resolve", "verification"])
        validate_verification(report["verified"][0], self.bundle)
        url = "https://github.com/local/synthetic/actions/runs/123"
        body = render(report, self.bundle, url)
        self.assertIn("verified for the translated model", body)
        self.assertIn("### P1\n\n**Intended property:** " + self.claim["property"], body)
        self.assertNotIn("## 1. " + self.claim["property"], body)
        self.assertIn("not proofs of source-code correctness", body)
        self.assertNotIn("reachability", body.lower())
        altered = copy.deepcopy(report)
        altered["properties"][0]["property"] = "Something never checked"
        with self.assertRaises(Abstain):
            render(altered, self.bundle, url)
        altered = copy.deepcopy(report)
        altered["properties"][0]["origin"] = "source"
        with self.assertRaises(Abstain):
            render(altered, self.bundle, url)
        altered = copy.deepcopy(report["verified"][0])
        altered["dafny"] += "// changed\n"
        with self.assertRaises(Abstain):
            validate_verification(altered, self.bundle)

    def test_interrupted_analysis_preserves_unpublishable_partial_evidence(self):
        async def interrupted(*args, **kwargs):
            saved = json.loads((self.directory / "report.json").read_text())
            self.assertIs(saved["completed"], False)
            raise asyncio.CancelledError()
        progress = io.StringIO()
        with patch("nfv_workflow.general.run_property", side_effect=interrupted), redirect_stdout(progress):
            with self.assertRaises(asyncio.CancelledError):
                self.execute([], Verifier())
        self.assertEqual(progress.getvalue(), "")
        partial = json.loads((self.directory / "report.json").read_text())
        self.assertIs(partial["completed"], False)
        with self.assertRaisesRegex(Abstain, "incomplete"):
            render(partial, self.bundle, "https://github.com/local/synthetic/actions/runs/123")

    def test_infrastructure_failure_stops_review_without_false_completion(self):
        from nfv_workflow.general import ToolFailure
        second = {**self.claim, "property": "The result is at least n."}
        for error in (InferenceFailure("Provider HTTP 403"), ToolFailure("Dafny unavailable")):
            progress = io.StringIO()
            with self.subTest(error=type(error).__name__), \
                    patch("nfv_workflow.general.run_property", side_effect=error) as analyze, \
                    redirect_stdout(progress):
                report = asyncio.run(run(self.bundle, [self.claim, second], self.directory,
                                         None, Limits(), Verifier()))
            analyze.assert_awaited_once()
            self.assertIs(report["completed"], False)
            self.assertEqual(report["errors"], [str(error)])
            self.assertEqual(len(report["properties"]), 1)
            self.assertEqual(progress.getvalue(), "")
            self.assertEqual(json.loads((self.directory / "report.json").read_text()), report)
            with self.assertRaisesRegex(Abstain, "incomplete"):
                render(report, self.bundle, "https://github.com/local/synthetic/actions/runs/123")

    def test_model_timeout_is_not_an_infrastructure_failure(self):
        with patch("nfv_workflow.general.run_property", side_effect=TimeoutError("model deadline")):
            report = self.execute([], Verifier())
        self.assertIs(report["completed"], True)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["properties"][0]["status"], EXHAUSTED)

    def test_worker_applies_requested_budget_and_preserves_internal_timeout_evidence(self):
        from nfv_workflow.worker import main
        destination = self.directory / "worker.json"
        stdout = ("Dafny program verifier finished with 4 verified, 0 errors, 1 time out\n")
        with patch("sys.argv", ["worker", str(self.directory / "model.dfy"), str(destination),
                                "60", "verify"]), \
                patch("nfv_workflow.worker.subprocess.run", side_effect=[
                    SimpleNamespace(returncode=4, stdout=stdout, stderr=""),
                    SimpleNamespace(stdout="4.11.0\n"),
                ]) as invoked:
            main()
        command = invoked.call_args_list[0].args[0]
        self.assertEqual(command[command.index("--verification-time-limit") + 1], "60")
        result = json.loads(destination.read_text())
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["stdout"], stdout)

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
        self.assertEqual(body.count("### P1\n"), 1)
        self.assertEqual(body.count("**Intended property:** " + self.claim["property"]), 1)
        self.assertIn("Outcome: refuted", body)

    def test_large_verified_model_preserves_results_and_links_full_evidence(self):
        self.translation["model"] += "// " + "large model comment " * 4000 + "\n"
        report = self.execute([self.translation], Verifier(positive=True))
        before = copy.deepcopy(report)
        body = render(report, self.bundle, "https://github.com/local/synthetic/actions/runs/123")
        self.assertLessEqual(len(body), 60000)
        self.assertIn("**Outcome: verified**", body)
        self.assertIn(self.claim["property"], body)
        self.assertIn("Nonnegative iteration count", body)
        self.assertIn("ensures r == n", body)
        self.assertIn(report["verified"][0]["dafny_sha256"], body)
        self.assertIn("full verified Dafny", body)
        self.assertEqual(report, before)

    def test_large_refutation_preserves_witness_conditions_and_source_links(self):
        self.translation["model"] += "// " + "large model comment " * 4000 + "\n"
        report = self.execute([self.translation], Verifier())
        before = copy.deepcopy(report)
        body = render(report, self.bundle, "https://github.com/local/synthetic/actions/runs/123")
        self.assertLessEqual(len(body), 60000)
        self.assertIn("**Outcome: refuted**", body)
        self.assertIn("n = 2", body)
        self.assertIn("Nonnegative iteration count", body)
        self.assertIn(self.claim["path"], body)
        self.assertIn(report["findings"][0]["dafny_sha256"], body)
        self.assertIn("full Dafny", body)
        self.assertEqual(report, before)

    def test_verified_model_compaction_starts_only_above_the_exact_comment_limit(self):
        report = self.execute([self.translation], Verifier(positive=True))
        url = "https://github.com/local/synthetic/actions/runs/123"
        padding = "x" * (60000 - len(render(report, self.bundle, url)) - 3)
        self.translation["model"] += "//" + padding + "\n"
        report = self.execute([self.translation], Verifier(positive=True))
        body = render(report, self.bundle, url)
        self.assertEqual(len(body), 60000)
        self.assertIn(padding, body)
        self.translation["model"] = self.translation["model"][:-1] + "x\n"
        report = self.execute([self.translation], Verifier(positive=True))
        body = render(report, self.bundle, url)
        self.assertLessEqual(len(body), 60000)
        self.assertNotIn(padding, body)
        self.assertIn("full verified Dafny", body)

    def test_failed_properties_use_short_headers_and_normal_sized_statements(self):
        properties = [
            {"id": "first", "property": "The source-derived result preserves its bounds.",
             "origin": "source", "status": INVALID, "diagnostic": "Invalid model."},
            {"id": "second", "property": "The result remains ordered for every admissible input.",
             "status": EXHAUSTED, "diagnostic": "Time budget exhausted."},
        ]
        report = {"schema": 3, "mode": "general-model-analysis",
                  "identity": self.bundle["identity"], "properties": properties,
                  "findings": [], "verified": [], "failures": [], "errors": []}
        body = render(report, self.bundle, "https://github.com/local/synthetic/actions/runs/123")
        self.assertIn("### P1\n\n**Inferred property:** " + properties[0]["property"], body)
        self.assertIn("### P2\n\n**Intended property:** " + properties[1]["property"], body)
        for item in properties:
            self.assertNotIn("## " + item["property"], body)
            self.assertIn("**Outcome: " + item["status"] + "**", body)

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

    def test_catalog_binds_changed_functions_without_model_line_numbers(self):
        path = self.claim["path"]
        self.bundle["sources"][path]["text"] += "\nint g(int n) {\n return n;\n}\n"
        self.bundle["changed"][path] += [6, 7, 8]
        catalog = function_catalog(self.bundle)
        self.assertFalse(catalog["errors"])
        self.assertEqual([fn["name"] for fn in catalog["functions"]], ["f", "g"])
        self.assertEqual([fn["lines"] for fn in catalog["functions"]], [[4], [7, 8]])
        fn = catalog["functions"][0]
        raw = {**self.claim, "function_id": fn["id"], "lines": [6, 7, 8],
               "origin": "source", "signal_id": "source:" + path,
               "quote": "int f(int n) {"}
        claim = select_properties([raw], self.bundle)[0]
        self.assertEqual(claim["lines"], [4])
        self.assertEqual(scoped_context(self.bundle, claim)["source_scope"]["function"], "f")
        self.assertEqual(select_properties([claim], self.bundle)[0], claim)
        with self.assertRaises(Abstain):
            select_properties([{**raw, "quote": "return n;"}], self.bundle)
        self.bundle["sources"][path]["text"] += "// different snapshot\n"
        with self.assertRaises(Abstain):
            select_properties([raw], self.bundle)

    def test_draft_goal_syntax_is_corrected_before_the_first_typed_model_is_frozen(self):
        initial = copy.deepcopy(self.translation)
        bad = "r == (n if n >= 0 else 0)"
        good = "r == (if n >= 0 then n else 0)"
        initial["model"] = initial["model"].replace("ensures r == n", "ensures " + bad)
        initial["original_ensures"] = [bad]
        repaired = initial["model"].replace(bad, good)
        class DraftVerifier(Verifier):
            def resolve(self, source, directory):
                self.operations.append(("resolve", source))
                if bad in source:
                    line = next(i for i, text in enumerate(source.splitlines(), 1) if "ensures" in text)
                    return {"outcome": "type_error", "exit_code": 2, "mode": "typecheck",
                            "stderr": f"model.dfy({line},3): Error: Invalid conditional expression"}
                return {"outcome": "verified", "exit_code": 0, "mode": "typecheck"}
        report = self.execute([initial, {"model": repaired}], DraftVerifier(positive=True))
        self.assertEqual(report["properties"][0]["status"], VERIFIED, report)
        evidence = report["verified"][0]
        self.assertEqual(evidence["translation"]["original_ensures"], [good])
        self.assertEqual(evidence["translation"]["model"], repaired)
        drafts = self.directory / evidence["candidate"]["id"] / "drafts"
        self.assertIn(bad, (drafts / "0.json").read_text())
        self.assertIn(good, (drafts / "1.json").read_text())
        validate_translation(evidence["translation"], evidence["candidate"], self.bundle)

    def test_multiline_source_can_map_to_one_dafny_computation(self):
        model = self.translation["model"]
        path = self.claim["path"]
        marker = f"// src: {path}:2 | int r = 0;"
        model = model.replace(marker, marker + f"\n// src: {path}:4 | return r + 1;", 1)
        mapped = validate_translation(self.translation, self.claim, self.bundle, model)
        assignment = [row for row in mapped if row["model"] == "r := 0;"]
        self.assertEqual([row["line"] for row in assignment], [2, 4])
        self.assertEqual(len({row["model_line"] for row in assignment}), 1)

    def test_restoring_omitted_goals_preserves_inputs_and_computation(self):
        original = self.translation["model"]
        missing = original.replace(
            "// synth: modeled contract or structural syntax\n  ensures r == n\n", "")
        for goals in [["r == n"], ["r == n", "r >= n"]]:
            with self.subTest(goals=goals):
                restored = with_postconditions(missing, "Target", goals)
                self.assertEqual(clauses(restored, "Target", "ensures"), goals)
                self.assertEqual(clauses(restored, "Target", "requires"), ["n >= 0"])
                self.assertEqual(restored.split("\n{\n", 1)[1], original.split("\n{\n", 1)[1])
                validate_translation({**self.translation, "original_ensures": goals,
                                      "model": restored}, self.claim, self.bundle)
        with self.assertRaises(Abstain):
            with_postconditions(missing, "Absent", ["true"])

    def test_repair_context_deduplication_is_lossless_and_keeps_cross_file_sources(self):
        text = "\t  " + "long_quoted_source_expression" * 40 + " \t"
        context = {"sources": {
            "entry.cpp": {"blob_sha": "a" * 40, "ranges": [[42, 43]],
                          "numbered_lines": f"42| {text}\n43| return result;"},
            "external.cpp": {"blob_sha": "b" * 40, "ranges": [[7, 7]],
                             "numbered_lines": "7| int external_helper(int n);"},
        }}
        original = copy.deepcopy(context)
        model = f"// src: entry.cpp:42 | {text.strip()}\nr := n;\n"
        compacted = quoted_context(context, model)
        self.assertEqual(context, original)
        self.assertLess(len(json.dumps(compacted)), len(json.dumps(context)))
        self.assertEqual(compacted["sources"]["external.cpp"], context["sources"]["external.cpp"])
        number, tag_line, leading, trailing = compacted["sources"]["entry.cpp"]["quoted_in_model"][0]
        tag = SRC.fullmatch(model.splitlines()[tag_line - 1])
        self.assertEqual((number, leading + tag[3].strip() + trailing), (42, text))
        self.assertIn("43| return result;", compacted["sources"]["entry.cpp"]["numbered_lines"])
        fabricated = model.replace(text.strip(), "different_source")
        self.assertEqual(quoted_context(context, fabricated), context)

    def test_distinct_claims_have_independent_models_and_fixed_progress(self):
        second = {**self.claim, "property": "The result is at least n."}
        other = copy.deepcopy(self.translation)
        other["model"] = other["model"].replace("ensures r == n", "ensures r >= n")
        other["original_ensures"] = ["r >= n"]
        class ComponentVerifier(Verifier):
            def __call__(self, source, directory):
                self.operations.append((Path(directory).name, source))
                if Path(directory).name == "verification" and "ensures r == n" in source:
                    return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}
                return success()
        chat = Chat([self.translation, other])
        progress = io.StringIO()
        with redirect_stdout(progress):
            report = asyncio.run(run(self.bundle, [self.claim, second], self.directory,
                                     chat, Limits(proof_rounds=0), ComponentVerifier()))
        self.assertEqual([row["status"] for row in report["properties"]], [REFUTED, VERIFIED], report)
        self.assertEqual(len({row["id"] for row in report["properties"]}), 2)
        self.assertEqual(len(chat.prompts), 2)
        self.assertEqual([row["id"] for row in report["properties"]],
                         [row["id"] for row in select_properties([self.claim, second], self.bundle)])
        events = [json.loads(line) for line in progress.getvalue().splitlines()]
        self.assertEqual([row["event"] for row in events], ["property_completed"] * 2)
        self.assertEqual([row["properties_total"] for row in events], [2, 2])
        self.assertEqual([row["properties_completed"] for row in events], [1, 2])
        self.assertEqual([row["properties_remaining"] for row in events], [1, 0])
        self.assertFalse(any("pending" in row or "completed" in row for row in events))
        first, second = report["findings"][0], report["verified"][0]
        self.assertEqual(first["translation"]["original_ensures"], ["r == n"])
        self.assertEqual(second["translation"]["original_ensures"], ["r >= n"])
        self.assertNotIn("ensures r >= n", first["translation"]["model"])
        self.assertNotIn("ensures r == n", second["translation"]["model"])
        self.assertNotIn("requires r == n", second["translation"]["model"])
        for evidence in (first, second):
            self.assertTrue((self.directory / evidence["candidate"]["id"] / "source-context.json").exists())
        body = render(report, self.bundle, "https://github.com/local/synthetic/actions/runs/123")
        self.assertIn("### P1", body)
        self.assertIn("### P2", body)

    def test_compound_properties_stay_whole_for_verification_and_refutation(self):
        from nfv.agent import refute
        for goals in (["r == n && r >= n"], ["r == n", "r >= n"],
                      [" && ".join(f"r >= n + {index}" for index in range(11))]):
            for positive in (True, False):
                with self.subTest(goals=goals, positive=positive):
                    draft = copy.deepcopy(self.translation)
                    draft["model"] = with_postconditions(draft["model"], "Target", goals)
                    draft["original_ensures"] = goals
                    verifier = Verifier(positive=positive)
                    progress = io.StringIO()
                    with redirect_stdout(progress):
                        report = self.execute([draft], verifier)
                    self.assertEqual(len(report["properties"]), 1)
                    self.assertEqual(report["properties"][0]["status"],
                                     VERIFIED if positive else REFUTED, report)
                    evidence = (report["verified"] if positive else report["findings"])[0]
                    self.assertEqual(evidence["translation"]["original_ensures"], goals)
                    self.assertEqual(evidence["translation"]["model"], draft["model"])
                    self.assertEqual(len(self.chat.prompts), 1)
                    candidate = report["properties"][0]["id"]
                    saved = json.loads((self.directory / candidate / "drafts" / "0.json").read_text())
                    self.assertEqual(saved["translation"]["original_ensures"], goals)
                    events = [json.loads(line) for line in progress.getvalue().splitlines()]
                    self.assertEqual(len(events), 1)
                    self.assertEqual((events[0]["properties_total"],
                                      events[0]["properties_completed"]), (1, 1))
                    if not positive:
                        inverse = refute.invert_entry_spec(draft["model"], "Target", BACKEND)
                        self.assertTrue(inverse.ok, inverse.reason)
                        self.assertEqual(clauses(evidence["checked_model"], "Target", "ensures"),
                                         [inverse.inverted])
                    self.assertNotIn("selection_omissions", report)

    def test_whole_compound_properties_with_real_dafny(self):
        from nfv.shared.config import DAFNY_PATH
        from nfv_workflow.check import Dafny
        if not DAFNY_PATH:
            self.skipTest("Dafny is not installed")
        for expected, value in ((VERIFIED, "n + 1"), (REFUTED, "n")):
            for goals in ([f"r == {value} && r >= n"], [f"r == {value}", "r >= n"]):
                with self.subTest(expected=expected, goals=goals):
                    bundle, claim, draft = fixture()
                    claim["property"] = claim["quote"] = f"The result equals {value} and is at least n."
                    bundle["signals"][0]["text"] = claim["quote"]
                    draft["model"] = with_postconditions(draft["model"], "Target", goals)
                    draft["original_ensures"] = goals
                    chat = Chat([draft])
                    report = asyncio.run(run(bundle, [claim], self.directory,
                                             chat, Limits(), Dafny(30)))
                    self.assertEqual(len(report["properties"]), 1)
                    self.assertEqual(report["properties"][0]["status"], expected, report)
                    evidence = (report["verified"] if expected == VERIFIED else report["findings"])[0]
                    self.assertEqual(evidence["translation"]["original_ensures"], goals)
                    self.assertEqual(len(chat.prompts), 1)

    def test_implications_disjunctions_and_quantifiers_are_not_decomposed(self):
        for goal in ("n >= 0 ==> r == n && r >= n", "r == n || r > n",
                     "forall i: int :: i < n ==> i <= r && i + 1 <= r"):
            with self.subTest(goal=goal):
                draft = copy.deepcopy(self.translation)
                draft["model"] = with_postconditions(draft["model"], "Target", [goal])
                draft["original_ensures"] = [goal]
                report = self.execute([draft], Verifier(positive=True))
                self.assertEqual(len(report["properties"]), 1)
                self.assertEqual(report["properties"][0]["status"], VERIFIED, report)
                self.assertEqual(report["verified"][0]["translation"]["original_ensures"], [goal])

    def test_progress_counts_rejected_claims_as_finished_without_growing_total(self):
        progress = io.StringIO()
        with redirect_stdout(progress):
            report = asyncio.run(run(self.bundle, [{}, self.claim], self.directory,
                                     Chat([self.translation]), Limits(), Verifier(positive=True)))
        self.assertEqual([row["status"] for row in report["properties"]], [INVALID, VERIFIED])
        event = json.loads(progress.getvalue())
        self.assertEqual((event["properties_completed"], event["properties_total"],
                          event["properties_remaining"]), (2, 2, 0))

    def test_workflow_warning_policy_keeps_real_verifier_errors_fatal(self):
        from nfv.shared.config import DAFNY_PATH
        from nfv_workflow.check import Dafny, accepted
        if not DAFNY_PATH:
            self.skipTest("Dafny is not installed")
        verifier = Dafny(30)
        source = ("method Check(n: int) returns (r: int)\n"
                  "  requires forall i: int :: i >= 0 ==> i + 1 > 0\n"
                  "  ensures r == n\n{\n  r := n;\n}\n")
        resolved = verifier.resolve(source, self.directory / "warning-resolve")
        self.assertEqual(resolved["exit_code"], 0, resolved)
        self.assertIn("Warning:", resolved["stdout"] + resolved["stderr"])
        self.assertTrue(accepted(verifier(source, self.directory / "warning-verify")))
        bad = verifier(source.replace("r := n;", "r := n + 1;"), self.directory / "bad-verify")
        self.assertFalse(accepted(bad))
        self.assertEqual(bad["exit_code"], 4, bad)
        broken = verifier.resolve(source.replace("r := n;", "r := unknown;"),
                                  self.directory / "bad-resolve")
        self.assertNotEqual(broken["exit_code"], 0, broken)


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
        fn = function_catalog(enriched)["functions"][0]
        claim = select_properties([{**self.claim, "function_id": fn["id"]}], enriched)[0]
        self.assertEqual(scoped_context(enriched, claim)["sources"][self.path]["blob_sha"], oid)

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
            *[{**self.manifest, "files": [{"path": self.path, "symbols": [name]}]}
              for name in ("n::Limits()", "::Limits", "n::", "n:::Limits",
                           "operator(x)", "n::other[]", "operator<<<", "operator;system", "x" * 129)],
            {**self.manifest, "files": self.manifest["files"] * 17},
            {**self.manifest, "files": [{**self.manifest["files"][0], "spec": "trust me"}]},
        ]
        for manifest in bad:
            with self.subTest(manifest=manifest), self.assertRaises(Abstain):
                validate_manifest(manifest, self.bundle)
        api, _ = self.api(mode="120000")
        with self.assertRaises(Abstain):
            enrich(self.bundle, self.manifest, api)

    def test_cpp_qualified_and_call_operator_hints_reach_the_scoped_translator(self):
        self.path = "lib/context.cpp"
        self.text = ("int model_fixer::invert_app(int n) {\n return n + 1;\n}\n"
                     "struct var_subst {\n int operator()(int n) {\n return n + 2;\n }\n};\n"
                     "int lemma_builder::operator|=(int n) {\n return n + 3;\n}\n")
        self.bundle["source_inventory"].append(self.path)
        self.manifest["files"] = [{"path": self.path, "symbols": [
            "sat::model_fixer::invert_app", "invert_app", "var_subst::operator()",
            "var_subst::operator[]", "lemma_builder::operator|="]}]
        api, _ = self.api()
        enriched = enrich(self.bundle, self.manifest, api)
        self.assertEqual(enriched["repository_context"][0]["symbols"], ["invert_app", "operator"])
        context = scoped_context(enriched, self.claim)
        self.assertIn("return n + 1", context["sources"][self.path]["numbered_lines"])
        self.assertIn("return n + 2", context["sources"][self.path]["numbered_lines"])
        self.assertIn("return n + 3", context["sources"][self.path]["numbered_lines"])

    def test_qualified_hint_normalization_preserves_identifier_and_count_budgets(self):
        self.manifest["files"][0]["symbols"] = ["n::" + "x" * 125]
        self.assertEqual(validate_manifest(self.manifest, self.bundle)["files"][0]["symbols"],
                         ["x" * 125])
        for names in (["n::" + "x" * 126], ["n::helper"] * 9):
            self.manifest["files"][0]["symbols"] = names
            with self.subTest(names=names), self.assertRaises(Abstain):
                validate_manifest(self.manifest, self.bundle)

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
