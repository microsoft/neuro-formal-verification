import asyncio
import copy
import json
from pathlib import Path
import shutil
import unittest
import uuid

from nfv_workflow.general import Limits, run
from nfv_workflow.github import Abstain
from nfv_workflow.source_context import (CONTEXT_BYTES, REQUEST_BYTES, Function,
                                         caller_first, check_request, diagnostics,
                                         functions, mask, root_levels, scoped_context, size)
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
                         "\n\n" + "\n".join(f"{101 + i}| {line}" for i, line in enumerate(source.splitlines())),
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

    def test_rust_continued_strings_preserve_function_boundaries_and_offsets(self):
        for newline in ("\n", "\r\n"):
            for prefix, value_type in (("", "&str"), ("b", "&[u8]")):
                with self.subTest(newline=repr(newline), prefix=prefix):
                    source = (f'const TEXT: {value_type} = {prefix}"first {{ \\\n'
                              '    continuation }";\n'
                              'fn changed() {\n'
                              '    let text = "later";\n'
                              '}\n').replace("\n", newline)
                    clean = mask(source, "rust")
                    self.assertEqual(len(source), len(clean))
                    self.assertEqual([i for i, c in enumerate(source) if c == "\n"],
                                     [i for i, c in enumerate(clean) if c == "\n"])
                    self.assertNotIn("continuation", clean)
                    self.assertEqual((1, 1), (clean.count("{"), clean.count("}")))
                    definitions = functions({"text": source, "line_start": 101}, "rust")
                    self.assertEqual([("changed", 103, 103, 103, 105)],
                                     [(fn.name, fn.first, fn.declaration, fn.body, fn.last)
                                      for fn in definitions])

    def test_rust_continued_attribute_in_repository_context(self):
        helper = ('#![allow(dead_code, reason = "first line \\\n'
                  '    continuation")]\n'
                  'pub(crate) enum Error {\n'
                  '    Unexpected,\n'
                  '}\n'
                  'fn helper(n: i32) -> i32 {\n'
                  '    let _ = "later";\n'
                  '    n\n'
                  '}\n')
        bundle = {
            "sources": {
                "main.rs": {"text": "fn changed(n: i32) -> i32 {\n    helper(n)\n}\n"},
                "helper.rs": {"text": helper},
            },
            "repository_context": [{"path": "helper.rs", "symbols": ["helper"]}],
        }
        context = scoped_context(bundle, {"path": "main.rs", "lines": [2], "language": "rust"})
        self.assertEqual("changed", context["source_scope"]["function"])
        self.assertIn(("helper.rs", "helper"),
                      [(item["path"], item["name"]) for item in context["environment_declarations"]])
        self.assertIn('7|     let _ = "later";', context["sources"]["helper.rs"]["numbered_lines"])

    def test_rust_raw_bytes_characters_and_lifetimes(self):
        source = ("fn changed<'a>(n: &'a i32) -> i32 {\n"
                  '    let _ = br##"} " { "# "##;\n'
                  "    let _ = (b'{', '}', '\\'', '\\n');\n"
                  "    *n\n"
                  "}\n")
        definitions = functions({"text": source}, "rust")
        self.assertEqual([("changed", 1, 5)],
                         [(fn.name, fn.declaration, fn.last) for fn in definitions])
        self.assertIn("&'a i32", mask(source, "rust"))
        self.assertEqual((1, 1), (mask(source, "rust").count("{"), mask(source, "rust").count("}")))

    def test_rust_visibility_and_function_types_are_not_function_definitions(self):
        source = ("pub(crate) struct Data {\n"
                  "    callback: fn(i32) -> i32,\n"
                  "}\n"
                  "pub(super) enum Choice {\n"
                  "    Value(i32),\n"
                  "}\n"
                  "impl Data {\n"
                  "    pub(crate) async fn changed<'a>(&'a self) -> i32 {\n"
                  "        let callback: fn(i32) -> i32 = |n| { n + 1 };\n"
                  "        callback(1)\n"
                  "    }\n"
                  "}\n")
        self.assertEqual(["changed"], [fn.name for fn in functions({"text": source}, "rust")])

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

    def test_omission_notices_cannot_overflow_a_bounded_repository_context(self):
        for padding in ("x" * 30000, "\N{EURO SIGN}" * 10000):
            with self.subTest(multibyte=padding.startswith("\N{EURO SIGN}")):
                helpers = "\n".join(
                    f"struct Helper{i} {{\n int helper(int n) {{\n // {padding}\n return n;\n }}\n}};\n"
                    for i in range(64))
                bundle = {"sources": {"main": {"text": "int target(int n) {\n return n;\n}\n"},
                                      "helpers.cpp": {"text": helpers}},
                          "repository_context": [{"path": "helpers.cpp", "symbols": ["helper"]}]}
                context = scoped_context(bundle, {"path": "main", "lines": [2], "language": "cpp"})
                self.assertLessEqual(size(context), CONTEXT_BYTES)
                self.assertIn("2|  return n;", context["sources"]["main"]["numbered_lines"])
                self.assertIn(padding, context["sources"]["helpers.cpp"]["numbered_lines"])
                self.assertIn("additional context-omission notices", context["context_omissions"][-1])

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

    def test_caller_property_runs_first_without_changing_its_entry_or_callee_claim(self):
        bundle, claim, _ = fixture()
        path = claim["path"]
        bundle["sources"][path]["text"] = (
            "int leaf(int n) {\n return n;\n}\n"
            "int root(int n) {\n return leaf(n);\n}\n")
        bundle["changed"][path] = [2, 5]
        bundle["signals"][0]["text"] = "Leaf returns n. Root returns n."
        leaf = {**claim, "lines": [2], "quote": "Leaf returns n.", "property": "Leaf returns n."}
        root = {**claim, "lines": [5], "quote": "Root returns n.", "property": "Root returns n."}
        chat = Chat([{"abstain": "caller inspected"}, {"abstain": "callee inspected"}])
        report = asyncio.run(run(bundle, [leaf, root], self.directory, chat, verifier=FailingVerifier()))
        self.assertEqual([[5], [2]], [item["lines"] for item in report["properties"]])
        self.assertEqual(["Root returns n.", "Leaf returns n."],
                         [item["property"] for item in report["properties"]])
        scopes = [json.loads(prompt.split("\n", 1)[1])["source_scope"]["function"]
                  for prompt in chat.prompts]
        self.assertEqual(["root", "leaf"], scopes)

    def test_large_metadata_stops_syntax_repair_before_inference(self):
        bundle, claim, translation = fixture()
        translation["correspondence"] += "x" * REQUEST_BYTES

        class SyntaxFailure(FailingVerifier):
            def resolve(self, source, directory):
                return {"outcome": "type_error", "exit_code": 2, "mode": "typecheck"}

        chat = Chat([translation])
        report = self.execute(bundle, claim, chat, SyntaxFailure())
        self.assertEqual(1, len(chat.prompts))
        self.assertIn("syntax-repair request exceeds", report["failures"][0]["diagnostic"])

    def test_large_model_stops_proof_repair_before_inference(self):
        bundle, claim, translation = fixture()
        translation["model"] += "// " + "x" * REQUEST_BYTES + "\n"
        chat = Chat([translation])
        report = self.execute(bundle, claim, chat, FailingVerifier())
        self.assertEqual(1, len(chat.prompts))
        self.assertIn("proof-repair request exceeds", report["failures"][0]["diagnostic"])

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
        self.assertIn("Type checking failed", report["failures"][0]["diagnostic"])
        syntax_payload = json.loads(chat.prompts[1])

        verifier = VerboseVerifier()
        verifier.resolve = FailingVerifier().resolve
        chat = Chat([translation, {"annotations": []}])
        report = self.execute(bundle, claim, chat, verifier, limits=Limits(proof_rounds=1))
        self.assertIn("Neither verification nor refutation succeeded", report["failures"][0]["diagnostic"])
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
        self.assertIn("escapes the scoped changed function", report["failures"][0]["diagnostic"])

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
        self.assertIn("outside the supplied scoped context", report["failures"][0]["diagnostic"])
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
        self.assertIn("Environment contract cites source outside", report["failures"][0]["diagnostic"])
        self.assertFalse(verifier.operations)


