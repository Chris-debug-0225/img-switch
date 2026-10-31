"""Remove a nonessential CPython documentation example from copied runtimes.

Only the splitroot example line is replaced, with equal-length spaces. The
original interpreter and its licenses are never modified.
"""
from pathlib import Path
import re
import sys
import zipfile

EXAMPLE_LINE = re.compile(
    rb"(?m)^[ \t]*splitroot\('C:/Users/([A-Za-z0-9_-]+)'\) == "
    rb"\('C:', '/', 'Users/\1'\)[ \t]*\n"
)

def remove_example(data):
    return EXAMPLE_LINE.subn(lambda match: b' ' * (len(match.group()) - 1) + b'\n', data)

def scrub_binaries(binaries, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for name, source, kind in binaries:
        filename = Path(name).name.lower()
        if (filename.startswith('libpython') and filename.endswith('.dylib')) or re.fullmatch(r'python3\d+\.dll', filename):
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
