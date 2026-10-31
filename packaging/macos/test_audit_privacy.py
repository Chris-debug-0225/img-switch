import importlib.util
from pathlib import Path
import unittest
import tempfile
import subprocess
import sys

path = Path(__file__).with_name('audit_privacy.py')
spec = importlib.util.spec_from_file_location('audit_privacy', path)
audit = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(audit)
except FileNotFoundError:
    audit = None

class AuditPrivacyTests(unittest.TestCase):
    def test_detects_real_user_paths_and_build_directories(self):
        self.assertIsNotNone(audit, 'privacy scanner missing')
        findings = audit.classify_bytes(b'/Users/actual-user/project /tmp/img-switch-build.abc/source')
        self.assertIn('user_path', findings)
        self.assertIn('build_path', findings)

    def test_all_user_paths_are_flagged(self):
        self.assertIsNotNone(audit, 'privacy scanner missing')
        sample = getattr(audit, 'EXAMPLE', b"/Users/example-user')")
        self.assertIn('user_path', audit.classify_bytes(sample))

    def test_reports_location_and_category_without_path_contents(self):
        scanner = audit.Scanner()
        scanner.bytes(b'/Users/secret-user/private', 'Frameworks/example.bin')
        self.assertEqual(scanner.locations[('Frameworks/example.bin', 'user_path')], 1)
        self.assertNotIn('secret-user', repr(scanner.locations))

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS clang and Mach-O')
    def test_debug_path_classification_and_strip(self):
        with tempfile.TemporaryDirectory(prefix='audit-debug-') as tmp:
            source = Path(tmp) / 'sample.c'
            binary = Path(tmp) / 'sample.so'
            source.write_text('int sample(void) { return 42; }\n', encoding='utf-8')
            subprocess.run(['cc', '-dynamiclib', '-g', '-fdebug-prefix-map=' + tmp + '=/Users/example-user/work',
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            fields = audit.macho_path_fields(binary.read_bytes())
            self.assertTrue(fields)
            self.assertTrue(any('debug' in field or field == 'symbol_strings' for field in fields), fields)
            subprocess.run(['strip', '-S', str(binary)], check=True, capture_output=True)
            self.assertFalse(audit.USER_PATH.search(binary.read_bytes()))

if __name__ == '__main__':
    unittest.main()
