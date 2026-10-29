import importlib.util
from pathlib import Path
import unittest

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

if __name__ == '__main__':
    unittest.main()
