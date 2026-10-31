import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HOOK = Path(__file__).with_name('ssl_runtime_hook.py')


class CertificateHookTests(unittest.TestCase):
    def run_hook(self, bundle, extra_env=None):
        env = dict(os.environ)
        env.pop('SSL_CERT_FILE', None)
        env.pop('SSL_CERT_DIR', None)
        env.update(extra_env or {})
        script = (
            'import os, runpy, ssl, sys; '
            'sys._MEIPASS = sys.argv[1]; runpy.run_path(sys.argv[2]); '
            'print(os.environ.get("SSL_CERT_FILE", "")); '
            'print(len(ssl.create_default_context().get_ca_certs()))'
        )
        return subprocess.run([sys.executable, '-c', script, str(bundle), str(HOOK)],
                              env=env, capture_output=True, text=True, check=True).stdout.splitlines()

    def test_bundled_certificates_are_used_when_no_trust_store_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            cert = bundle / 'certifi' / 'cacert.pem'
            cert.parent.mkdir()
            shutil.copyfile('/etc/ssl/cert.pem', cert)
            output = self.run_hook(bundle)
            self.assertEqual(output[0], str(cert))
            self.assertGreater(int(output[1]), 0)

    def test_explicit_certificate_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_hook(Path(directory), {'SSL_CERT_FILE': '/etc/ssl/cert.pem'})
            self.assertEqual(output[0], '/etc/ssl/cert.pem')
            self.assertGreater(int(output[1]), 0)


if __name__ == '__main__':
    unittest.main()
