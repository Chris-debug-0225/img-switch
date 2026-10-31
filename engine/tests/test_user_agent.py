"""Exercise model discovery, health check and generation against a strict relay."""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import os
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from mock_relay import make_png
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import failovercfg

ENGINE = Path(__file__).resolve().parents[1] / 'scripts/failover_imagegen_tool.py'


class StrictRelay(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self):
        if not self.headers.get('User-Agent', '').startswith('IMG-Switch/'):
            self.send_response(403)
            self.end_headers()
            return
        if self.command == 'POST':
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            payload = {'data': [{'b64_json': base64.b64encode(make_png(64, 64)).decode()}]}
        else:
            payload = {'data': [{'id': 'test-image'}]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = respond
    do_POST = respond


class RelayUserAgentTests(unittest.TestCase):
    def test_macos_python_uses_system_roots_if_no_default_or_override(self):
        paths = mock.Mock(cafile=None, capath=None)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(failovercfg.sys, 'platform', 'darwin'), \
             mock.patch.object(failovercfg.ssl, 'get_default_verify_paths', return_value=paths), \
             mock.patch.object(failovercfg.os.path, 'isfile', return_value=True):
            failovercfg.configure_default_certificates()
            self.assertEqual(os.environ.get('SSL_CERT_FILE'), '/etc/ssl/cert.pem')

    def test_macos_python_preserves_user_certificate_override(self):
        with mock.patch.dict(os.environ, {'SSL_CERT_FILE': '/custom/ca.pem'}, clear=True), \
             mock.patch.object(failovercfg.sys, 'platform', 'darwin'):
            failovercfg.configure_default_certificates()
            self.assertEqual(os.environ['SSL_CERT_FILE'], '/custom/ca.pem')

    def test_relay_accepts_model_discovery_health_and_generation(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), StrictRelay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = Path(d) / 'config.json'
                cfg.write_text(json.dumps({'version': 1, 'sources': [{
                    'name': 'test', 'base_url': 'http://127.0.0.1:%s/v1' % server.server_port,
                    'api_key': 'test-key', 'model': 'test-image'}]}))
                env = dict(os.environ, FAILOVER_IMAGEGEN_TOOL_CONFIG=str(cfg))
                for args in (['--models'], ['--check'],
                             ['generate', '--prompt', 'test', '--size', '64x64']):
                    with self.subTest(args=args):
                        result = subprocess.run([sys.executable, str(ENGINE)] + args,
                                                cwd=d, env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
