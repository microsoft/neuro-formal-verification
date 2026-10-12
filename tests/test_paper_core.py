from contextlib import redirect_stdout
from dataclasses import replace
import io
import inspect
import json
from pathlib import Path, PurePosixPath
import shutil
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import uuid

from nfv.agent import integrity, refute
from nfv.backends import dafny
from nfv.backends.registry import BACKENDS, get
from nfv.data import build_safe, killing_tests
from nfv.metrics import compute
from nfv.shared.config import DAFNY_PATH, Z3_PATH


MODEL = """// src: sample.cpp:1 | int f(int n) {
method f(n: int) returns (r: int)
// synth: original property
  ensures r > n
{
// src: sample.cpp:2 | return n;
  r := n;
}
"""


def candidate(witness="n == 0"):
    backend = get("dafny-theory-lemma4")
    inverted = refute.invert_entry_spec(MODEL, "f", backend)
    return refute.install_precondition(inverted.model, "f", [witness], backend)


class CoreTests(unittest.TestCase):
    def test_ubuntu_dafny_and_z3_lookup(self):
        from nfv.shared import config

        class LinuxPath(PurePosixPath):
            @classmethod
            def home(cls):
                return cls("/home/nfv")

            def exists(self):
                return str(self) in {
                    "/home/nfv/.dotnet/tools/dafny",
                    "/opt/dafny/z3/bin/z3-4.12.1",
                }

            def is_dir(self):
                return False

        environment = {"NFV_Z3_PATH": "/opt/dafny/z3/bin/z3-4.12.1"}
        with patch.object(config, "os", SimpleNamespace(name="posix", environ=environment)):
            with patch.object(config, "Path", LinuxPath):
                with patch.object(config.shutil, "which", return_value="/opt/dafny/dafny"):
                    self.assertEqual(config.find_tool("dafny"), "/opt/dafny/dafny")
                with patch.object(config.shutil, "which", return_value=None):
                    self.assertEqual(config.find_tool("dafny"), "/home/nfv/.dotnet/tools/dafny")
                    self.assertEqual(config.find_z3(), environment["NFV_Z3_PATH"])
                environment.clear()
                with patch.object(config.shutil, "which", return_value="/usr/bin/z3"):
                    self.assertEqual(config.find_z3(), "/usr/bin/z3")

    def test_ubuntu_cbmc_lookup_does_not_probe_windows_executable(self):
        from nfv.shared import config
        with patch.object(config, "os", SimpleNamespace(name="posix", environ={})):
            with patch.object(config, "Path", side_effect=AssertionError("Windows-only lookup")):
                with patch.object(config.shutil, "which", return_value="/usr/bin/cbmc"):
                    self.assertEqual(config.find_cbmc(), "/usr/bin/cbmc")

    def test_pinned_sdk_uses_retained_keyword_only_api(self):
        from copilot import CopilotClient
        from copilot.session import CopilotSession
        inspect.signature(CopilotClient).bind(log_level="error")
        inspect.signature(CopilotClient.create_session).bind(
            object(), model="gpt-5.6-sol",
            system_message={"mode": "replace", "content": "system"},
            available_tools=[])
        inspect.signature(CopilotSession.send_and_wait).bind(
            object(), "prompt", timeout=600.0)
        self.assertTrue(callable(CopilotSession.disconnect))

    def test_cbmc_does_not_require_msvc_on_non_windows(self):
        from nfv.shared import config
        with patch.object(config.os, "name", "posix"):
            self.assertIsInstance(config._build_msvc_env(), dict)

    def test_only_paper_backends_are_registered(self):
        self.assertEqual(set(BACKENDS), {
            "dafny", "dafny-naive", "dafny-theory", "dafny-theory-lemma4",
            "cbmc", "cbmc-theory"})
        with self.assertRaises(ValueError):
            get("boogie")
        with self.assertRaises(ValueError):
            get("cbmc-theory").run("", mode="unbounded")

    def test_lemma4_and_naive_variants_preserve_semantics(self):
        base, lemma = get("dafny-theory"), get("dafny-theory-lemma4")
        self.assertTrue(lemma.library_theory and lemma.phase0_precondition)
        self.assertFalse(lemma.abduce_definedness)
        self.assertTrue(lemma.proof_guide.startswith(base.proof_guide))
        self.assertEqual(replace(lemma, name=base.name, proof_guide=base.proof_guide), base)
        self.assertTrue(get("dafny-naive").naive)
        self.assertFalse(get("dafny-naive").library_theory)

    def test_negation_and_freeze(self):
        backend = get("dafny-theory-lemma4")
        inversion = refute.invert_entry_spec(MODEL, "f", backend)
        self.assertTrue(inversion.ok)
        self.assertEqual(refute.entry_ensures(candidate(), "f", backend), [inversion.inverted])
        self.assertTrue(integrity.check_freeze(MODEL, candidate()).ok)
        self.assertFalse(integrity.check_freeze(MODEL, candidate().replace("r := n;", "r := n+1;")).ok)

    def test_foreign_witness_is_not_clean_refutation(self):
        self.assertEqual(refute.witness_foreign_symbols(["Helper(n)"], ["n"]), ["Helper"])

    @unittest.skipUnless(DAFNY_PATH and Z3_PATH, "Dafny and matching Z3 not installed")
    def test_real_dafny_negation_and_vacuity_gate(self):
        backend = get("dafny-theory-lemma4")
        result = backend.run(candidate(), mode="unbounded", timeout=30)
        self.assertIs(result.outcome, dafny.Outcome.VERIFIED)
        self.assertTrue(refute.reachable(candidate(), "f", backend, timeout=30)[0])
        self.assertFalse(refute.reachable(candidate("n != n"), "f", backend, timeout=30)[0])


