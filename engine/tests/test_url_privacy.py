#!/usr/bin/env python3
"""Regression tests for credentials in displayed URL hosts."""
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import failovercfg
import failover_imagegen_tool as imagegen
import setup as config_setup


class UrlPrivacyTests(unittest.TestCase):
    def assert_safe_output(self, output):
        self.assertIn("relay.example.com", output)
        for secret in ("example-user", "password", "private"):
            self.assertNotIn(secret, output)

    def test_host_excludes_userinfo_path_query_and_fragment(self):
        self.assertEqual(
            failovercfg.endpoint_host("https://example-user:password@relay.example.com:8443/v1?key=private#token"),
            "relay.example.com:8443",
        )
        self.assertEqual(failovercfg.endpoint_host("https://relay.example.com/v1"), "relay.example.com")
        self.assertEqual(failovercfg.endpoint_host("http://u:p@[::1]:8443/v1"), "[::1]:8443")

    def test_malformed_or_missing_host_never_returns_original_input(self):
        for url in ("https://u:private@[broken", "https://u:private@host:bad/v1", "u:private@host", "https:///private"):
            with self.subTest(url=url):
                self.assertEqual(failovercfg.endpoint_host(url), "<无效地址>")

    def test_download_failure_does_not_expose_url_or_credentials(self):
        url = "https://example-user:password@relay.example.com/file?token=private"
        with mock.patch.object(imagegen.urllib.request, "urlopen", side_effect=urllib.error.URLError(url)):
            with self.assertRaises(imagegen.AttemptError) as raised:
                imagegen._download(url, 1)
        message = str(raised.exception)
        self.assert_safe_output(message)
        self.assertNotIn("token", message)

    def test_list_verbose_and_setup_do_not_print_url_credentials(self):
        url = "https://example-user:password@relay.example.com/v1?token=private"
        source = {"name": "test", "base_url": url, "api_key": "dummy", "model": "image"}
        stream = StringIO()
        with mock.patch.object(imagegen, "_load_cfg_or_exit", return_value={"sources": [source]}), redirect_stdout(stream):
            imagegen.cmd_list_sources(None)
        self.assert_safe_output(stream.getvalue())

        stream = StringIO()
        with mock.patch.object(imagegen, "_http_json", return_value=(200, {}, b"")), \
             mock.patch.object(imagegen, "_extract_image", return_value=b"image"), \
             mock.patch.object(imagegen, "detect_image", return_value=("png", 1, 1)), redirect_stdout(stream):
            imagegen.attempt_source(source, "prompt", (1, 1), None, "image", verbose=True)
        self.assert_safe_output(stream.getvalue())

        stream = StringIO()
        args = SimpleNamespace(name="test", base_url=url, api_key="dummy", model=["image"], disabled=False, timeout=None)
        with mock.patch.object(config_setup, "_load_or_exit", return_value={"sources": []}), \
             mock.patch.object(config_setup, "_save_and_report"), redirect_stdout(stream):
            config_setup.cmd_add(args)
        self.assert_safe_output(stream.getvalue())


if __name__ == "__main__":
    unittest.main()
