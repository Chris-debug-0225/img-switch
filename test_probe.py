import socket
import unittest
import urllib.error
from unittest import mock

import app


class ProbeTests(unittest.TestCase):
    def test_temporary_connection_failure_is_retried(self):
        response = mock.MagicMock()
        with mock.patch('app.urllib.request.urlopen', side_effect=[
                urllib.error.URLError(ConnectionResetError('reset')), response]) as send, \
             mock.patch('app.time.sleep'):
            label, detail, ok = app.probe_relay('https://example.com/v1', {'Authorization': 'Bearer test'})
        self.assertTrue(ok)
        self.assertTrue(label.startswith('✓'))
        self.assertEqual(send.call_count, 2)

    def test_timeout_is_reported_as_timeout(self):
        with mock.patch('app.urllib.request.urlopen', side_effect=urllib.error.URLError(socket.timeout())), \
             mock.patch('app.time.sleep'):
            label, detail, ok = app.probe_relay('https://example.com/v1', {})
        self.assertFalse(ok)
        self.assertIn('超时', label + detail)

    def test_forbidden_is_not_reported_as_bad_key_or_retried(self):
        error = urllib.error.HTTPError('https://example.com/v1/models', 403, 'Forbidden', {}, None)
        with mock.patch('app.urllib.request.urlopen', side_effect=error) as send:
            label, detail, ok = app.probe_relay('https://example.com/v1', {})
        self.assertFalse(ok)
        self.assertIn('403', label + detail)
        self.assertNotIn('密钥无效', label + detail)
        self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
