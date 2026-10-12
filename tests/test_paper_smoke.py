from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from nfv.backends.dafny import Outcome
from paper import smoke
from paper.redaction import public_metadata


def observed(outcome, exit_code, *, malformed=False):
    return SimpleNamespace(
        outcome=outcome, exit_code=exit_code,
        is_cex=outcome is Outcome.VERIFICATION_FAILED, is_malformed=malformed,
        to_dict=lambda: {"outcome": outcome.value, "exit_code": exit_code,
                         "stdout": "test verifier response", "stderr": ""},
    )


class PaperSmokeTests(unittest.TestCase):
    def test_public_diagnostics_keep_verdicts_without_host_paths(self):
        value = {"outcome": "failed", "exit_code": 10,
                 "stdout": r'C:\Users\example\project\model.c:12: assertion failed',
                 "nested": ["/home/example/project/model.dfy:8: error"]}
        clean = public_metadata(value)
        self.assertEqual(clean["outcome"], "failed")
        self.assertEqual(clean["exit_code"], 10)
        self.assertEqual(clean["stdout"], "<host-path>/model.c:12: assertion failed")
        self.assertEqual(clean["nested"], ["<host-path>/model.dfy:8: error"])

    def test_public_redaction_records_pin_released_metadata(self):
        records = json.loads((smoke.ROOT / "paper/provenance/redactions.json").read_text())
        for record in records["files"]:
            self.assertEqual(hashlib.sha256((smoke.ROOT / record["path"]).read_bytes()).hexdigest(),
                             record["release_sha256"])
            self.assertEqual(len(record["original_sha256"]), 64)

    def test_selection_is_two_fixed_cases_per_backend(self):
        self.assertEqual(len(smoke.CASES), 4)
        self.assertEqual(sum(c.backend == "dafny" for c in smoke.CASES), 2)
        self.assertEqual(sum(c.backend == "cbmc" for c in smoke.CASES), 2)

    def test_manifest_pins_replay_and_excludes_generated_python_caches(self):
        manifest = json.loads((smoke.ROOT / "paper/manifest.json").read_text(encoding="utf-8"))
        evidence = manifest["smoke_replay"]
        self.assertEqual(hashlib.sha256((smoke.ROOT / evidence["report"]).read_bytes()).hexdigest(),
                         evidence["sha256"])
        self.assertFalse(any("__pycache__" in item["destination"]
                             for item in manifest["files"]))

    def test_models_are_exact_strings_from_hash_pinned_original_records(self):
        for case in smoke.CASES:
            row, code, provenance = smoke.load_case(case)
            self.assertEqual(code, row[case.artifact_field])
            self.assertEqual(hashlib.sha256(code.encode("utf-8")).hexdigest(),
                             provenance["artifact_utf8_sha256"])
            raw = (smoke.ROOT / provenance["result_file"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance["result_file_sha256"])
            line = raw.splitlines(keepends=True)[provenance["result_line"] - 1]
            self.assertEqual(hashlib.sha256(line).hexdigest(), provenance["result_line_sha256"])

    def test_dafny_receives_unmodified_final_negation(self):
        case = smoke.CASES[1]
        _, code, _ = smoke.load_case(case)
        with patch("paper.smoke.dafny.run_dafny",
                   return_value=observed(Outcome.VERIFIED, 0)) as verify:
            replay = smoke.replay_case(case, timeout=31)
        verify.assert_called_once_with(code, mode="unbounded", timeout=31)
        self.assertTrue(replay["matched"])
        self.assertEqual(replay["artifact_prediction"], "incorrect")

    def test_raw_hash_mismatch_fails_before_verifier_execution(self):
        with patch("paper.smoke.digest", return_value="wrong-hash"):
            with patch("paper.smoke.dafny.run_dafny") as verify:
                replay = smoke.replay_case(smoke.CASES[0], timeout=30)
        verify.assert_not_called()
        self.assertFalse(replay["matched"])
        self.assertTrue(any("SHA-256 mismatch" in d for d in replay["discrepancies"]))

    def test_cbmc_clean_result_remains_abstention(self):
        case = smoke.CASES[2]
        _, code, _ = smoke.load_case(case)
        with patch("paper.smoke.cbmc.run_cbmc",
                   return_value=observed(Outcome.VERIFIED, 0)) as verify:
            replay = smoke.replay_case(case, timeout=31)
        verify.assert_called_once_with(code, mode="bounded", unroll=10, timeout=31)
        self.assertTrue(replay["matched"])
        self.assertEqual(replay["artifact_prediction"], "abstain")

    def test_malformed_counterexample_does_not_match_original_refutation(self):
        with patch("paper.smoke.cbmc.run_cbmc",
                   return_value=observed(Outcome.VERIFICATION_FAILED, 10, malformed=True)):
            replay = smoke.replay_case(smoke.CASES[3], timeout=30)
        self.assertFalse(replay["matched"])
        self.assertEqual(replay["artifact_prediction"], "abstain")

    def test_proof_failure_and_missing_tool_are_discrepancies(self):
        with patch("paper.smoke.dafny.run_dafny",
                   return_value=observed(Outcome.VERIFICATION_FAILED, 4)):
            replay = smoke.replay_case(smoke.CASES[1], timeout=30)
        self.assertFalse(replay["matched"])
        with patch("paper.smoke.dafny.run_dafny", side_effect=RuntimeError("tool missing")):
            replay = smoke.replay_case(smoke.CASES[0], timeout=30)
        self.assertFalse(replay["matched"])
        self.assertTrue(any("tool missing" in d for d in replay["discrepancies"]))

    def test_cli_returns_failure_and_persists_discrepancy(self):
        output = Path("tests") / ("smoke-" + uuid.uuid4().hex) / "report.json"
        report = {"cases": [{"name": "fixed", "entry_id": "entry", "matched": False,
                             "discrepancies": ["verifier mismatch"]}]}
        try:
            with patch("paper.smoke.run_smoke", return_value=report):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(smoke.main(["--output", str(output)]), 1)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(saved["all_matched"])
            self.assertEqual(saved["cases"][0]["discrepancies"], ["verifier mismatch"])
        finally:
            shutil.rmtree(output.parent, ignore_errors=True)

    def test_cli_cannot_overwrite_raw_or_provenance(self):
        for output in ("nfv/results/lemma4_verify.jsonl", "paper/manifest.json",
                       "paper/reported/paper.tex"):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    smoke.main(["--output", output])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
