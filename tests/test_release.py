# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from pathlib import Path
import hashlib
import json
import subprocess
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATA_COLLECTION = (
    "Data Collection. The software may collect information about you and your use "
    "of the software and send it to Microsoft. Microsoft may use this information "
    "to provide services and improve our products and services. You may turn off "
    "the telemetry as described in the repository. There are also some features "
    "in the software that may enable you and Microsoft to collect data from users "
    "of your applications. If you use these features, you must comply with "
    "applicable law, including providing appropriate notices to users of your "
    "applications together with a copy of Microsoft's privacy statement. Our "
    "privacy statement is located at https://go.microsoft.com/fwlink/?LinkID=824704. "
    "You can learn more about data collection and use in the help documentation "
    "and our privacy statement. Your use of the software operates as your consent "
    "to these practices."
)


class ReleaseTests(unittest.TestCase):
    def test_named_mit_license_and_compatibility_copy(self):
        license_text = (ROOT / "LICENSE.TXT").read_bytes()
        self.assertEqual((ROOT / "LICENSE").read_bytes(), license_text)
        text = license_text.decode("utf-8")
        self.assertLess(text.index("Neuro-Formal Verification"), text.index("Copyright"))
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) Microsoft Corporation.", text)
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', text)

    def test_required_readme_and_contribution_notices(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for required in (
            "https://opensource.microsoft.com/codeofconduct/",
            "https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general",
            "[SECURITY.md](SECURITY.md)",
            "[PRIVACY](PRIVACY)",
            "[MIT license](LICENSE.TXT)",
        ):
            self.assertIn(required, readme)
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("license/cla", contributing)
        self.assertIn("automatically determines", contributing)
        self.assertIn("Maintainers must not merge", contributing)
        self.assertIn("python -m unittest discover -s tests", contributing)
        self.assertIn("[AGENTS.md](AGENTS.md)", contributing)
        for required in (
            "naming clearance", "authoring-team permission", "legal/IP",
            "Microsoft organization", "not release approval",
        ):
            self.assertIn(required, contributing)

    def test_privacy_notice_and_opt_out_instructions(self):
        privacy = (ROOT / "PRIVACY").read_text(encoding="utf-8")
        self.assertIn(DATA_COLLECTION, " ".join(privacy.split()))
        for required in (
            "GH_AW_DEFAULT_OTLP_ENDPOINT",
            "--disable-client-telemetry",
            "DOTNET_CLI_TELEMETRY_OPTOUT=1",
            "service-side",
            "cancel active/queued runs",
        ):
            self.assertIn(required, privacy)
        onboarding = (ROOT / "workflow" / "ONBOARDING.md").read_text(encoding="utf-8")
        self.assertIn("../PRIVACY", onboarding)

    def test_core_package_includes_release_notices(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["license"], "MIT")
        for name in (
            "LICENSE.TXT", "LICENSE", "NOTICE.txt", "PRIVACY",
            "paper/DATA_LICENSE.md", "paper/DATA_PROVENANCE.md",
        ):
            self.assertIn(name, config["project"]["license-files"])
            self.assertTrue((ROOT / name).is_file())
        workflow_config = tomllib.loads(
            (ROOT / "workflow" / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertIn("licenses/*", workflow_config["project"]["license-files"])

    def test_incorporated_component_notices_are_retained(self):
        notice = (ROOT / "NOTICE.txt").read_text(encoding="utf-8")
        for name in (
            "paper/licenses/microsoft-intent-formalization-LICENSE",
            "paper/licenses/microsoft-intent-formalization-evalplus-LICENSE",
            "paper/licenses/openai-human-eval-LICENSE",
            "workflow/licenses/github-gh-aw-LICENSE",
        ):
            self.assertIn(name, notice)
            self.assertTrue(ROOT.joinpath(*name.split("/")).is_file())

    def test_dataset_license_is_scoped_separately_from_code(self):
        license_text = (ROOT / "paper" / "DATA_LICENSE.md").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith(
            "# Community Data License Agreement - Permissive - Version 2.0\n"
        ))
        self.assertIn("makes available the text of this agreement with the shared Data", license_text)
        for name in ("README.md", "paper/README.md", "paper/DATA_PROVENANCE.md",
                     "TRANSPARENCY.md", "NOTICE.txt"):
            with self.subTest(path=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("CDLA-Permissive-2.0", text)
                self.assertIn("DATA_LICENSE.md", text)
                self.assertIn("MIT", text)
        provenance = (ROOT / "paper" / "DATA_PROVENANCE.md").read_text(encoding="utf-8")
        self.assertIn("not a replacement", provenance)
        self.assertIn("retained experiment results are not relicensed", provenance)

    def test_documentation_uses_review_terminology(self):
        if not (ROOT / ".git").exists():
            self.skipTest("Documentation inventory requires Git metadata")
        paths = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, text=True
        ).split("\0")
        for name in filter(None, paths):
            path = ROOT.joinpath(*name.split("/"))
            if path.suffix.lower() in {".md", ".rst"} or name in {"PRIVACY", "NOTICE.txt"}:
                with self.subTest(path=name):
                    text = path.read_text(encoding="utf-8")
                    self.assertNotRegex(text, r"(?i)audit")
                    self.assertNotRegex(text, r"(?i)(?<!neuro-)formal code review")

    def test_tracked_source_headers_and_no_prebuilt_runtime_binaries(self):
        if not (ROOT / ".git").exists():
            self.skipTest("Tracked-source audit requires Git metadata")
        paths = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, text=True
        ).split("\0")
        manifest = json.loads((ROOT / "paper" / "manifest.json").read_text(encoding="utf-8"))
        frozen_tooling = {
            item["destination"]: item["release_sha256"]
            for item in manifest["files"]
            if item["destination"].startswith("paper/data/")
            and item["destination"].endswith(".py")
        }
        binary_extensions = {
            ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".a", ".lib",
            ".class", ".jar", ".war", ".pyc", ".whl", ".nupkg", ".pdb",
        }
        for name in filter(None, paths):
            path = ROOT.joinpath(*name.split("/"))
            with self.subTest(path=name):
                self.assertNotIn(path.suffix.lower(), binary_extensions)
                is_source = path.suffix in {
                    ".py", ".cjs", ".js", ".ts", ".sh", ".ps1",
                    ".yml", ".yaml", ".toml",
                } or name in {
                    ".github/skills/nfv-pr-review/SKILL.md",
                    ".github/aw/actions-lock.json",
                } or (name.startswith("workflow/src/") and path.suffix == ".txt")
                if is_source:
                    header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:8])
                    sidecar = Path(str(path) + ".license")
                    if sidecar.is_file():
                        header += "\n" + sidecar.read_text(encoding="utf-8")
                    if name in frozen_tooling:
                        self.assertEqual(
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                            frozen_tooling[name],
                        )
                    self.assertIn("Copyright (c) Microsoft Corporation.", header)
                    self.assertIn("Licensed under the MIT License.", header)


if __name__ == "__main__":
    unittest.main()
