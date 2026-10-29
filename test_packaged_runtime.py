#!/usr/bin/env python3
"""Installed skills retain a self-contained CLI and survive failed upgrades."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import app


class PackagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="img switch 测试 ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home with space"
        (self.home / ".codex").mkdir(parents=True)
        self.engine = self.root / "bundle" / "engine"
        self.engine.mkdir(parents=True)
        shutil.copyfile(Path(app.__file__).parent / "engine" / "SKILL.md", self.engine / "SKILL.md")
        binary = self.engine / "bin" / ("img-switch-engine.exe" if os.name == "nt" else "img-switch-engine")
        binary.parent.mkdir()
        binary.write_bytes(b"bundled-cli" if os.name == "nt" else b"#!/bin/sh\nprintf '%s\\n' \"$1\"\nexit 7\n")
        binary.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "FAILOVER_IMAGEGEN_APP_HOME": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.engine_patch = mock.patch.object(app, "engine_dir", return_value=str(self.engine))
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)

    def test_bundled_engine_installs_without_python_reference(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        name = "img-switch-engine.exe" if os.name == "nt" else "img-switch-engine"
        self.assertEqual((skill / "bin" / name).read_bytes(), (self.engine / "bin" / name).read_bytes())
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('"<skill_dir>/bin/' + name + '" --list-sources', text)
        self.assertNotIn("python <skill_dir>", text)
        shutil.rmtree(self.engine.parent)
        self.assertTrue((skill / "bin" / name).is_file())
        if os.name != "nt":
            result = subprocess.run([str(skill / "bin" / name), "--list-sources"],
                                    env={"PATH": ""}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout, "--list-sources\n")

    def test_failed_upgrade_preserves_installed_skill(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        original = (skill / "SKILL.md").read_bytes()
        with mock.patch.object(app.shutil, "copytree", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                app.install_skill([".codex"])
        self.assertEqual((skill / "SKILL.md").read_bytes(), original)

    def test_failed_rollback_keeps_backup(self):
        app.install_skill([".codex"])
        root = self.home / ".codex" / "skills"
        original = (root / app.SKILL_NAME / "SKILL.md").read_bytes()
        real_replace = os.replace
        calls = []
        def fail_after_backup(source, dest):
            calls.append((source, dest))
            if len(calls) > 1:
                raise OSError("rename failed")
            real_replace(source, dest)
        with mock.patch.object(app.os, "replace", side_effect=fail_after_backup):
            with self.assertRaises(Exception):
                app.install_skill([".codex"])
        backups = list(root.glob('.img-switch-stage-*/old/SKILL.md'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_same_named_unmanaged_skill_is_not_replaced_or_removed(self):
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: failover-imagegen-tool\n---\ncustom data\n", encoding="utf-8")
        (skill / "config.json").write_text('{"private": true}', encoding="utf-8")
        self.assertEqual(app.install_skill([".codex"]), [])
        self.assertEqual(app.remove_skill([".codex"]), [])
        self.assertIn("custom data", (skill / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse(Path(app.appdata_config()).exists())

    def test_windows_skill_explains_powershell_call_operator(self):
        (self.engine / "bin" / "img-switch-engine.exe").write_bytes(b"cli")
        with mock.patch.object(app.os, "name", "nt"):
            app._prepare_packaged_skill(str(self.engine))
        text = (self.engine / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('PowerShell', text)
        self.assertIn('& "<skill_dir>/bin/img-switch-engine.exe"', text)

    def test_upgrade_preserves_legacy_configs_without_overwriting_unified_config(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        (skill / "config.json").write_bytes(b"legacy-config")
        (skill / "config.json.tmp").write_bytes(b"legacy-temp")
        config = Path(app.appdata_config())
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(b"existing-unified")
        backup_dir = config.parent / "migration-backups"
        backup_dir.mkdir()
        (backup_dir / "existing.backup").write_bytes(b"existing-backup")
        (self.engine / "config.json").write_bytes(b"bundle-secret")
        (self.engine / "config.json.tmp").write_bytes(b"bundle-temp-secret")
        app.install_skill([".codex"])
        self.assertEqual(config.read_bytes(), b"existing-unified")
        self.assertFalse((skill / "config.json").exists())
        self.assertFalse((skill / "config.json.tmp").exists())
        self.assertTrue((config.parent / "migration-backups").is_dir(), "legacy backup directory missing")
        backups = list((config.parent / "migration-backups").iterdir())
        self.assertEqual(sorted(p.read_bytes() for p in backups), [b"existing-backup", b"legacy-config", b"legacy-temp"])
        if os.name != "nt":
            self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in backups if p.name != "existing.backup"))

    def test_remove_preserves_legacy_configs(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        (skill / "config.json").write_bytes(b"legacy-config")
        (skill / "config.json.tmp").write_bytes(b"legacy-temp")
        config = Path(app.appdata_config())
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(b"existing-unified")
        app.remove_skill([".codex"])
        self.assertFalse(skill.exists())
        self.assertEqual(config.read_bytes(), b"existing-unified")
        self.assertTrue((config.parent / "migration-backups").is_dir(), "legacy backup directory missing")
        backups = list((config.parent / "migration-backups").iterdir())
        self.assertEqual(sorted(p.read_bytes() for p in backups), [b"legacy-config", b"legacy-temp"])

    def test_backup_failure_prevents_removing_old_skill(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        (skill / "config.json").write_bytes(b"legacy-config")
        config = Path(app.appdata_config())
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(b"existing-unified")
        with mock.patch.object(app.tempfile, "mkstemp", side_effect=OSError("backup failed")):
            with self.assertRaises(OSError):
                app.remove_skill([".codex"])
        self.assertEqual((skill / "config.json").read_bytes(), b"legacy-config")

    def test_migration_creates_private_unified_config_and_backups(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        (skill / "config.json").write_bytes(b"legacy-config")
        (skill / "config.json.tmp").write_bytes(b"legacy-temp")
        app.install_skill([".codex"])
        config = Path(app.appdata_config())
        self.assertEqual(config.read_bytes(), b"legacy-config")
        backups = list((config.parent / "migration-backups").iterdir())
        self.assertEqual(sorted(p.read_bytes() for p in backups), [b"legacy-config", b"legacy-temp"])
        if os.name != "nt":
            self.assertEqual(config.stat().st_mode & 0o077, 0)

    def test_backup_failure_prevents_upgrade(self):
        app.install_skill([".codex"])
        skill = self.home / ".codex" / "skills" / app.SKILL_NAME
        (skill / "config.json.tmp").write_bytes(b"legacy-temp")
        with mock.patch.object(app.tempfile, "mkstemp", side_effect=OSError("backup failed")):
            with self.assertRaises(OSError):
                app.install_skill([".codex"])
        self.assertEqual((skill / "config.json.tmp").read_bytes(), b"legacy-temp")


if __name__ == "__main__":
    unittest.main()
