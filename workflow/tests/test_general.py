import asyncio
import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from nfv_workflow.general import (LANGUAGE_GUIDES, Limits, reachable, run,
                                  select_properties, validate_finding)
from nfv_workflow.general_gates import annotate, validate_translation
from nfv_workflow.github import Abstain
from nfv_workflow.publish import dafny_block, relevant_code, render, source_block, source_calls


SOURCES = {
    "c": (".c", ["int f(int n) {", "  int r = 0;", "  for (int i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "cpp": (".cpp", ["int f(int n) {", "  int r = 0;", "  for (int i=0; i<n; ++i) { ++r; }", "  return r + 1;", "}"]),
    "java": (".java", ["static int f(int n) {", "  int r = 0;", "  for (int i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "csharp": (".cs", ["static int F(int n) {", "  int r = 0;", "  for (int i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "go": (".go", ["func f(n int) int {", "  r := 0", "  for i := 0; i < n; i++ { r++ }", "  return r + 1", "}"]),
    "javascript": (".js", ["function f(n) {", "  let r = 0;", "  for (let i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "typescript": (".ts", ["function f(n: number): number {", "  let r: number = 0;", "  for (let i=0; i<n; i++) { r++; }", "  return r + 1;", "}"]),
    "python": (".py", ["def f(n):", "    r = 0", "    for i in range(n): r += 1", "    return r + 1"]),
    "rust": (".rs", ["fn f(n: u32) -> u32 {", "    let mut r = 0;", "    for _ in 0..n { r += 1; }", "    r + 1", "}"]),
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
        self.last_translation = None

    async def ask(self, system, prompt, timeout):
        self.prompts.append(prompt)
        if not self.responses and '"typecheck":' in prompt and self.last_translation:
            response = {"model": self.last_translation["model"]}
        else:
            response = self.responses.pop(0)
        if callable(response):
            response = response(prompt)
        if isinstance(response, dict) and "entry_point" in response:
            self.last_translation = copy.deepcopy(response)
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
    def test_prompt_models_missing_callees_as_environment(self):
        prompt = (Path(__file__).parents[1] / "src/nfv_workflow/general_prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("Missing referenced files mean abstention", prompt)
        self.assertIn("Do not abstain merely because a callee is implemented elsewhere", prompt)

    def test_prompt_analyzes_captured_neighbors_without_narrowing_entry_inputs(self):
        prompt = (Path(__file__).parents[1] / "src/nfv_workflow/general_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Analyze those bodies", prompt)
        self.assertIn("source-derived helpers", prompt)
        self.assertIn("do not assume they are the only callers", prompt)
        self.assertIn("or restrict entry inputs merely to the arguments they happen to pass", prompt)

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

    def execute_many(self, bundle, claims, chat, **kwargs):
        return asyncio.run(run(bundle, claims, self.directory, chat, **kwargs))

    def test_default_repair_budgets(self):
        self.assertEqual((Limits().syntax_rounds, Limits().proof_rounds), (6, 10))

    def test_context_omits_unrelated_files(self):
        bundle, claim, _ = fixture()
        sources = {claim["path"]: bundle["sources"][claim["path"]],
                   "near.h": {"text": "int near;\n"},
                   "far.h": {"text": "x" * 700000}}
        bundle["sources"] = {"near.h": sources["near.h"], "far.h": sources["far.h"],
                             claim["path"]: sources[claim["path"]]}
        chat = Chat([{"abstain": "stop after inspecting the prompt"}])
        report = self.execute(bundle, claim, chat)
        prompt = chat.prompts[0]
        self.assertLess(len(prompt.encode("utf-8")), 32000)
        self.assertNotIn('"near.h"', prompt)
        self.assertNotIn('"far.h"', prompt)
        self.assertIn("All other function bodies and files are omitted", prompt)
        self.assertIn("stop after inspecting", report["failures"][0]["diagnostic"])

    def test_large_unrelated_file_content_does_not_block_changed_function(self):
        bundle, claim, _ = fixture()
        bundle["sources"][claim["path"]]["text"] += "x" * 130000
        chat = Chat([{"abstain": "scoped function reached model"}])

        report = self.execute(bundle, claim, chat)

        self.assertEqual(len(chat.prompts), 1)
        self.assertNotIn("x" * 1000, chat.prompts[0])
        self.assertIn("scoped function reached model", report["failures"][0]["diagnostic"])

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
        self.assertIn("debug builds", LANGUAGE_GUIDES["rust"])

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

    def test_invalid_claim_is_an_abstention_not_a_batch_failure(self):
        bundle, claim, _ = fixture()
        unchanged = {**claim, "lines": [999999], "property": "Cites unchanged code"}
        with self.assertRaises(Abstain):
            select_properties([unchanged], bundle)
        chat = Chat([{"abstain": "valid claim reached translation"}])
        report = self.execute_many(bundle, [unchanged, claim], chat)
        reasons = [x["diagnostic"] for x in report["failures"]]
        self.assertTrue(any("not tied to added/changed head code" in r for r in reasons))
        self.assertTrue(any("valid claim reached translation" in r for r in reasons))
        self.assertEqual(report["errors"], [])

    def test_translator_sees_repository_line_numbers(self):
        bundle, claim, _ = fixture()
        chat = Chat([{"abstain": "stop"}])
        self.execute(bundle, claim, chat)
        first = bundle["sources"][claim["path"]]["text"].splitlines()[0]
        self.assertIn(json.dumps(f"1| {first}")[1:-1], chat.prompts[0])
        self.assertNotIn('"text":', chat.prompts[0].split("\n", 1)[1])

    def test_proof_repair_budget_is_ten(self):
        bundle, claim, translation = fixture(invariant=False)
        chat = Chat([translation] + [{"annotations": []}] * 10)
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, chat, verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertEqual(len(chat.prompts), 11)
        self.assertEqual(sum(op == "verify" for op, _ in verifier.operations), 12)
        self.assertIn("10 proof repairs", report["failures"][0]["diagnostic"])
        entry = report["properties"][0]
        self.assertEqual(entry["status"], "resources exhausted")
        self.assertEqual(entry["formalized"], translation["original_ensures"])
        self.assertIn("ensures r == n", verifier.operations[1][1])
        for operation, source in verifier.operations[2:]:
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
        self.assertIn("6 syntax repairs", report["failures"][0]["diagnostic"])

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
        self.assertIn("frozen computation", report["failures"][0]["diagnostic"])

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
            self.assertTrue(report["failures"], change)

    def test_missing_external_context_abstains(self):
        bundle, claim, translation = fixture()
        translation["required_context"].append("src/missing.h")
        report = self.execute(bundle, claim, Chat([translation]), verifier=FailingVerifier())
        self.assertIn("Missing required source context", report["failures"][0]["diagnostic"])

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
        bundle["sources"][claim["path"]]["text"] = source.replace(
            "int f(int n) {", "int f(int n) { // method Target(n: int)")
        translation["model"] = translation["model"].replace(
            "| int f(int n) {", "| int f(int n) { // method Target(n: int)")
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, Chat([translation]), verifier=verifier)
        self.assertFalse(report["findings"])
        self.assertFalse(verifier.operations)
        self.assertIn("shadowed", report["failures"][0]["diagnostic"])

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
        self.assertEqual(report["failures"][0]["reason"], "resources exhausted")
        self.assertIn("Input-domain check", report["failures"][0]["diagnostic"])

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

    def test_report_shows_input_and_code_with_natural_language_specs(self):
        bundle, claim, translation = fixture("cpp")
        bundle["identity"]["source_kind"] = "pull-request-head"
        text = bundle["sources"][claim["path"]]["text"].splitlines()
        text[1] += "  // ``` @someone"
        text[3] = "  return r + g();"
        bundle["sources"][claim["path"]]["text"] = "\n".join(text + ["int g() {", "  return 1;", "}"]) + "\n"
        claim["id"] = "p1"
        translation["environment"] = [{"name": "G", "description": "g always returns one.",
                                       "kind": "source-derived",
                                       "source": {"path": claim["path"], "lines": [6, 8]}}]
        translation["source_notes"].extend([
            {"path": claim["path"], "line": 3, "kind": "invariant",
             "note": "The count equals the number of iterations completed."},
            {"path": claim["path"], "line": 6, "kind": "spec",
             "note": "No input restrictions are needed."},
        ])
        finding = {
            "kind": "conditional-model-refutation", "candidate": claim, "translation": translation,
            "source_map": [
                {"path": claim["path"], "line": 1, "source": text[0],
                 "model": "method Target(n: int) returns (r: int)"},
                {"path": claim["path"], "line": 4, "source": text[3], "model": "r := r + G();"},
                {"path": claim["path"], "line": 6, "source": "int g() {",
                 "model": "function G(): int"},
            ],
            "retained_assumptions": [0], "counters": {"syntax_repairs": 1, "proof_repairs": 0},
            "evidence": "p1/final/model.dfy", "dafny_sha256": "0" * 64, "dafny": "method M()\n{\n}\n",
            "result": {"outcome": "verified", "exit_code": 0, "mode": "unbounded",
                       "stdout": "Dafny program verifier finished with 2 verified, 0 errors"}}
        report = {"identity": bundle["identity"], "findings": [finding], "errors": []}
        with patch("nfv_workflow.general.validate_finding"):
            body = render(report, bundle, "https://github.com/local/synthetic/actions/runs/1")
        self.assertIn("# NFV: 1 potential issue in PR #1", body)
        self.assertIn("(https://arxiv.org/abs/2608.21516)", body)
        self.assertLess(body.index("[Neuro-formal verification"), body.index("# NFV:"))
        self.assertIn("### 1. Potential issue\n\n**Intended property:** The result equals n.", body)
        self.assertNotIn("## 1. The result equals n.", body)
        self.assertIn("> The result equals n.", body)
        self.assertIn("**Input on which it fails:** `n = 2`", body)
        self.assertIn("**Sketch of the failure:** The loop counts to the input and then adds one more.", body)
        self.assertNotIn("Source correspondence is not established", body)
        self.assertNotIn("modeled return", body)
        base = "https://github.com/local/synthetic/blob/" + "a" * 40 + "/src/example.cpp"
        self.assertIn(f"**Changed code** — [`src/example.cpp` lines 1-5]({base}#L1-L5)\n"
                      "Function: `int f(int n) {`", body)
        changed = body[body.index("````cpp\n"):body.index("**Helper definition**")]
        self.assertIn("1  int f(int n) {\n   // NFV: Spec (requires): Nonnegative iteration count\n"
                      "   // NFV: Spec (ensures, intended): The result equals n.\n"
                      "2    int r = 0;  // ``` @someone\n", changed)
        self.assertIn("3    for (int i=0; i<n; ++i) { ++r; }\n"
                      "     // NFV: Invariant: The count equals the number of iterations completed.", changed)
        # Untrusted source text stays inside a longer fence, so it cannot close the block.
        self.assertTrue(changed.rstrip().endswith("````"))
        self.assertIn(f"**Helper definition** — [`src/example.cpp` lines 6-8]({base}#L6-L8)\n"
                      "Function: `int g() {`\n\n```cpp\n6  int g() {\n"
                      "   // NFV: Spec: g always returns one.\n"
                      "   // NFV: Spec: No input restrictions are needed.\n7    return 1;\n8  }\n```", body)
        self.assertIn("**Call relationships (static call sites):**", body)
        self.assertIn(f"`int f(int n) {{` ([`src/example.cpp` lines 1-1]({base}#L1-L1))", body)
        self.assertIn(f"call site: [`src/example.cpp` lines 4-4]({base}#L4-L4)", body)
        self.assertIn("// NFV: Call site for the helper at src/example.cpp:6; see helper definition.", changed)
        self.assertNotIn("**Trace**", body)
        self.assertLess(body.index("**Sketch of the failure:**"), body.index("<summary>Details</summary>"))
        self.assertLess(body.index("<summary>Details</summary>"), body.index("**Relevant code**"))
        self.assertLess(body.index("**Helper definition**"), body.index("<summary>Minimized Dafny</summary>"))
        self.assertEqual(body.count("<details>"), 2)
        self.assertEqual(body.count("</details>"), 2)
        details = body[body.index("<details>"):]
        self.assertIn("```dafny\nmethod M()", details)

    def test_source_note_kinds_are_validated_and_legacy_notes_remain_supported(self):
        bundle, claim, translation = fixture()
        validate_translation(translation, claim, bundle)
        translation["source_notes"][0]["kind"] = "invariant"
        validate_translation(translation, claim, bundle)
        translation["source_notes"][0]["kind"] = "execution"
        with self.assertRaisesRegex(Abstain, "kind must be invariant or spec"):
            validate_translation(translation, claim, bundle)

    def test_natural_language_annotation_stays_inside_a_safe_source_fence(self):
        bundle = {"sources": {"src/example.py": {"text": "for x in xs:\n    pass\n"}}}
        _, block = source_block(bundle, "src/example.py", 1, 2, "python",
                                "local/synthetic", "a" * 40,
                                {("src/example.py", 1): ["Invariant: ```\n</details> @someone"]})
        self.assertTrue(block.startswith("````python\n"))
        self.assertIn("# NFV: Invariant: ``` </details> @someone\n", block)
        self.assertTrue(block.endswith("\n````"))

    def test_helper_spec_uses_its_own_source_lines_when_declaration_is_elsewhere(self):
        bundle, claim, translation = fixture()
        bundle["sources"]["src/helper.c"] = {"text": "int g() {\n  return 1;\n}\n"}
        translation["environment"] = [{"name": "G", "description": "g returns one.",
                                       "source": {"path": "src/helper.c", "lines": [1, 2, 3]}}]
        finding = {"translation": translation, "retained_assumptions": [0], "source_map": [
            {"path": claim["path"], "line": 1, "source": "int f(int n) {",
             "model": "method Target(n: int)"},
            {"path": "src/helper.h", "line": 100, "source": "int g();", "model": "function G(): int"},
        ]}
        body = "\n\n".join(relevant_code(claim, translation, finding, bundle,
                                        "local/synthetic", "a" * 40))
        self.assertIn("`src/helper.c` lines 1-3", body)
        self.assertIn("1  int g() {\n   // NFV: Spec: g returns one.", body)
        self.assertNotIn("lines 1-100", body)

    def test_source_calls_connect_nested_helpers_and_deduplicate_sites(self):
        def row(name, line, code):
            return {"path": "src/example.cpp", "line": line, "source": name, "model": code}
        finding = {
            "translation": {"environment": [{"name": "G"}, {"name": "H"}]},
            "source_map": [
                row("int f() {", 1, "method Target()"),
                row("return g();", 2, "r := G();"),
                row("return g();", 2, "r := G();"),
                row('const char* s = "H()";', 3, 'var s := "H()"; // G()'),
                row("return g();", 2, "assert H() == 1;"),
                row("int g() {", 4, "function G(): int"),
                row("return h();", 5, "H()"),
                row("int h() {", 7, "function H(): int"),
                row("return 1;", 8, "1"),
            ],
        }
        _, calls = source_calls(finding)
        self.assertEqual([(a["line"], site["line"], b["line"]) for a, site, b in calls],
                         [(1, 2, 4), (4, 5, 7)])

    def test_source_block_keeps_late_call_sites_and_excerpt_line_numbers(self):
        bundle = {"sources": {"src/example.py": {
            "line_start": 100, "text": "\n".join("    f()" for _ in range(70))}}}
        link, block = source_block(bundle, "src/example.py", 100, 169, "python",
                                  "local/synthetic", "a" * 40,
                                  {("src/example.py", 169): ["Call site for helper."]})
        self.assertIn("#L100-L169", link)
        self.assertIn("169      f()\n         # NFV: Call site for helper.", block)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_all_languages_with_fake_translation_and_real_dafny(self):
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
                self.assertIn("[Neuro-formal verification (NFV)](https://arxiv.org/abs/2608.21516)", body)
                self.assertIn("**Sketch of the failure:** The loop counts to the input and then adds one more.", body)
                self.assertNotIn("Source correspondence is not established", body)
                self.assertIn("Spec (ensures, intended): The result equals n.", body)
                source_line = bundle["sources"][claim["path"]]["text"].splitlines()[claim["lines"][0] - 1]
                self.assertIn(f"{claim['lines'][0]}  {source_line.rstrip()}", body)
                self.assertIn("<summary>Minimized Dafny</summary>", body)

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
    def test_captured_same_file_callee_body_is_checked_with_caller_context(self):
        bundle, claim, translation = fixture()
        path = claim["path"]
        bundle["sources"][path]["text"] = bundle["sources"][path]["text"].replace(
            "return r + 1;", "return bump(r);")
        bundle["sources"][path]["text"] += (
            "int bump(int x) {\n  return x + 1;\n}\n"
            "int wrapper() {\n  return f(100);\n}\n")
        model = translation["model"].replace("| return r + 1;", "| return bump(r);")
        position = model.rfind("r := r + 1;")
        model = model[:position] + "r := Bump(r);" + model[position + len("r := r + 1;"):]
        translation["model"] = (
            f"// src: {path}:6 | int bump(int x) {{\n"
            "function Bump(x: int): int\n// synth: helper body\n{\n"
            f"// src: {path}:7 | return x + 1;\n"
            "  x + 1\n// synth: helper end\n}\n" + model)
        translation["environment"] = [{
            "name": "Bump", "kind": "source-derived", "description": "Captured increment helper.",
            "source": {"path": path, "lines": [6, 7]}}]
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)
        finding = report["findings"][0]
        self.assertTrue(any(row["path"] == path and row["line"] == 7 for row in finding["source_map"]))
        self.assertEqual({"n": 2}, finding["translation"]["witness"])
        self.assertEqual([1, 5], finding["translation"]["source_span"])
        context = json.loads((self.directory / finding["candidate"]["id"] / "source-context.json").read_text())
        self.assertEqual({("bump", "callee"), ("wrapper", "caller")},
                         {(item["name"], item["role"]) for item in context["related_functions"]})

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_general_external_environment_body_is_checked(self):
        bundle, claim, translation = fixture()
        helper = "// Returns x + 1.\nint bump(int x) {\n  return x + 1;\n}\n"
        bundle["sources"]["src/helper.c"] = {"text": helper}
        bundle["sources"][claim["path"]]["text"] = bundle["sources"][claim["path"]]["text"].replace(
            "return r + 1;", "return bump(r);")
        model = translation["model"].replace("| return r + 1;", "| return bump(r);")
        position = model.rfind("r := r + 1;")
        model = model[:position] + "r := Bump(r);" + model[position + len("r := r + 1;"):]
        translation["model"] = (
            "// src: src/helper.c:2 | int bump(int x) {\n"
            "function Bump(x: int): int\n"
            "// synth: helper body\n{\n"
            "// src: src/helper.c:1 | // Returns x + 1.\n"
            "  x + 1\n// synth: helper end\n}\n" + model)
        translation["required_context"].append("src/helper.c")
        translation["environment"] = [{
            "name": "Bump", "kind": "inferred", "description": "External contract: returns x + 1.",
            "source": {"path": "src/helper.c", "lines": [1, 2]}}]
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
    def test_undefined_sequence_access_is_repaired_before_freezing(self):
        bundle, claim, translation = fixture()
        source = ["int f(int* values) {", "  // Requires a nonempty input array.",
                  "  return values[0];", "}"]
        bundle["sources"][claim["path"]]["text"] = "\n".join(source) + "\n"
        bundle["changed"][claim["path"]] = claim["lines"] = [3]
        claim["quote"] = claim["property"] = bundle["signals"][0]["text"] = "Returns the first array element."
        translation.update({
            "model": f"// src: {claim['path']}:1 | {source[0]}\n"
                     "method Target(values: seq<int>) returns (r: int)\n"
                     "// synth: intended postcondition\n  ensures r == values[0]\n"
                     "// synth: body\n{\n"
                     f"// src: {claim['path']}:3 | {source[2].strip()}\n"
                     "  r := values[0];\n// synth: end\n}\n",
            "original_ensures": ["r == values[0]"], "source_span": [1, 4],
            "assumptions": [], "source_notes": [],
        })
        translation.pop("witness")
        repaired = translation["model"].replace(
            "// synth: intended postcondition", "// synth: documented input domain\n"
            "  requires |values| > 0\n// synth: intended postcondition")
        conditions = [{"clause": "|values| > 0", "description": "The input array is nonempty.",
                       "origin": "inferred", "optional": False}]
        report = self.execute(bundle, claim, Chat([translation, {"model": repaired,
                              "assumptions": conditions}]), limits=Limits(proof_rounds=0))
        self.assertFalse(report["failures"], report)
        self.assertEqual(len(report["verified"]), 1, report)
        self.assertEqual(report["verified"][0]["translation"]["original_ensures"], ["r == values[0]"])
        self.assertEqual(report["verified"][0]["counters"]["syntax_repairs"], 1)

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
    def test_unrelated_type_repair_cannot_replace_a_valid_goal(self):
        bundle, claim, correct = fixture()
        initial = copy.deepcopy(correct)
        initial["model"] = initial["model"].replace("r := 0;", "r := false;")
        wrong = correct["model"].replace("ensures r == n", "ensures r != n")
        report = self.execute(bundle, claim, Chat([initial, {"model": wrong}]), limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["translation"]["original_ensures"], ["r == n"])
        self.assertNotIn("ensures r != n", report["findings"][0]["translation"]["model"])

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_unrelated_type_repair_cannot_drop_a_valid_goal(self):
        bundle, claim, correct = fixture()
        initial = copy.deepcopy(correct)
        initial["model"] = initial["model"].replace("r := 0;", "r := false;")
        missing = correct["model"].replace(
            "// synth: modeled contract or structural syntax\n  ensures r == n\n", "")
        report = self.execute(bundle, claim, Chat([initial, {"model": missing}]),
                              limits=Limits(proof_rounds=0))
        self.assertEqual(len(report["findings"]), 1, report)
        self.assertEqual(report["findings"][0]["translation"]["original_ensures"], ["r == n"])
        self.assertEqual(report["findings"][0]["counters"]["syntax_repairs"], 1)

    @unittest.skipUnless(shutil.which("dafny"), "Requires real Dafny")
    def test_holding_modeled_property_is_verified_without_a_witness(self):
        bundle, claim, translation = fixture()
        bundle["identity"]["source_kind"] = "pull-request-head"
        bundle["sources"][claim["path"]]["text"] = bundle["sources"][claim["path"]]["text"].replace(
            "return r + 1;", "return r;")
        model = translation["model"].replace("| return r + 1;", "| return r;")
        position = model.rfind("r := r + 1;")
        translation["model"] = model[:position] + "r := r;" + model[position + len("r := r + 1;"):]
        del translation["witness"]
        report = self.execute(bundle, claim, Chat([translation]), limits=Limits(proof_rounds=0))
        self.assertFalse(report["findings"])
        self.assertFalse(report["failures"], report)
        self.assertEqual(len(report["verified"]), 1, report)
        self.assertEqual(report["properties"][0]["status"], "verified")
        body = render(report, bundle, "https://github.com/local/synthetic/actions/runs/123")
        self.assertIn("verified for the translated model", body)
        self.assertIn("not proofs of source-code correctness", body)

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
