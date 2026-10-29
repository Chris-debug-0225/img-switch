import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('privacy', Path(__file__).with_name('audit_privacy.py'))
audit = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(audit)
except FileNotFoundError:
    audit = None

class PrivacyTests(unittest.TestCase):
    def test_ci_runner_word_is_not_treated_as_identity(self):
        scanner = audit.Scanner()
        scanner.username = b'runner'
        scanner.hostname = b''
        scanner.source = b''
        scanner.bytes(b'run this runner command')
        self.assertEqual(scanner.counts['username'], 0)

    def test_sensitive_paths_and_user_path_are_flagged(self):
        self.assertIsNotNone(audit, 'privacy scanner missing')
        sample = getattr(audit, 'EXAMPLE', b"/Users/example-user')")
        self.assertIn('user_path', audit.classify_bytes(sample))
        results = audit.classify_bytes(b'C:\\Users\\developer\\project C:\\Temp\\img-switch-build-xyz')
        self.assertTrue({'user_path', 'build_path'} <= results)

    def test_recursive_code_filenames(self):
        self.assertIsNotNone(audit, 'privacy scanner missing')
        scanner = audit.Scanner()
        scanner.code(compile('def f(): return 1', 'C:\\Users\\test-user\\src\\app.py', 'exec'))
        self.assertGreater(scanner.counts['user_path'], 0)
        self.assertGreater(scanner.counts['absolute_code_path'], 0)

    def test_pyz_scans_marshaled_code(self):
        self.assertIsNotNone(audit, 'privacy scanner missing')
        import marshal, struct, zlib
        code = compile('x = 1', 'C:\\build\\user\\module.py', 'exec')
        payload = zlib.compress(marshal.dumps(code))
        toc = marshal.dumps({'sample': (0, 12, len(payload))})
        data = b'PYZ\0' + b'0000' + struct.pack('!i', 12 + len(payload)) + payload + toc
        scanner = audit.Scanner()
        scanner.pyz(data)
        self.assertGreater(scanner.counts['absolute_code_path'], 0)

    def test_nested_engine_archive_is_inspected(self):
        self.assertIsNotNone(audit, 'privacy scanner missing')
        import marshal, struct, sys, zlib

        def pyz(path):
            body = zlib.compress(marshal.dumps(compile('x = 1', path, 'exec')))
            return b'PYZ\0' + b'0000' + struct.pack('!i', 12 + len(body)) + body + marshal.dumps({'module': (0, 12, len(body))})

        def carchive(entries):
            data, table = bytearray(), bytearray()
            for name, kind, raw in entries:
                encoded_name = name.encode('utf-8') + b'\0'
                table += audit.ENTRY.pack(audit.ENTRY.size + len(encoded_name), len(data), len(raw), len(raw), 0, kind) + encoded_name
                data += raw
            length = len(data) + len(table) + audit.COOKIE.size
            return bytes(data + table) + audit.COOKIE.pack(audit.COOKIE_MAGIC, length, len(data), len(table), sys.version_info.major * 100 + sys.version_info.minor, b'python')

        cli = carchive([('PYZ.pyz', b'z', pyz('C:\\Users\\test-user\\source.py'))])
        gui = carchive([('PYZ.pyz', b'z', pyz('module.py')), ('engine/bin/img-switch-engine.exe', b'b', cli)])
        scanner = audit.Scanner()
        scanner.archive(gui, True)
        self.assertGreater(scanner.counts['user_path'], 0)
        self.assertGreater(scanner.counts['absolute_code_path'], 0)

if __name__ == '__main__':
    unittest.main()
