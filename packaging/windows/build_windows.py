#!/usr/bin/env python3
"""Build a Windows test installer on Windows with PyInstaller and Inno Setup 6."""
import argparse
import importlib.metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
GUI_HIDDEN_IMPORTS = ('failovercfg', 'argparse', 'glob', 'uuid', 'base64',
                      'urllib.request', 'urllib.error', 'urllib.parse', 'json',
                      'time', 'socket')
ENGINE_FILES = (
    'engine/SKILL.md',
    'engine/scripts/failovercfg.py',
    'engine/scripts/failover_imagegen_tool.py',
    'engine/scripts/setup.py',
)


def stage_engine(root, destination):
    """Copy only explicit runtime resources, never user configuration or caches."""
    for rel in ENGINE_FILES:
        src, dst = root / rel, destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def audit_executable(executable):
    """Reject incomplete or sensitive-content scans before creating an installer."""
    subprocess.run([sys.executable, str(ROOT / 'packaging/windows/audit_privacy.py'),
                    str(executable)], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('version must be numeric major.minor.patch')
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        parser.error('this installer must be built with 64-bit Python on Windows')
    if importlib.metadata.version('pyinstaller') != '6.16.0':
        parser.error('install pyinstaller==6.16.0 to match the bundled license')
    iscc = shutil.which('iscc') or shutil.which('ISCC.exe')
    if not iscc:
        parser.error('Inno Setup 6 compiler (ISCC.exe) must be in PATH')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    installer = output / ('IMG-Switch-' + args.version + '-Windows-x64-unsigned-setup.exe')
    if installer.exists():
        raise FileExistsError('Refusing to overwrite existing installer: ' + str(installer))
    notices = ROOT / 'packaging/windows/licenses'
    for name in ('TCL.txt', 'TK.txt', 'PYINSTALLER.txt'):
        if not (notices / name).is_file():
            raise RuntimeError('Required third-party license is missing: ' + name)
    with tempfile.TemporaryDirectory(prefix='img-switch-build-') as tmp:
        temp = Path(tmp)
        stage = temp / 'stage'
        stage.mkdir()
        stage_engine(ROOT, stage)
        shutil.copyfile(ROOT / 'app.py', stage / 'app.py')
        dist, work, specs = temp / 'dist', temp / 'work', temp / 'specs'
        specs.mkdir()
        common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                  '--distpath', str(dist), '--workpath', str(work), '--specpath', str(specs)]
        subprocess.run(common + ['--onefile', '--console', '--name', 'img-switch-engine',
                       str(stage / 'engine/scripts/failover_imagegen_tool.py')], check=True, cwd=stage)
        bin_dir = stage / 'engine/bin'
        bin_dir.mkdir()
        shutil.copyfile(dist / 'img-switch-engine.exe', bin_dir / 'img-switch-engine.exe')
        hidden = [part for module in GUI_HIDDEN_IMPORTS for part in ('--hidden-import', module)]
        subprocess.run(common + ['--onefile', '--windowed', '--name', 'IMG-Switch',
                       '--add-data', str(stage / 'engine') + ';engine'] + hidden +
                       ['--paths', str(stage / 'engine/scripts'), str(stage / 'app.py')],
                       check=True, cwd=stage)
        audit_executable(dist / 'IMG-Switch.exe')
        # Python's Windows distribution includes the license terms for Python and
        # its bundled third-party components. Fail closed if they are unavailable.
        python_license = Path(sys.base_prefix) / 'LICENSE.txt'
        if not python_license.is_file():
            raise RuntimeError('Python distribution LICENSE.txt is missing; cannot assemble license notices')
        shutil.copyfile(ROOT / 'LICENSE', stage / 'PROJECT-LICENSE.txt')
        shutil.copyfile(python_license, stage / 'PYTHON-LICENSE.txt')
        subprocess.run([iscc, '/DAppVersion=' + args.version,
                        '/DAppExe=' + str(dist / 'IMG-Switch.exe'),
                        '/DProjectLicense=' + str(stage / 'PROJECT-LICENSE.txt'),
                        '/DPythonLicense=' + str(stage / 'PYTHON-LICENSE.txt'),
                        '/DThirdPartyLicenses=' + str(notices),
                        '/O' + str(output), str(ROOT / 'packaging/windows/setup.iss')], check=True)
        print('Unsigned Windows test installer:', installer)

if __name__ == '__main__':
    main()
