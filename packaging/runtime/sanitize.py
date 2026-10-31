"""Remove CPython build paths from copies used to assemble the distribution.

The splitroot documentation example is replaced with equal-length spaces.
On macOS, debug symbols are stripped from extension-module copies and matching
CPython source-directory prefixes in Mach-O string sections are replaced with
equal-length placeholders. Original runtime files and licenses are untouched.
"""
from pathlib import Path
import re
import sys
import zipfile
import hashlib
import shutil
import subprocess

EXAMPLE_LINE = re.compile(
    rb"(?m)^[ \t]*splitroot\('C:/Users/([A-Za-z0-9_-]+)'\) == "
    rb"\('C:', '/', 'Users/\1'\)[ \t]*\n"
)
CPYTHON_SOURCE = re.compile(
    rb'/Users/[^/\x00\s]+/build/v[0-9]+\.[0-9]+\.[0-9]+/'
    rb'(Modules/[A-Za-z0-9_./-]+\.(?:c|h))(?=\x00)'
)

def remove_example(data):
    return EXAMPLE_LINE.subn(lambda match: b' ' * (len(match.group()) - 1) + b'\n', data)

def scrub_cpython_source_paths(path):
    """Remove only CPython __FILE__ prefixes inside Mach-O __cstring sections."""
    from macholib.MachO import MachO
    path = Path(path)
    data = bytearray(path.read_bytes())
    count = 0
    for header in MachO(str(path)).headers:
        for _load, _command, sections in header.commands:
            if not isinstance(sections, list):
                continue
            for section in sections:
                if section.sectname.rstrip(b'\0') != b'__cstring':
                    continue
                start = header.offset + section.offset
                end = start + section.size
                if start < 0 or end > len(data):
                    raise ValueError('Mach-O string section extends outside binary')
                for match in CPYTHON_SOURCE.finditer(data, start, end):
                    left, right = match.start(), match.start(1)
                    data[left:right] = b'.' * (right - left)
                    count += 1
    if count:
        path.write_bytes(data)
        print('Removed CPython source-build prefixes from extension module:', count)
    return count

def scrub_binaries(binaries, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for name, source, kind in binaries:
        filename = Path(name).name.lower()
        if sys.platform == 'darwin' and filename.endswith('.so'):
            subdir = directory / hashlib.sha256(name.encode('utf-8')).hexdigest()
            subdir.mkdir(parents=True, exist_ok=True)
            replacement = subdir / Path(name).name
            shutil.copyfile(source, replacement)
            subprocess.run(['strip', '-S', str(replacement)], check=True, capture_output=True)
            scrub_cpython_source_paths(replacement)
            source = str(replacement)
        elif filename == 'python' or (filename.startswith('libpython') and filename.endswith('.dylib')) or re.fullmatch(r'python3\d+\.dll', filename):
            data = Path(source).read_bytes()
            data, count = remove_example(data)
            if count:
                replacement = directory / Path(name).name
                replacement.write_bytes(data)
                source = str(replacement)
                print('Removed CPython documentation example from copied runtime:', count)
        result.append((name, source, kind))
    return result

def scrub_datas(datas, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for name, source, kind in datas:
        if Path(name).name == 'base_library.zip':
            replacement = directory / 'base_library.zip'
            with zipfile.ZipFile(source) as old, zipfile.ZipFile(replacement, 'w') as new:
                for item in old.infolist():
                    data = old.read(item)
                    data, count = remove_example(data)
                    if count:
                        print('Removed CPython documentation example from bytecode:', count)
                    new.writestr(item, data)
            source = str(replacement)
        result.append((name, source, kind))
    return result

def patch_spec(spec, directory):
    spec = Path(spec)
    text = spec.read_text(encoding='utf-8')
    marker = 'pyz = PYZ(a.pure)'
    if text.count(marker) != 1:
        raise ValueError('Unexpected PyInstaller spec structure')
    injection = ("import runpy\n"
                 "_scrub = runpy.run_path(%r)\n"
                 "a.binaries = _scrub['scrub_binaries'](a.binaries, %r)\n"
                 "a.datas = _scrub['scrub_datas'](a.datas, %r)\n\n" %
                 (str(Path(__file__).resolve()), str(Path(directory).resolve()), str(Path(directory).resolve())))
    spec.write_text(text.replace(marker, injection + marker), encoding='utf-8')

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: sanitize.py spec-file copied-runtime-directory')
    patch_spec(sys.argv[1], sys.argv[2])
