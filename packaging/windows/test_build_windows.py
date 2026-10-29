import importlib.util
import tempfile
import unittest
import subprocess
import json
import sys
import time
import urllib.request
from pathlib import Path

PATH = Path(__file__).with_name('build_windows.py')

class BuildTest(unittest.TestCase):
    def test_local_mock_server_returns_image(self):
        script = PATH.with_name('mock_server.py')
        self.assertTrue(script.is_file(), 'local mock server missing')
        with tempfile.TemporaryDirectory() as tmp:
            port_file = Path(tmp) / 'port'
            process = subprocess.Popen([sys.executable, '-B', str(script), str(port_file)])
            try:
                deadline = time.monotonic() + 10
                while not port_file.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(port_file.exists(), 'mock server failed to start')
                request = urllib.request.Request('http://127.0.0.1:' + port_file.read_text() + '/v1/images/generations',
                    data=json.dumps({'prompt': 'test', 'model': 'mock', 'size': '64x64'}).encode(),
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer dummy'}, method='POST')
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertTrue(json.load(response)['data'][0]['b64_json'])
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_audit_rejects_invalid_executable(self):
        spec = importlib.util.spec_from_file_location('windows_build', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'audit_executable'), 'build privacy audit is missing')
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / 'invalid.exe'
            invalid.write_bytes(b'invalid executable')
            with self.assertRaises(subprocess.CalledProcessError):
                module.audit_executable(invalid)

    def test_gui_includes_dynamic_engine_imports(self):
        spec = importlib.util.spec_from_file_location('windows_build', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue({'failovercfg', 'argparse', 'glob', 'uuid', 'base64',
                         'urllib.request', 'urllib.error', 'urllib.parse'} <= set(getattr(module, 'GUI_HIDDEN_IMPORTS', ())))

    def test_stage_includes_only_explicit_runtime_files(self):
        self.assertTrue(PATH.is_file(), 'Windows build script is missing')
        spec = importlib.util.spec_from_file_location('windows_build', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root, stage = Path(tmp) / 'source', Path(tmp) / 'stage'
            for rel in module.ENGINE_FILES:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(rel, encoding='utf-8')
            (root / 'engine/config.json').write_text('PRIVATE', encoding='utf-8')
            (root / 'engine/config.json.tmp').write_text('PRIVATE', encoding='utf-8')
            module.stage_engine(root, stage)
            self.assertEqual({str(p.relative_to(stage)).replace('\\', '/') for p in stage.rglob('*') if p.is_file()}, set(module.ENGINE_FILES))

if __name__ == '__main__':
    unittest.main()