class CallerOrderTests(unittest.TestCase):
    def order(self, source, lines):
        bundle = {"sources": {"main.cpp": {"text": source}}}
        claims = [{"path": "main.cpp", "lines": [line], "language": "cpp"} for line in lines]
        original = copy.deepcopy(claims)
        ordered = caller_first(bundle, claims)
        self.assertEqual(original, claims)
        return [item["lines"][0] for item in ordered]

    def test_caller_first_through_unchanged_intermediary(self):
        source = ("int leaf() {\n return 1;\n}\n"
                  "int middle() {\n return leaf();\n}\n"
                  "int root() {\n return middle();\n}\n")
        self.assertEqual([8, 2], self.order(source, [2, 8]))

    def test_multiple_roots_stable_ties_and_recursion(self):
        source = ("int a() {\n return b();\n}\n"
                  "int b() {\n return a();\n}\n"
                  "int root() {\n return a();\n}\n"
                  "int independent() {\n return 1;\n}\n")
        self.assertEqual([11, 8, 5, 2], self.order(source, [5, 11, 2, 8]))

    def test_shortcut_does_not_move_callee_before_changed_caller(self):
        source = ("int leaf() {\n return 1;\n}\n"
                  "int parent() {\n return leaf();\n}\n"
                  "int middle() {\n return parent();\n}\n"
                  "int root() {\n return middle() + leaf();\n}\n")
        self.assertEqual([5, 2], self.order(source, [2, 5]))

    def test_unscopable_property_remains_for_explicit_abstention(self):
        source = "int leaf() {\n return 1;\n}\nint root() {\n return leaf();\n}\n"
        self.assertEqual([5, 2, 999], self.order(source, [999, 2, 5]))

    def test_same_names_in_different_files_do_not_create_edges(self):
        bundle = {"sources": {
            "one.cpp": {"text": "int caller() {\n return callee();\n}\n"},
            "two.cpp": {"text": "int callee() {\n return 1;\n}\n"},
        }}
        claims = [{"path": path, "lines": [2], "language": "cpp"} for path in ("two.cpp", "one.cpp")]
        self.assertEqual(claims, caller_first(bundle, claims))

    def test_deep_graph_does_not_depend_on_python_recursion_limit(self):
        definitions = [Function(f"f{i}", i + 1, i + 1, i + 1, i + 1,
                                frozenset({f"f{i + 1}"}) if i < 1499 else frozenset())
                       for i in range(1500)]
        levels = root_levels(definitions)
        self.assertEqual(0, levels[definitions[0]])
        self.assertEqual(1499, levels[definitions[-1]])
