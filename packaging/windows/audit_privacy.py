#!/usr/bin/env python3
"""Read a Windows PyInstaller GUI exe and nested CLI in memory; never print matches.

Use the same Python version as the build (marshal is version-specific). This checks
path/identity patterns, not arbitrary secrets or third-party license compliance.
"""
import collections
import getpass
import io
import marshal
import os
from pathlib import Path
import re
import socket
import struct
import sys
import types
import zipfile
import zlib

USER_PATH = re.compile(rb'(?i)(?:/users/[^/\s\x00]+|/home/[^/\s\x00]+|[a-z]:\\users\\[^\\\s\x00]+)')
BUILD_PATH = re.compile(rb'(?i)(?:[a-z]:\\[^\x00\r\n]{0,200}img-switch-build-[a-z0-9]+|(?:/private)?/tmp/img-switch-build[.-][a-z0-9]+)')
COOKIE_MAGIC = b'MEI\014\013\012\013\016'
COOKIE = struct.Struct('!8sIIII64s')
ENTRY = struct.Struct('!IIIIBc')


def classify_bytes(data):
    result = set()
    if USER_PATH.search(data): result.add('user_path')
    if BUILD_PATH.search(data): result.add('build_path')
    return result


class Scanner:
    def __init__(self):
        self.counts = collections.Counter()
        self.objects = 0
        self.entries = 0
        self.hostname = socket.gethostname().encode()
        self.username = getpass.getuser().encode()
        self.source = str(Path(__file__).resolve().parents[2]).encode()

    def bytes(self, data):
        self.counts.update(classify_bytes(data))
        if len(self.hostname) >= 6 and self.hostname.lower() in data.lower(): self.counts['hostname'] += 1
        # Generic CI account names occur in ordinary library text. Paths are
        # still checked above; do not flag such words wherever they occur.
        generic = (b'runner', b'runneradmin', b'root', b'user', b'administrator')
        if len(self.username) >= 4 and self.username.lower() not in generic:
            username = re.escape(self.username.lower())
            if re.search(rb'(?<![a-z0-9_])' + username + rb'(?![a-z0-9_])', data.lower()):
                self.counts['username'] += 1
        if len(self.source) >= 6 and self.source.lower() in data.lower(): self.counts['source_path'] += 1

    def code(self, obj):
        self.objects += 1
        self.bytes(obj.co_filename.encode('utf-8', errors='replace'))
        if os.path.isabs(obj.co_filename) or re.match(r'^[A-Za-z]:[\\/]', obj.co_filename):
            self.counts['absolute_code_path'] += 1
        for value in obj.co_consts:
            if isinstance(value, types.CodeType): self.code(value)
            elif isinstance(value, str): self.bytes(value.encode('utf-8', errors='replace'))
            elif isinstance(value, bytes): self.bytes(value)

    def pyz(self, data):
        if data[:4] != b'PYZ\0' or len(data) < 12: raise ValueError('invalid PYZ')
        offset = struct.unpack('!i', data[8:12])[0]
        if offset < 12 or offset >= len(data): raise ValueError('invalid PYZ table')
        table = dict(marshal.loads(data[offset:]))
        for kind, start, size in table.values():
            if kind == 3: continue
            if start < 12 or size < 0 or start + size > offset: raise ValueError('invalid PYZ entry')
            payload = zlib.decompress(data[start:start + size])
            self.bytes(payload)
            if kind in (0, 1): self.code(marshal.loads(payload))

    def zip(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                payload = archive.read(name)
                self.bytes(payload)
                if name.endswith('.pyc'): self.code(marshal.loads(payload[16:]))

    def archive(self, data, is_gui):
        self.bytes(data)
        pos = data.rfind(COOKIE_MAGIC)
        if pos < 0 or pos + COOKIE.size > len(data): raise ValueError('missing archive cookie')
        _, length, toc_off, toc_len, version, _ = COOKIE.unpack_from(data, pos)
        if version != sys.version_info.major * 100 + sys.version_info.minor:
            raise ValueError('build and scanner Python versions differ')
        begin = pos + COOKIE.size - length
        if begin < 0 or begin + toc_off + toc_len > pos: raise ValueError('invalid archive bounds')
        toc = data[begin + toc_off:begin + toc_off + toc_len]
        cursor, cli_count, pyz_count = 0, 0, 0
        while cursor < len(toc):
            if cursor + ENTRY.size > len(toc): raise ValueError('invalid archive table')
            rec_len, start, size, unpacked, compressed, kind = ENTRY.unpack_from(toc, cursor)
            if rec_len < ENTRY.size or cursor + rec_len > len(toc): raise ValueError('invalid archive entry')
            name = toc[cursor + ENTRY.size:cursor + rec_len].rstrip(b'\0').decode('utf-8')
            cursor += rec_len
            if kind == b'o': continue
            if start + size > toc_off: raise ValueError('invalid archive data bounds')
            payload = data[begin + start:begin + start + size]
            if compressed: payload = zlib.decompress(payload)
            if len(payload) != unpacked: raise ValueError('invalid archive length')
            self.entries += 1
            self.bytes(payload)
            norm = name.replace('\\', '/').lower()
            if norm.endswith('/img-switch-engine.exe'):
                if not is_gui: raise ValueError('unexpected nested engine')
                cli_count += 1
                self.archive(payload, False)
            elif kind == b'z':
                pyz_count += 1
                self.pyz(payload)
            elif norm.endswith('.zip'):
                self.zip(payload)
            elif kind in (b's', b'm', b'M'):
                self.code(marshal.loads(payload))
        if pyz_count != 1 or (is_gui and cli_count != 1):
            raise ValueError('required embedded archive is missing or ambiguous')


def main():
    if len(sys.argv) != 2: raise SystemExit('Usage: python audit_privacy.py path/to/IMG-Switch.exe')
    scanner = Scanner()
    try:
        scanner.archive(Path(sys.argv[1]).read_bytes(), True)
    except Exception as error:
        print('Scan incomplete:', type(error).__name__)
        return 2
    print('Archive entries:', scanner.entries, '; Python code objects:', scanner.objects)
    for label, count in sorted(scanner.counts.items()): print(label + ':', count)
    unexpected = sum(scanner.counts.values())
    print('Result:', 'REVIEW REQUIRED' if unexpected else 'No unexpected identity or path matches')
    return int(bool(unexpected))

if __name__ == '__main__':
    raise SystemExit(main())
