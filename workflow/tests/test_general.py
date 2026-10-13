import asyncio
import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from nfv_workflow.general import LANGUAGE_GUIDES, Limits, reachable, run, select_properties, validate_finding
from nfv_workflow.general_gates import annotate
from nfv_workflow.github import Abstain
from nfv_workflow.publish import dafny_block, render


SOURCES = {
    "c": (".c", ["int f(int n) {", "  int r = 0;", "  for (int i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "cpp": (".cpp", ["int f(int n) {", "  int r = 0;", "  for (int i=0; i<n; ++i) { ++r; }", "  return r + 1;", "}"]),
    "java": (".java", ["static int f(int n) {", "  int r = 0;", "  for (int i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "csharp": (".cs", ["static int F(int n) {", "  int r = 0;", "  for (int i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "go": (".go", ["func f(n int) int {", "  r := 0", "  for i := 0; i < n; i++ { r++ }", "  return r + 1", "}"]),
    "javascript": (".js", ["function f(n) {", "  let r = 0;", "  for (let i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "typescript": (".ts", ["function f(n: number): number {", "  let r: number = 0;", "  for (let i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "python": (".py", ["def f(n):", "    r = 0", "    for i in range(n): r += 1", "    return r + 1"]),
}


def fixture(language="c", invariant=True):
    extension, function_source = SOURCES[language]
    prefix = ["class Example {"] if language in ("java", "csharp") else (
        ["package example", ""] if language == "go" else [])
    offset = len(prefix)
    source = prefix + function_source + (["}"] if language in ("java", "csharp") else [])
    path = "src/example" + extension
    claim = {"path": path, "lines": [4 + offset], "signal_id": "comment:1",
             "quote": "The result equals n.", "property": "The result equals n.", "language": language}
    bundle = {
        "identity": {"repository": "local/synthetic", "number": 1, "head_sha": "a" * 40,
                     "source_sha": "a" * 40, "base_sha": "b" * 40,
                     "merged": True, "merge_sha": "c" * 40, "source_kind": "synthetic"},
        "sources": {path: {"text": "\n".join(source) + "\n"}},
        "changed": {path: [4 + offset]},
        "signals": [{"id": "comment:1", "text": claim["quote"]}],
    }
    lines = []
    def src(number, code):
        number += offset
        lines.extend([f"// src: {path}:{number} | {source[number - 1].strip()}", code])
    def synth(code):
        lines.extend(["// synth: modeled contract or structural syntax", code])
    src(1, "method Target(n: int) returns (r: int)")
    synth("  requires n >= 0")
    synth("  ensures r == n")
    synth("{")
    src(2, "  r := 0;")
    src(2, "  var i := 0;")
    src(3, "  while i < n")
    synth("    invariant 0 <= i <= n")
    if invariant:
        synth("    invariant r == i")
    synth("  {")
    src(3, "    r := r + 1;")
    src(3, "    i := i + 1;")
    synth("  }")
    src(4, "  r := r + 1;")
    synth("}")
    translation = {
        "entry_point": "Target", "model": "\n".join(lines) + "\n",
        "original_ensures": ["r == n"], "source_span": [1 + offset, len(function_source) + offset], "witness": {"n": 2},
        "assumptions": [{"clause": "n >= 0", "description": "Nonnegative iteration count",
                         "origin": "inferred", "optional": False}],
        "environment": [], "required_context": [path],
        "correspondence": "Source correspondence is not established. This model uses exact small integers "
                          "and excludes overflow, concurrency, and exceptional execution.",
        "explanation": "The loop counts to the input and then adds one more.",
        "source_notes": [{"path": path, "line": 4 + offset, "note": "The modeled return includes the extra increment."}],
    }
    return bundle, claim, translation


class Chat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def ask(self, system, prompt, timeout):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if callable(response):
            response = response(prompt)
        return SimpleNamespace(text=json.dumps(response), model="fake-model", api_calls=0)


class FailingVerifier:
    def __init__(self):
        self.operations = []

    def resolve(self, source, directory):
        self.operations.append(("resolve", source))
        return {"outcome": "verified", "exit_code": 0, "mode": "typecheck", "stdout": "", "stderr": ""}

    def __call__(self, source, directory):
        self.operations.append(("verify", source))
        return {"outcome": "failed", "exit_code": 4, "mode": "unbounded", "stdout": "not proved", "stderr": ""}


class GeneralTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.directory)
        try:
            self.directory.parent.rmdir()
        except OSError:
            pass

    def execute(self, bundle, claim, chat, **kwargs):
        return asyncio.run(run(bundle, [claim], self.directory, chat, **kwargs))

    def test_doubled_defaults(self):
        self.assertEqual((Limits().syntax_rounds, Limits().proof_rounds), (6, 10))

    def test_reachability_probe_ignores_braces_in_provenance_comments(self):
        model = ("method Target(x: int) returns (r: int)\n  ensures r == x\n{\n"
                 "// src: f.cpp:2 | if (x > 0) {\n  r := x;\n}\n")
        seen = {}

        class Verifier:
            def run(self, source, directory, filter_symbol=None):
                seen["source"] = source
                return {"outcome": "failed", "exit_code": 4, "mode": "unbounded"}

        ok, _ = reachable(Verifier(), model, "Target", self.directory)
        self.assertTrue(ok)
        self.assertIn("ensures false", seen["source"])

    def test_each_language_has_specific_semantics_guidance(self):
        self.assertEqual(set(LANGUAGE_GUIDES), set(SOURCES))
        self.assertIn("IEEE-754", LANGUAGE_GUIDES["javascript"])
        self.assertIn("checked versus unchecked", LANGUAGE_GUIDES["csharp"])
        self.assertIn("arbitrary-precision", LANGUAGE_GUIDES["python"])

    def test_untrusted_model_text_cannot_close_evidence_fence(self):
        source = "// source contains a fence\n```\n<untrusted>"
        block = dafny_block(source)
        self.assertTrue(block.startswith("````dafny\n"))
        self.assertTrue(block.endswith("\n````"))

    def test_ten_properties_no_triage_filter(self):
        bundle, claim, _ = fixture()
        properties = [claim]
        for i in range(15):
            properties.append({**claim, "property": f"Intended property {i}"})
        self.assertEqual(len(select_properties(properties, bundle)), 10)
        self.assertEqual(len(select_properties([claim, claim], bundle)), 1)

    def test_proof_repair_budget_is_ten(self):
        bundle, claim, translation = fixture(invariant=False)
        chat = Chat([translation] + [{"annotations": []}] * 10)
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, chat, verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertEqual(len(chat.prompts), 11)
        self.assertEqual(sum(op == "verify" for op, _ in verifier.operations), 11)
        self.assertIn("10 proof repairs", report["abstentions"][0]["reason"])
        for operation, source in verifier.operations:
            if operation == "verify":
                self.assertIn("ensures !((r == n))", source)
                self.assertIn("method NfvWitnessCheck()", source)

    def test_type_repair_budget_is_six(self):
        bundle, claim, translation = fixture()
        class TypeFailures(FailingVerifier):
            def resolve(self, source, directory):
                self.operations.append(("resolve", source))
                return {"outcome": "type_error", "exit_code": 2, "mode": "typecheck"}
        verifier = TypeFailures()
        chat = Chat([translation] + [{"model": translation["model"]}] * 6)
        report = self.execute(bundle, claim, chat, verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertEqual(len(chat.prompts), 7)
        self.assertEqual(len(verifier.operations), 7)
        self.assertIn("6 syntax repairs", report["abstentions"][0]["reason"])

    def test_missing_verifier_does_not_spend_repair_calls(self):
        bundle, claim, translation = fixture()
        class Broken(FailingVerifier):
            def resolve(self, source, directory):
                return {"outcome": "tool_error", "stderr": "Dafny unavailable"}
        chat = Chat([translation])
        report = self.execute(bundle, claim, chat, verifier=Broken())
        self.assertTrue(report["errors"])
        self.assertFalse(report["findings"])
        self.assertEqual(len(chat.prompts), 1)

    def test_proof_repair_cannot_replace_computation(self):
        bundle, claim, translation = fixture(invariant=False)
        chat = Chat([translation, {"model": translation["model"].replace("r := r + 1;", "r := n;")}])
        report = self.execute(bundle, claim, chat, verifier=FailingVerifier())
        self.assertFalse(report["findings"])
        self.assertIn("frozen computation", report["abstentions"][0]["reason"])

    def test_missing_or_fabricated_provenance_abstains(self):
        for change in ("missing", "fabricated"):
            bundle, claim, translation = fixture()
            if change == "missing":
                translation["model"] = translation["model"].replace(
                    f"// src: {claim['path']}:4 | {SOURCES['c'][1][3].strip()}\n", "")
            else:
                translation["model"] = translation["model"].replace("| return r + 1;", "| return r;")
            report = self.execute(bundle, claim, Chat([translation]), verifier=FailingVerifier())
            self.assertFalse(report["findings"], change)
            self.assertTrue(report["abstentions"], change)

    def test_missing_external_context_abstains(self):
        bundle, claim, translation = fixture()
        translation["required_context"].append("src/missing.h")
        report = self.execute(bundle, claim, Chat([translation]), verifier=FailingVerifier())
        self.assertIn("Missing required source context", report["abstentions"][0]["reason"])

    def test_unsafe_dafny_never_reaches_verifier(self):
        for unsafe in ("assume false;", "expect false;", "decreases *", "include \"secret.dfy\""):
            bundle, claim, translation = fixture()
            translation["model"] = translation["model"].replace("  r := 0;", "  " + unsafe)
            verifier = FailingVerifier()
            report = self.execute(bundle, claim, Chat([translation]), verifier=verifier)
            self.assertFalse(report["findings"])
            self.assertFalse(verifier.operations)

    def test_quoted_entry_cannot_redirect_goal_inversion(self):
        bundle, claim, translation = fixture()
        source = bundle["sources"][claim["path"]]["text"]
        bundle["sources"][claim["path"]]["text"] = source.replace("int f(int n) {", "// method Target(n: int)")
        translation["model"] = translation["model"].replace("| int f(int n) {", "| // method Target(n: int)")
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, Chat([translation]), verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertFalse(verifier.operations)
        self.assertIn("shadowed", report["abstentions"][0]["reason"])

    def test_noncanonical_line_separator_is_rejected(self):
        bundle, claim, translation = fixture()
        translation["model"] = translation["model"].replace("ensures r == n", "ensures r == n\u2028")
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, Chat([translation]), verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertFalse(verifier.operations)

    def test_annotation_cannot_insert_assignment_or_contract(self):
        for text in ("r := 0;", "requires false", "assert true; r := 0;",
                     "invariant true // hidden", "decreases *"):
            with self.subTest(text=text), self.assertRaises(Abstain):
                annotate("while true\n{\n}\n", [{"after": 1, "text": text}])

    def test_reachability_timeout_is_not_accepted(self):
        bundle, claim, translation = fixture()
        class Unreachable(FailingVerifier):
            def __call__(self, source, directory):
                return {"outcome": "verified", "exit_code": 0, "mode": "unbounded",
                        "stdout": "2 verified, 0 errors", "stderr": ""}
            def run(self, source, directory, **kwargs):
                return {"outcome": "timeout", "exit_code": None, "mode": "unbounded"}
        report = self.execute(bundle, claim, Chat([translation]), verifier=Unreachable())
        self.assertFalse(report["findings"])
        self.assertIn("reachability", report["abstentions"][0]["reason"])

    def test_bodyless_environment_is_rejected(self):
        bundle, claim, translation = fixture()
        translation["model"] = ("// src: src/example.c:1 | int f(int n) {\n"
                                "function Missing(x: int): int\n" + translation["model"])
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, Chat([translation]), verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertFalse(verifier.operations)

    def test_cli_general_defaults_are_wired_to_evaluate(self):
        from nfv_workflow import cli
        bundle, claim, _ = fixture()
        event = self.directory / "event.json"
        submitted = self.directory / "submitted.json"
        event.write_text("{}")
        submitted.write_text(json.dumps({"items": [{"type": "nfv_submit", "proposals": json.dumps([claim])}]}))
        async def fake_run(bundle, properties, output, model, limits):
            self.assertEqual((limits.syntax_rounds, limits.proof_rounds), (6, 10))
            self.assertEqual(model, "fake-model")
            return {"findings": [], "errors": []}
        with patch.object(cli, "collect", return_value=bundle), patch.object(cli, "run_general", side_effect=fake_run):
            self.assertEqual(cli.main(["evaluate", "--event", str(event), "--submission", str(submitted),
                                       "--out", str(self.directory / "out"), "--model", "fake-model"]), 0)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_all_eight_languages_with_fake_translation_and_real_dafny(self):
        for language in SOURCES:
            with self.subTest(language=language):
                bundle, claim, translation = fixture(language)
                report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
                self.assertEqual(len(report["findings"]), 1, report)
                finding = report["findings"][0]
                self.assertEqual(finding["gates"]["source_correspondence"], "not established")
                self.assertTrue(finding["gates"]["witness_consistency"])
                validate_finding(finding, bundle)
                changed = copy.deepcopy(finding)
                changed["checked_model"] = changed["checked_model"].replace("r := r + 1;", "r := r + 2;")
                with self.assertRaises(Abstain):
                    validate_finding(changed, bundle)
                publish_bundle = copy.deepcopy(bundle)
                publish_bundle["identity"]["source_kind"] = "pull-request-head"
                report["identity"] = publish_bundle["identity"]
                body = render(report, publish_bundle, "https://github.com/local/synthetic/actions/runs/123")
                self.assertIn("not a source-confirmed bug", body)
                self.assertIn("source correspondence is not established", body)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_general_heap_state_model_is_checked(self):
        bundle, claim, _ = fixture("java")
        source = [
            "class Example {", "  static class Cell { int value; }", "  static int f(int x) {",
            "    Cell c = new Cell();", "    c.value = x;", "    c.value++;",
            "    return c.value;", "  }", "}"]
        bundle["sources"][claim["path"]]["text"] = "\n".join(source) + "\n"
        bundle["changed"][claim["path"]] = [6]
        bundle["signals"][0]["text"] = "The result equals x."
        claim.update(lines=[6], quote="The result equals x.", property="The result equals x.")
        model = []
        def src(line, code):
            model.extend([f"// src: {claim['path']}:{line} | {source[line - 1].strip()}", code])
        def synth(code):
            model.extend(["// synth: structural or specification syntax", code])
        src(2, "class Cell")
        synth("{")
        src(2, "  var value: int")
        synth("}")
        src(3, "method Target(x: int) returns (r: int)")
        synth("  ensures r == x")
        synth("{")
        src(4, "  var c := new Cell;")
        src(5, "  c.value := x;")
        src(6, "  c.value := c.value + 1;")
        src(7, "  r := c.value;")
        synth("}")
        translation = {
            "entry_point": "Target", "model": "\n".join(model) + "\n", "original_ensures": ["r == x"],
            "source_span": [3, 8], "witness": {"x": 2}, "assumptions": [], "environment": [],
            "required_context": [claim["path"]],
            "correspondence": "Source correspondence is not established; a fresh Dafny object models the Java cell, excluding overflow.",
            "explanation": "The modeled fresh cell stores the input, increments it, and returns the updated value.",
            "source_notes": [{"path": claim["path"], "line": 6, "note": "The modeled object field is incremented."}]}
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_general_external_environment_body_is_checked(self):
        bundle, claim, translation = fixture()
        helper = "int bump(int x) {\n  return x + 1;\n}\n"
        bundle["sources"]["src/helper.c"] = {"text": helper}
        bundle["sources"][claim["path"]]["text"] = bundle["sources"][claim["path"]]["text"].replace(
            "return r + 1;", "return bump(r);")
        model = translation["model"].replace("| return r + 1;", "| return bump(r);")
        position = model.rfind("r := r + 1;")
        model = model[:position] + "r := Bump(r);" + model[position + len("r := r + 1;"):]
        translation["model"] = (
            "// src: src/helper.c:1 | int bump(int x) {\n"
            "function Bump(x: int): int\n"
            "// synth: helper body\n{\n"
            "// src: src/helper.c:2 | return x + 1;\n"
            "  x + 1\n// synth: helper end\n}\n" + model)
        translation["required_context"].append("src/helper.c")
        translation["environment"] = [{
            "name": "Bump", "kind": "source-derived", "description": "Modeled external increment",
            "source": {"path": "src/helper.c", "lines": [1, 2, 3]}}]
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertTrue(any(x["path"] == "src/helper.c" for x in report["findings"][0]["source_map"]))

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_actual_typecheck_repair(self):
        bundle, claim, correct = fixture()
        initial = copy.deepcopy(correct)
        initial["model"] = initial["model"].replace("r := 0;", "r := false;")
        report = self.execute(bundle, claim, Chat([initial, {"model": correct["model"]}]),
                              limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["counters"]["syntax_repairs"], 1)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_actual_annotation_only_proof_repair(self):
        bundle, claim, translation = fixture(invariant=False)
        def repair(prompt):
            request = json.loads(prompt)
            after = next(int(line.split(":")[0]) for line in request["frozen_model_lines"] if "while i < n" in line)
            return {"annotations": [{"after": after, "text": "invariant r == i"}]}
        report = self.execute(bundle, claim, Chat([translation, repair]), limits=Limits(proof_rounds=1))
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["counters"]["proof_repairs"], 1)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_changed_goal_in_type_repair_is_rejected(self):
        bundle, claim, correct = fixture()
        initial = copy.deepcopy(correct)
        initial["model"] = initial["model"].replace("r := 0;", "r := false;")
        wrong = correct["model"].replace("ensures r == n", "ensures r != n")
        report = self.execute(bundle, claim, Chat([initial, {"model": wrong}]), limits=Limits(proof_rounds=0))
        self.assertFalse(report["findings"])
        self.assertIn("goal was altered", report["abstentions"][0]["reason"])

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_holding_modeled_property_abstains(self):
        bundle, claim, translation = fixture()
        bundle["sources"][claim["path"]]["text"] = bundle["sources"][claim["path"]]["text"].replace(
            "return r + 1;", "return r;")
        model = translation["model"].replace("| return r + 1;", "| return r;")
        position = model.rfind("r := r + 1;")
        translation["model"] = model[:position] + "r := r;" + model[position + len("r := r + 1;"):]
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertFalse(report["findings"])
        self.assertIn("Negative check failed", report["abstentions"][0]["reason"])

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_inconsistent_witness_does_not_refute_vacuously(self):
        bundle, claim, translation = fixture()
        translation["witness"] = {"n": -1}
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertFalse(report["findings"])

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_optional_assumption_is_rechecked_and_minimized(self):
        bundle, claim, translation = fixture()
        translation["model"] = translation["model"].replace(
            "// synth: modeled contract or structural syntax\n  ensures r == n",
            "// synth: optional domain restriction\n  requires n <= 100\n"
            "// synth: modeled contract or structural syntax\n  ensures r == n")
        translation["assumptions"].append({"clause": "n <= 100", "description": "Small input",
                                           "origin": "inferred", "optional": True})
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["retained_assumptions"], [0])
        validate_finding(report["findings"][0], bundle)


if __name__ == "__main__":
    unittest.main()
