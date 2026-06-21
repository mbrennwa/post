# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from licensing_support import (
    REPO_ROOT,
    glob_paths,
    load_json,
    parse_derived_files,
    read_pyproject_license_files,
)

HEADER_SCAN_LINES = 20


class CopyrightHeaderTests(unittest.TestCase):
    def test_python_files_have_spdx_headers(self) -> None:
        paths_config = load_json("licensing/paths.json")
        expected_spdx = paths_config["expected_spdx"]
        spdx_key = "-".join(("SPDX", "License", "Identifier")) + ":"

        missing: list[str] = []
        for pattern in paths_config["require_spdx"]:
            for path in glob_paths(pattern):
                text = path.read_text(encoding="utf-8")
                rel = path.relative_to(REPO_ROOT)
                if "Copyright" not in text:
                    missing.append(f"{rel}: missing Copyright")
                spdx_lines = [
                    line.strip()
                    for line in text.splitlines()
                    if spdx_key in line
                ]
                if not spdx_lines:
                    missing.append(f"{rel}: missing {spdx_key}")
                elif not any(expected_spdx in line for line in spdx_lines):
                    missing.append(
                        f"{rel}: {spdx_key} must include {expected_spdx!r}"
                    )

        self.assertEqual(missing, [])


class ThirdPartyAttributionTests(unittest.TestCase):
    def test_third_party_entries(self) -> None:
        manifest = load_json("licensing/third_party.json")
        failures: list[str] = []

        for entry in manifest["entries"]:
            license_path = REPO_ROOT / entry["license_file"]
            if not license_path.is_file():
                failures.append(f"{entry['id']}: missing {entry['license_file']}")
                continue

            for rel_path in entry["files"]:
                source_path = REPO_ROOT / rel_path
                if not source_path.is_file():
                    failures.append(f"{entry['id']}: missing source file {rel_path}")
                    continue

                header = "\n".join(
                    source_path.read_text(encoding="utf-8").splitlines()[:HEADER_SCAN_LINES]
                )
                for needle in entry["header_must_contain"]:
                    if needle not in header:
                        failures.append(
                            f"{entry['id']}: {rel_path} header missing {needle!r}"
                        )

        self.assertEqual(failures, [])


class ThirdPartyManifestConsistencyTests(unittest.TestCase):
    def test_manifest_matches_license_files(self) -> None:
        manifest = load_json("licensing/third_party.json")
        by_license_file = {
            entry["license_file"]: sorted(entry["files"])
            for entry in manifest["entries"]
        }

        failures: list[str] = []
        for license_file, manifest_files in sorted(by_license_file.items()):
            license_path = REPO_ROOT / license_file
            derived_files = sorted(parse_derived_files(license_path))
            if derived_files != manifest_files:
                failures.append(
                    f"{license_file}: manifest {manifest_files!r} != "
                    f"license file {derived_files!r}"
                )

        licenses_dir = REPO_ROOT / "LICENSES"
        for license_path in sorted(licenses_dir.glob("*.txt")):
            rel = str(license_path.relative_to(REPO_ROOT))
            derived_files = parse_derived_files(license_path)
            if not derived_files:
                continue
            if rel not in by_license_file:
                failures.append(f"{rel}: has derived files but no manifest entry")

        self.assertEqual(failures, [])


class LicenseFilesTests(unittest.TestCase):
    def test_root_license_exists(self) -> None:
        self.assertTrue((REPO_ROOT / "LICENSE").is_file())

    def test_license_files_listed_in_pyproject(self) -> None:
        declared = set(read_pyproject_license_files())
        failures: list[str] = []

        if "LICENSE" not in declared:
            failures.append("pyproject.toml license-files missing LICENSE")

        licenses_dir = REPO_ROOT / "LICENSES"
        for license_path in sorted(licenses_dir.glob("*.txt")):
            rel = str(license_path.relative_to(REPO_ROOT))
            if rel not in declared:
                failures.append(f"pyproject.toml license-files missing {rel}")

        self.assertEqual(failures, [])


class ReuseLintTests(unittest.TestCase):
    def test_reuse_lint(self) -> None:
        if shutil.which("reuse") is None:
            self.skipTest("reuse not installed")

        result = subprocess.run(
            ["reuse", "lint"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
