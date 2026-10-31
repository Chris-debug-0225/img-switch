#!/usr/bin/env python3
"""Exercise an installed test app against a local mock without a Python on PATH."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'engine' / 'tests'))
import mock_relay


def run_app(app_bundle):
    app = Path(app_bundle) / 'Contents/MacOS/IMG Switch'
    if not app.is_file():
        raise RuntimeError('Pass the path to an IMG Switch.app bundle')
    server = mock_relay.start_mock('ok_b64')
    try:
        with tempfile.TemporaryDirectory(prefix='img-switch-test-') as temp:
            root = Path(temp)
            home = root / 'home with space'
            home.mkdir()
            config_root = home / '.config'
            cfg = config_root / 'failover-imagegen-tool/config.json'
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({'version': 1, 'sources': [{
                'name': 'mock', 'base_url': 'http://127.0.0.1:%s/v1' % server.server_address[1],
                'api_key': 'test-only', 'model': 'mock-image-1', 'enabled': True
            }]}), encoding='utf-8')
            env = dict(os.environ, HOME=str(home), FAILOVER_IMAGEGEN_APP_HOME=str(home),
                       XDG_CONFIG_HOME=str(config_root), PATH='/nonexistent',
                       FAILOVER_IMAGEGEN_APP_SMOKE='1')
            env.pop('FAILOVER_IMAGEGEN_TOOL_CONFIG', None)
            gui = subprocess.run([str(app), 'gui'], env=env, text=True, capture_output=True, timeout=30)
            if gui.returncode != 0 or 'app smoke ok' not in gui.stdout:
                raise RuntimeError('GUI smoke failed: %s %s' % (gui.stdout, gui.stderr))
            install = subprocess.run([str(app), 'on'], env=env, text=True, capture_output=True, timeout=30)
            if install.returncode:
                raise RuntimeError('installation failed: %s %s' % (install.stdout, install.stderr))
            skill = home / '.agents/skills/failover-imagegen-tool'
            engine = skill / 'bin/img-switch-engine'
            if not engine.is_file():
                raise RuntimeError('installed engine missing')
            engine.write_bytes(b'outdated managed engine')
            upgrade = subprocess.run([str(app), 'gui'], env=env, text=True,
                                     capture_output=True, timeout=30)
            bundled_engine = Path(app_bundle) / 'Contents/Resources/engine/bin/img-switch-engine'
            if upgrade.returncode or engine.read_bytes() != bundled_engine.read_bytes():
                raise RuntimeError('GUI launch did not upgrade the connected engine')
            p = subprocess.run([str(engine), 'generate', '--prompt', 'test image', '--name', 'smoke', '--size', '64x64'], cwd=root, env=env, text=True, capture_output=True, timeout=60)
            if p.returncode:
                raise RuntimeError('generation failed: %s %s' % (p.stdout, p.stderr))
            images = list((root / 'generated').glob('*.png'))
            metas = list((root / 'generated').glob('*.meta.json'))
            if len(images) != 1 or len(metas) != 1 or not images[0].read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
                raise RuntimeError('expected PNG and metadata are missing')
            off = subprocess.run([str(app), 'off'], env=env, text=True, capture_output=True, timeout=30)
            if off.returncode or skill.exists() or not cfg.is_file():
                raise RuntimeError('uninstall did not preserve configuration')
            print('PASS GUI, skill install and upgrade, no-Python mock generation, uninstall and config preservation')
    finally:
        server.shutdown()
        server.server_close()

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python3 packaging/macos/smoke.py "/path/to/IMG Switch.app"')
    run_app(sys.argv[1])
