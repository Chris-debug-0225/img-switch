#!/usr/bin/env python3
"""Inspect an extracted macOS app, including PyInstaller archives, without printing matches.

Run with the same Python environment used to build the app (PyInstaller is required).
Nonzero means a path/hostname was found or the scan was incomplete.
"""
import collections
import io
import marshal
import os
from pathlib import Path
import re
import socket
import sys
import types
import zipfile
import tempfile

USER_PATH = re.compile(rb'(?i)(?:/users/[^/\s\x00]+|/home/[^/\s\x00]+|[a-z]:\\users\\[^\\\s\x00]+)')
BUILD_PATH = re.compile(rb'(?i)(?:/private)?/tmp/img-switch-build\.[a-z0-9]+')


def macho_path_fields(data):
    """Classify matches by Mach-O region without printing or returning their values."""
    offsets = [m.start() for m in USER_PATH.finditer(data)]
    if not offsets:
        return {}
    from macholib.MachO import MachO
    regions = []
    try:
        with tempfile.NamedTemporaryFile(prefix='img-switch-macho-') as f:
            f.write(data)
            f.flush()
            for header in MachO(f.name).headers:
                start = header.offset + header.header._size_
                regions.append((start, start + header.header.sizeofcmds, 'load_commands'))
                for load, command, sections in header.commands:
                    if load.get_cmd_name() == 'LC_SYMTAB':
                        regions.append((header.offset + command.stroff,
                                        header.offset + command.stroff + command.strsize, 'symbol_strings'))
                    if isinstance(sections, list):
                        for section in sections:
                            name = section.sectname.rstrip(b'\0').decode('ascii', errors='replace')
                            name = re.sub(r'[^A-Za-z0-9_]', '_', name)[:32]
                            start = header.offset + section.offset
                            regions.append((start, start + section.size, 'section:' + name))
    except (OSError, ValueError, TypeError, ImportError):
        return {'unclassified': len(offsets)}
    fields = collections.Counter()
    for offset in offsets:
        matches = [label for start, end, label in regions if start <= offset < end]
        fields[matches[0] if matches else 'unclassified'] += 1
    return dict(fields)


def classify_bytes(data):
    findings = set()
    if USER_PATH.search(data):
        findings.add('user_path')
    if BUILD_PATH.search(data):
        findings.add('build_path')
    return findings


class Scanner:
    def __init__(self):
        self.counts = collections.Counter()
        self.locations = collections.Counter()
        self.files = 0
        self.codes = 0
        self.host = socket.gethostname().encode()
        self.source = str(Path(__file__).resolve().parents[2]).encode()
        self.macho_diagnostics = os.environ.get('IMG_SWITCH_MACHO_DIAGNOSTICS') == '1'

    def bytes(self, data, location='<unknown>'):
        findings = set(classify_bytes(data))
        if len(self.host) >= 6 and self.host in data:
            findings.add('hostname')
        if len(self.source) >= 6 and self.source in data:
            findings.add('source_path')
        self.counts.update(findings)
        for label in findings:
            self.locations[(location, label)] += 1
        if 'user_path' in findings and self.macho_diagnostics:
            for field, count in macho_path_fields(data).items():
                self.locations[(location, 'macho_' + field)] += count

    def code(self, obj, location='<unknown>'):
        self.codes += 1
        self.bytes(obj.co_filename.encode('utf-8', errors='replace'), location)
        if os.path.isabs(obj.co_filename) or re.match(r'^[A-Za-z]:[\\/]', obj.co_filename):
            self.counts['absolute_code_path'] += 1
            self.locations[(location, 'absolute_code_path')] += 1
        for value in obj.co_consts:
            if isinstance(value, types.CodeType):
                self.code(value, location)
            elif isinstance(value, str):
                self.bytes(value.encode('utf-8', errors='replace'), location)
            elif isinstance(value, bytes):
                self.bytes(value, location)

    def zip(self, data, location):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                payload = archive.read(name)
                entry = location + ' :: ' + name
                self.bytes(payload, entry)
                if name.endswith('.pyc'):
                    self.code(marshal.loads(payload[16:]), entry)

    def pyinstaller(self, file, location):
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(file))
        for name in archive.toc:
            if name == 'PYZ.pyz':
                nested = archive.open_embedded_archive(name)
                for item in nested.toc:
                    payload = nested.extract(item)
                    if isinstance(payload, types.CodeType):
                        self.code(payload, location + ' :: PYZ :: ' + item)
                    elif isinstance(payload, bytes):
                        self.bytes(payload, location + ' :: PYZ :: ' + item)
            elif name.endswith('.zip'):
                self.zip(archive.extract(name), location + ' :: ' + name)
            elif name in ('app', 'failover_imagegen_tool', 'pyiboot01_bootstrap', 'pyi_rth_inspect', 'pyi_rth__tkinter'):
                self.code(marshal.loads(archive.extract(name)), location + ' :: ' + name)
            else:
                self.bytes(archive.extract(name), location + ' :: ' + name)

    def app(self, root):
        contents = root / 'Contents'
        if not contents.is_dir():
            raise ValueError('Not an app bundle')
        for file in contents.rglob('*'):
            if file.is_file() and not file.is_symlink():
                self.files += 1
                data = file.read_bytes()
                location = file.relative_to(contents).as_posix()
                self.bytes(data, location)
                if file.name == 'base_library.zip':
                    self.zip(data, location)
        self.pyinstaller(contents / 'MacOS/IMG Switch', 'MacOS/IMG Switch')
        self.pyinstaller(contents / 'Frameworks/engine/bin/img-switch-engine', 'Frameworks/engine/bin/img-switch-engine')


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python audit_privacy.py "/path/to/IMG Switch.app"')
    scanner = Scanner()
    try:
        scanner.app(Path(sys.argv[1]))
    except Exception as error:
        print('Scan incomplete:', type(error).__name__)
        return 2
    print('Files scanned:', scanner.files, '; Python code objects:', scanner.codes)
    for label, count in sorted(scanner.counts.items()):
        print(label + ':', count)
    for (location, label), count in sorted(scanner.locations.items()):
        # Report package-relative names only, never the matched contents.
        label_bytes = location.encode('utf-8', errors='replace')
        if (USER_PATH.search(label_bytes) or os.path.isabs(location) or
                (len(scanner.host) >= 6 and scanner.host.lower() in label_bytes.lower())):
            location = '<redacted-location>'
        print('  %s: %s (%d)' % (location, label, count))
    bad = sum(scanner.counts.values())
    print('Result:', 'REVIEW REQUIRED' if bad else 'No unexpected path/hostname matches')
    return int(bool(bad))

if __name__ == '__main__':
    raise SystemExit(main())
