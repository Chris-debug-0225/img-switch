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

USER_PATH = re.compile(rb'(?i)(?:/users/[^/\s\x00]+|/home/[^/\s\x00]+|[a-z]:\\users\\[^\\\s\x00]+)')
BUILD_PATH = re.compile(rb'(?i)(?:/private)?/tmp/img-switch-build\.[a-z0-9]+')


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
        self.files = 0
        self.codes = 0
        self.host = socket.gethostname().encode()
        self.source = str(Path(__file__).resolve().parents[2]).encode()

    def bytes(self, data):
        self.counts.update(classify_bytes(data))
        if len(self.host) >= 6 and self.host in data:
            self.counts['hostname'] += 1
        if len(self.source) >= 6 and self.source in data:
            self.counts['source_path'] += 1

    def code(self, obj):
        self.codes += 1
        self.bytes(obj.co_filename.encode('utf-8', errors='replace'))
        if os.path.isabs(obj.co_filename) or re.match(r'^[A-Za-z]:[\\/]', obj.co_filename):
            self.counts['absolute_code_path'] += 1
        for value in obj.co_consts:
            if isinstance(value, types.CodeType):
                self.code(value)
            elif isinstance(value, str):
                self.bytes(value.encode('utf-8', errors='replace'))
            elif isinstance(value, bytes):
                self.bytes(value)

    def zip(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                payload = archive.read(name)
                self.bytes(payload)
                if name.endswith('.pyc'):
                    self.code(marshal.loads(payload[16:]))

    def pyinstaller(self, file):
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(file))
        for name in archive.toc:
            if name == 'PYZ.pyz':
                nested = archive.open_embedded_archive(name)
                for item in nested.toc:
                    payload = nested.extract(item)
                    if isinstance(payload, types.CodeType):
                        self.code(payload)
                    elif isinstance(payload, bytes):
                        self.bytes(payload)
            elif name.endswith('.zip'):
                self.zip(archive.extract(name))
            elif name in ('app', 'failover_imagegen_tool', 'pyiboot01_bootstrap', 'pyi_rth_inspect', 'pyi_rth__tkinter'):
                self.code(marshal.loads(archive.extract(name)))
            else:
                self.bytes(archive.extract(name))

    def app(self, root):
        contents = root / 'Contents'
        if not contents.is_dir():
            raise ValueError('Not an app bundle')
        for file in contents.rglob('*'):
            if file.is_file() and not file.is_symlink():
                self.files += 1
                data = file.read_bytes()
                self.bytes(data)
                if file.name == 'base_library.zip':
                    self.zip(data)
        self.pyinstaller(contents / 'MacOS/IMG Switch')
        self.pyinstaller(contents / 'Frameworks/engine/bin/img-switch-engine')


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
    bad = sum(scanner.counts.values())
    print('Result:', 'REVIEW REQUIRED' if bad else 'No unexpected path/hostname matches')
    return int(bool(bad))

if __name__ == '__main__':
    raise SystemExit(main())
