#!/usr/bin/env python3
"""Source archives contain a stable allowlist and no local artifacts."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


PROJECT = Path(__file__).resolve().parents[2]
BUILDER = PROJECT / "packaging" / "source" / "build_source.py"
SENTINEL = b"DO-" + b"NOT-" + b"PUBLISH"


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "snapshot"
        shutil.copytree(PROJECT, self.project, ignore=shutil.ignore_patterns(
            ".git", "dist", "build", "sanitized-delivery", "__pycache__", ".DS_Store"))
        for name in ("engine/config.json", "engine/config.json.tmp", ".env", "._README.md", "packaging/macos/secret.key"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(SENTINEL)

    def build(self, output):
        return subprocess.run([sys.executable, str(BUILDER), "--root", str(self.project), "--output", str(output)],
                              capture_output=True, text=True, timeout=30)

    def test_deterministic_allowlisted_files_and_metadata(self):
        first, second = self.base / "one.zip", self.base / "two.zip"
        r1, r2 = self.build(first), self.build(second)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as z:
            names = set(z.namelist())
            for path in ("app.py", "engine/SKILL.md", "app_tests.py", "packaging/windows/setup.iss", "packaging/macos/build.sh", "packaging/source/build_source.py", "LICENSE", "README.md", ".github/workflows/test-installers.yml"):
                self.assertIn("img-switch/" + path, names)
            self.assertTrue(all(name.startswith("img-switch/") and ".." not in Path(name).parts for name in names))
            self.assertTrue(all(not info.extra and not info.comment and info.date_time == (1980, 1, 1, 0, 0, 0) for info in z.infolist()))
            self.assertFalse(any(SENTINEL in z.read(info) for info in z.infolist()))
            self.assertFalse(any(any(piece in name for piece in (".DS_Store", "__pycache__", "config.json.tmp", "sanitized-delivery", "/dist/", "._")) for name in names))

    def test_existing_output_is_never_overwritten(self):
        output = self.base / "archive.zip"
        output.write_bytes(b"do-not-overwrite")
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"do-not-overwrite")

    @unittest.skipIf(os.name == "nt", "symlinks may require Windows privileges")
    def test_rejects_symlinked_allowlisted_file(self):
        readme = self.project / "README.md"
        readme.unlink()
        readme.symlink_to(self.base / "outside")
        (self.base / "outside").write_text("PRIVATE", encoding="utf-8")
        output = self.base / "archive.zip"
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
