import asyncio
import copy
import json
from pathlib import Path
import shutil
import unittest
import uuid

from nfv_workflow.general import Limits, run
from nfv_workflow.github import Abstain
from nfv_workflow.source_context import (CONTEXT_BYTES, REQUEST_BYTES, check_request,
                                         diagnostics, scoped_context, size)
from test_general import Chat, FailingVerifier, SOURCES, fixture


class SourceContextTests(unittest.TestCase):
    def context(self, source, anchors, language="cpp", line_start=1, extra=None):
        bundle = {"sources": {"main": {"text": source, "line_start": line_start}, **(extra or {})}}
        claim = {"path": "main", "lines": anchors, "language": language}
        return scoped_context(bundle, claim)

    def test_all_supported_languages(self):
        for language in SOURCES:
            with self.subTest(language=language):
                bundle, claim, _ = fixture(language)
                context = scoped_context(bundle, claim)
                self.assertEqual("F" if language == "csharp" else "f",
                                 context["source_scope"]["function"])
                self.assertIn(f"{claim['lines'][0]}|", context["sources"][claim["path"]]["numbered_lines"])
                self.assertLessEqual(size(context), CONTEXT_BYTES)

    def test_multiline_method_and_original_numbers(self):
        source = ("class C {\n// A documented method.\nint C::changed(\n"
                  "  int n\n) const\n{\n  return helper(n);\n}\n};\n")
        context = self.context(source, [107], line_start=101)
        scope = context["source_scope"]
        self.assertEqual(("changed", 102, 108), (scope["function"], scope["first"], scope["last"]))
        self.assertEqual("102| // A documented method.\n103| int C::changed(\n"
                         "104|   int n\n105| ) const\n106| {\n107|   return helper(n);\n108| }"
                         "\n\n101| class C {",
                         context["sources"]["main"]["numbered_lines"])

    def test_strings_comments_raw_strings_and_nested_blocks(self):
        source = ('int f(int n) {\n const char* x = R"tag({ " } // )tag";\n'
                  ' // } ignored\n /* { ignored } */\n if (n) {\n'
                  '   const char* y = "} {";\n   return n;\n }\n return 0;\n}\n')
        context = self.context(source, [7])
        self.assertEqual(("f", 10), (context["source_scope"]["function"], context["source_scope"]["last"]))
        self.assertEqual([], context["related_functions"])

    def test_initializer_call_is_not_a_function(self):
        context = self.context("void target() {\n defs.push_back({1, 2});\n}\n", [2])
        self.assertEqual("target", context["source_scope"]["function"])
        self.assertEqual([], context["related_functions"])

    def test_used_type_and_constant_declarations_are_environment(self):
        source = ("class Example {\n static class Cell { int value; }\n"
                  " static const int Limit = 8;\n static int target(int x) {\n"
                  "  Cell c = new Cell();\n  c.value = x + Limit;\n  return c.value;\n }\n}\n")
        context = self.context(source, [6], "java")
        self.assertEqual({"Cell", "Limit"}, {x["name"] for x in context["environment_declarations"]})
        self.assertIn("2|  static class Cell", context["sources"]["main"]["numbered_lines"])

    def test_methods_receivers_arrows_and_rust_lifetimes(self):
        cases = [
            ("go", "func (r *Receiver) Changed(n int) int {\n return n\n}\n", "Changed"),
            ("typescript", "const changed = (n: number): number => {\n return n;\n};\n", "changed"),
            ("javascript", "class C {\n changed(n) {\n return n;\n }\n}\n", "changed"),
            ("java", "@Override\npublic int changed(int n) {\n return n;\n}\n", "changed"),
            ("rust", "fn changed<'a, 'b>(n: &'a i32, m: &'b i32) -> i32 {\n *n + *m\n}\n", "changed"),
            ("rust", 'fn changed(n: i32) -> i32 {\n let _ = r#" } " { "#; n\n}\n', "changed"),
        ]
        for language, source, name in cases:
            with self.subTest(language=language):
                anchor = 3 if language in ("javascript", "java") else 2
                self.assertEqual(name, self.context(source, [anchor], language)["source_scope"]["function"])

    def test_python_nested_function_and_decorators(self):
        source = ("class C:\n    # Documentation\n    @decorate\n    def outer(self):\n"
                  "        def inner(n):\n            return n\n        return inner(1)\n")
        context = self.context(source, [7], "python")
        self.assertEqual(("outer", 2, 7), (context["source_scope"]["function"],
                                         context["source_scope"]["first"], context["source_scope"]["last"]))
        context = self.context(source, [6], "python")
        self.assertEqual("inner", context["source_scope"]["function"])

    def test_few_same_file_neighbors_and_no_recursive_expansion(self):
        source = ("int third(int n) {\n return n;\n}\n"
                  "int second(int n) {\n return third(n);\n}\n"
                  "int helper(int n) {\n return second(n);\n}\n"
                  "int target(int n) {\n return helper(n) + second(n) + third(n) + remote(n);\n}\n"
                  "int caller1(int n) {\n return target(n);\n}\n"
                  "int caller2(int n) {\n return target(n);\n}\n"
                  "int caller3(int n) {\n return target(n);\n}\n")
        context = self.context(source, [11])
        neighbors = context["related_functions"]
        self.assertEqual({"helper", "second"}, {x["name"] for x in neighbors if x["role"] == "callee"})
        self.assertEqual({"caller1", "caller2"}, {x["name"] for x in neighbors if x["role"] == "caller"})
        self.assertEqual(["remote", "third"], context["callee_names"])
        self.assertNotIn("20|", context["sources"]["main"]["numbered_lines"])
        self.assertNotIn("2| return n;", context["sources"]["main"]["numbered_lines"])
        self.assertEqual([["main", "third"]], [[x["path"], x["name"]] for x in context["environment_declarations"]])

    def test_external_calls_get_only_declaration_and_comments(self):
        extra = {
            "external.cpp": {"text": "// Returns a nonnegative value.\nint remote(int n) {\n return 999;\n}\n"},
            "external.h": {"text": "// Library contract.\nint remote(int n);\n"},
            "unrelated.cpp": {"text": "int other(int n) {\n return 888;\n}\n"},
        }
        context = self.context("int target(int n) {\n return remote(n);\n}\n", [2], extra=extra)
        self.assertEqual([], context["related_functions"])
        self.assertIn("nonnegative", context["sources"]["external.cpp"]["numbered_lines"])
        self.assertIn("Library contract", context["sources"]["external.h"]["numbered_lines"])
        self.assertNotIn("999", json.dumps(context))
        self.assertNotIn("888", json.dumps(context))
        self.assertEqual(["remote"], context["callee_names"])

    def test_inline_external_body_is_not_supplied(self):
        for source in ("int remote(int n) { return 999; }\n",
                       "int remote(int n) { int secret = 999; if (n) {\n return secret;\n}\nreturn n;\n}\n"):
            with self.subTest(source=source):
                context = self.context("int target(int n) {\n return remote(n);\n}\n", [2],
                                       extra={"external.cpp": {"text": source}})
                self.assertNotIn("999", json.dumps(context))

    def test_too_large_changed_function_abstains(self):
        source = "int target(int n) {\n" + " // lots of context\n" * 3000 + " return n;\n}\n"
        with self.assertRaisesRegex(Abstain, "Changed function exceeds"):
            self.context(source, [3002])

    def test_large_secondary_body_is_omitted(self):
        source = ("int helper(int n) {\n" + " // large body\n" * 4000 + " return n;\n}\n"
                  "int target(int n) {\n return helper(n);\n}\n")
        context = self.context(source, [4005])
        self.assertEqual([], context["related_functions"])
        self.assertIn("callee body omitted", " ".join(context["context_omissions"]))
        self.assertLessEqual(size(context), CONTEXT_BYTES)
        self.assertNotIn("large body", json.dumps(context))

    def test_separate_functions_and_ambiguous_boundaries_abstain(self):
        with self.assertRaisesRegex(Abstain, "one identifiable function"):
            self.context("int f() {\n return 1;\n}\nint g() {\n return 2;\n}\n", [2, 5])
        with self.assertRaisesRegex(Abstain, "unbalanced"):
            self.context("int f() {\n return 1;\n}\n}\n", [2])
        with self.assertRaisesRegex(Abstain, "incomplete"):
            self.context("int f() {\n return 1;\n", [2])
        with self.assertRaisesRegex(Abstain, "ambiguous"):
            self.context("int f() { return 1; } int g() { return 2; }\n", [1])
        with self.assertRaisesRegex(Abstain, "one identifiable function"):
            self.context("def outer():\n def f():\n  return 1\n def g():\n  return 2\n", [3, 5], "python")

    def test_unicode_request_size_and_system_are_counted(self):
        check_request("x" * 100, "a" * (REQUEST_BYTES - 100), "translation")
        for stage in ("translation", "syntax-repair", "proof-repair"):
            with self.subTest(stage=stage), self.assertRaisesRegex(Abstain, stage + " request exceeds"):
                check_request("x" * 100, "\N{EURO SIGN}" * (REQUEST_BYTES // 3), stage)

    def test_unicode_diagnostics_are_bounded_and_full_result_is_untouched(self):
        result = {"outcome": "failed", "exit_code": 4, "mode": "unbounded",
                  "stdout": "begin\n" + "\N{EURO SIGN}" * 30000 + "\nend", "stderr": "short"}
        original = copy.deepcopy(result)
        shortened = diagnostics(result)
        self.assertLess(len(shortened["stdout"].encode("utf-8")), 8000)
        self.assertTrue(shortened["stdout"].startswith("begin"))
        self.assertTrue(shortened["stdout"].endswith("end"))
        self.assertIn("omitted from repair prompt", shortened["stdout"])
        self.assertEqual(original, result)


class RequestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / ".general-scratch" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.directory)
        try:
            self.directory.parent.rmdir()
        except OSError:
            pass

    def execute(self, bundle, claim, chat, verifier, **kwargs):
        return asyncio.run(run(bundle, [claim], self.directory, chat, verifier=verifier, **kwargs))

    def test_large_metadata_stops_syntax_repair_before_inference(self):
        bundle, claim, translation = fixture()
        translation["correspondence"] += "x" * REQUEST_BYTES

        class SyntaxFailure(FailingVerifier):
            def resolve(self, source, directory):
                return {"outcome": "type_error", "exit_code": 2, "mode": "typecheck"}

        chat = Chat([translation])
        report = self.execute(bundle, claim, chat, SyntaxFailure())
        self.assertEqual(1, len(chat.prompts))
        self.assertIn("syntax-repair request exceeds", report["abstentions"][0]["reason"])

    def test_large_model_stops_proof_repair_before_inference(self):
        bundle, claim, translation = fixture()
        translation["model"] += "// " + "x" * REQUEST_BYTES + "\n"
        chat = Chat([translation])
        report = self.execute(bundle, claim, chat, FailingVerifier())
        self.assertEqual(1, len(chat.prompts))
        self.assertIn("proof-repair request exceeds", report["abstentions"][0]["reason"])

    def test_both_repairs_reuse_scope_and_shorten_verifier_logs(self):
        bundle, claim, translation = fixture()
        bundle["sources"][claim["path"]]["text"] += "int unrelated() {\n return 999;\n}\n"

        class VerboseVerifier(FailingVerifier):
            def resolve(self, source, directory):
                return {"outcome": "type_error", "exit_code": 2, "mode": "typecheck",
                        "stdout": "x" * 150000, "stderr": ""}

            def __call__(self, source, directory):
                return {"outcome": "failed", "exit_code": 4, "mode": "unbounded",
                        "stdout": "x" * 150000, "stderr": ""}

        chat = Chat([translation, {"model": translation["model"]}])
        report = self.execute(bundle, claim, chat, VerboseVerifier(), limits=Limits(syntax_rounds=1))
        self.assertIn("Type checking failed", report["abstentions"][0]["reason"])
        syntax_payload = json.loads(chat.prompts[1])

        verifier = VerboseVerifier()
        verifier.resolve = FailingVerifier().resolve
        chat = Chat([translation, {"annotations": []}])
        report = self.execute(bundle, claim, chat, verifier, limits=Limits(proof_rounds=1))
        self.assertIn("Negative check failed", report["abstentions"][0]["reason"])
        proof_payload = json.loads(chat.prompts[1])

        self.assertEqual(syntax_payload["source_context"], proof_payload["source_context"])
        self.assertNotIn("unrelated", json.dumps(syntax_payload["source_context"]))
        for payload, key in ((syntax_payload, "typecheck"), (proof_payload, "negative_check")):
            self.assertIn("omitted from repair prompt", payload[key]["stdout"])
            self.assertLess(size(payload), REQUEST_BYTES)

    def test_translation_cannot_expand_scope_to_another_function(self):
        bundle, claim, translation = fixture()
        bundle["sources"][claim["path"]]["text"] += "int other() {\n return 999;\n}\n"
        translation["source_span"][1] = 6
        # Cover the extra source line without introducing another helper.
        translation["model"] = translation["model"].replace(
            "// synth: modeled contract or structural syntax\n}\n",
            f"// src: {claim['path']}:6 | int other() {{\n  r := r;\n"
            "// synth: modeled contract or structural syntax\n}\n")
        chat = Chat([translation])
        report = self.execute(bundle, claim, chat, FailingVerifier())
        self.assertIn("escapes the scoped changed function", report["abstentions"][0]["reason"])

    def test_translation_cannot_cite_omitted_external_computation(self):
        bundle, claim, translation = fixture()
        bundle["sources"]["remote.c"] = {"text": "int remote(int x) {\n return x + 1;\n}\n"}
        bundle["sources"][claim["path"]]["text"] = bundle["sources"][claim["path"]]["text"].replace(
            "return r + 1;", "return remote(r);")
        translation["model"] = translation["model"].replace("| return r + 1;", "| return remote(r);")
        translation["model"] = (
            "// src: remote.c:1 | int remote(int x) {\nfunction Remote(x: int): int\n"
            "// synth: body\n{\n// src: remote.c:2 | return x + 1;\n x + 1\n"
            "// synth: body\n}\n" + translation["model"])
        translation["required_context"].append("remote.c")
        translation["environment"] = [{"name": "Remote", "kind": "source-derived",
                                      "description": "Increment x.",
                                      "source": {"path": "remote.c", "lines": [1, 2]}}]
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, Chat([translation]), verifier)
        self.assertIn("outside the supplied scoped context", report["abstentions"][0]["reason"])
        self.assertFalse(verifier.operations)

    def test_environment_cannot_claim_an_omitted_implementation_contract(self):
        bundle, claim, translation = fixture()
        bundle["sources"]["remote.c"] = {"text": "int remote(int x) {\n return x + 1;\n}\n"}
        bundle["sources"][claim["path"]]["text"] = bundle["sources"][claim["path"]]["text"].replace(
            "return r + 1;", "return remote(r);")
        translation["model"] = translation["model"].replace("| return r + 1;", "| return remote(r);")
        translation["model"] = (
            "// src: remote.c:1 | int remote(int x) {\nfunction Remote(x: int): int\n"
            "// synth: body\n{\n"
            "// src: src/example.c:4 | return remote(r);\n x + 1\n"
            "// synth: body\n}\n" + translation["model"])
        translation["required_context"].append("remote.c")
        translation["environment"] = [{"name": "Remote", "kind": "source-derived",
                                      "description": "Increment x.",
                                      "source": {"path": "remote.c", "lines": [1, 2]}}]
        verifier = FailingVerifier()
        report = self.execute(bundle, claim, Chat([translation]), verifier)
        self.assertIn("Environment contract cites source outside", report["abstentions"][0]["reason"])
        self.assertFalse(verifier.operations)
