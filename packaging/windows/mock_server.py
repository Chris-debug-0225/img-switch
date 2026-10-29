#!/usr/bin/env python3
"""Run the existing loopback mock for the Windows installation smoke test."""
from pathlib import Path
import sys
import threading

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'engine/tests'))
import mock_relay

server = mock_relay.start_mock('ok_b64')
Path(sys.argv[1]).write_text(str(server.server_address[1]), encoding='ascii')
threading.Event().wait()