class AccountPolicyTests(unittest.TestCase):
    def test_arbitrary_developer_is_allowed_without_author_default(self):
        from nfv.shared import account
        with patch.dict(account.os.environ, {}, clear=True):
            self.assertEqual(account.check_account(account.Account("another-developer")), [])

    def test_explicit_login_pin_is_enforced(self):
        from nfv.shared import account
        with patch.dict(account.os.environ, {"NFV_EXPECTED_LOGIN": "another-developer"}, clear=True):
            self.assertEqual(account.check_account(account.Account("another-developer")), [])
            with self.assertRaises(account.AccountError):
                account.check_account(account.Account("different-developer"))
            with self.assertRaises(account.AccountError):
                account.check_account(None)

    def test_workflow_emu_opt_in_uses_environment(self):
        from nfv.shared import account
        emu = account.Account("developer_enterprise")
        with patch.dict(account.os.environ, {}, clear=True):
            with self.assertRaises(account.AccountError):
                account.check_account(emu)
        with patch.dict(account.os.environ, {"NFV_ALLOW_EMU": "1"}, clear=True):
            self.assertTrue(account.check_account(emu, allow_emu=None))
            with self.assertRaises(account.AccountError):
                account.check_account(emu, allow_emu=False)
        with patch.dict(account.os.environ, {}, clear=True):
            self.assertTrue(account.check_account(emu, allow_emu=True))


class DatasetTests(unittest.TestCase):
    def test_sanitized_entries_rebuild_exactly(self):
        frozen = json.loads(Path(build_safe.OUTPUT_PATH).read_text(encoding="utf-8"))
        with redirect_stdout(io.StringIO()):
            rebuilt = build_safe.build()
            build_safe.self_check(rebuilt, build_safe.SOURCE_PATH)
        self.assertEqual(rebuilt, frozen["entries"])
        self.assertEqual(len(rebuilt), 206)
        self.assertTrue(all("killing_test" not in e for e in rebuilt))

    def test_oracle_is_opt_in_and_complete(self):
        frozen = json.loads(Path(build_safe.OUTPUT_PATH).read_text(encoding="utf-8"))["entries"]
        entries, missing = killing_tests.attach(frozen)
        self.assertEqual(missing, [])
        self.assertEqual(sum("killing_test" in e for e in entries), 103)
        self.assertTrue(all("killing_test" not in e for e in entries if e["label"] == "correct"))

    def test_historical_metrics_not_final_claims(self):
        raw = Path("paper/data/upstream/a5r10_refute.jsonl").read_text(encoding="utf-8")
        metrics = compute([json.loads(line) for line in raw.splitlines() if line.strip()])
        self.assertEqual((metrics.total, metrics.tp, metrics.fp), (206, 49, 8))


class AnalysisTests(unittest.TestCase):
    def test_partial_result_is_not_completed_with_abstentions(self):
        from paper.reproduce import validate_result
        output = Path("tests") / ("partial-" + uuid.uuid4().hex)
        output.mkdir()
        try:
            path = output / "lemma4_sonnet46_refute.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            self.assertEqual(validate_result(path, []), "expected 203 original records, found 1")
        finally:
            shutil.rmtree(output)

    def test_offline_tables_and_gaps_preserve_reported_files(self):
        from paper.reproduce import reproduce, sha256
        before = {p: sha256(p) for p in Path("paper/reported").rglob("*") if p.is_file()}
        output = Path("tests") / ("analysis-" + uuid.uuid4().hex)
        try:
            with redirect_stdout(io.StringIO()):
                report = reproduce(output)
            self.assertEqual(report["dataset"]["entries"], 206)
            self.assertEqual(len(report["generated"]), 20)
            self.assertTrue(all(r["matches_reported_numeric_tokens"]
                                for r in report["generated"].values()
                                if r["matches_reported_numeric_tokens"] is not None))
            self.assertTrue(report["full_result_reproduction"])
            self.assertEqual(report["blocked"], {})
            self.assertEqual((report["combined_metrics"]["correct_claims"],
                              report["combined_metrics"]["claims"]), (118, 128))
            self.assertEqual(len(report["run_metrics"]), 12)
            self.assertEqual(report["run_metrics"]["cbmc_theory.jsonl"]["correct_claims"], 65)
            self.assertEqual(report["historical_metrics"]["metrics"]["tp"], 49)
            self.assertEqual(before, {p: sha256(p) for p in before})
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_missing_result_mode_remains_explicit(self):
        from paper.reproduce import reproduce
        output = Path("tests") / ("missing-" + uuid.uuid4().hex)
        try:
            with patch("paper.reproduce.RUN_GENERATORS",
                       {"make_tasks_table": ("missing_for_test.jsonl",)}):
                with redirect_stdout(io.StringIO()):
                    report = reproduce(output)
            self.assertFalse(report["full_result_reproduction"])
            self.assertEqual(report["blocked"]["make_tasks_table"]["missing"],
                             ["nfv/results/missing_for_test.jsonl"])
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_recovered_raw_hashes_match_local_provenance(self):
        from paper.reproduce import sha256
        provenance = json.loads(Path("paper/provenance/local-results.json").read_text(encoding="utf-8"))
        self.assertEqual(len(provenance["source_snapshot"]), 40)
        self.assertEqual(set(provenance), {"source_snapshot", "files"})
        self.assertEqual(len(provenance["files"]), 12)
        for record in provenance["files"]:
            self.assertEqual(sha256(Path(record["destination"])), record["sha256"])


if __name__ == "__main__":
    unittest.main()
