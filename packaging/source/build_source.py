#!/usr/bin/env python3
"""Create a deterministic source-only ZIP from an explicit file allowlist."""
import argparse
from pathlib import Path
import sys
import zipfile


FILES = (
    ".gitignore", "LICENSE", "README.md", "app.py", "app_tests.py",
    "test_packaged_runtime.py", "安装.bat", "卸载.bat", "打包.bat",
    "engine/.gitignore", "engine/README.md", "engine/SKILL.md",
    "engine/config.example.json", "engine/prompts/example.txt",
    "engine/scripts/failovercfg.py", "engine/scripts/failover_imagegen_tool.py",
    "engine/scripts/setup.py", "engine/tests/mock_relay.py", "engine/tests/run_tests.py",
    "engine/tests/test_url_privacy.py",
    "packaging/macos/audit_privacy.py", "packaging/macos/build.sh", "packaging/macos/smoke.py",
    "packaging/macos/stage.py", "packaging/macos/make_dmg.py", "packaging/macos/test_audit_privacy.py",
    "packaging/macos/test_stage.py", "packaging/macos/test_make_dmg.py",
    "packaging/macos/licenses/TCL.txt", "packaging/macos/licenses/TK.txt",
    "packaging/windows/audit_privacy.py", "packaging/windows/build_windows.py",
    "packaging/windows/mock_server.py", "packaging/windows/setup.iss", "packaging/windows/smoke.ps1",
    "packaging/windows/test_audit_privacy.py", "packaging/windows/test_build_windows.py",
    "packaging/windows/licenses/PYINSTALLER.txt", "packaging/windows/licenses/SOURCES.md",
    "packaging/windows/licenses/TCL.txt", "packaging/windows/licenses/TK.txt",
    "packaging/source/build_source.py", "packaging/source/test_build_source.py",
    ".github/workflows/test-installers.yml",
    "docs/superpowers/plans/2026-10-29-open-source-release.md",
    "docs/superpowers/specs/2026-10-29-open-source-release-design.md",
)


def collect(root):
    root = root.resolve(strict=True)
    entries = []
    for name in sorted(FILES):
        parts = Path(name).parts
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("符号链接不能进入源码包: " + name)
        if not current.is_file():
            raise ValueError("源码白名单文件缺失: " + name)
        entries.append((name, current.read_bytes()))
    return entries


def create_archive(root, output):
    entries = collect(root)
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, data in entries:
                    info = zipfile.ZipInfo("img-switch/" + name, (1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    mode = 0o100755 if name.endswith(".sh") else 0o100644
                    info.external_attr = mode << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        create_archive(args.root, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print("源码打包失败: %s" % exc, file=sys.stderr)
        return 1
    print("源码包已创建: %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
