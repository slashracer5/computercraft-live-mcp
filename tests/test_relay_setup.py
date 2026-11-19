"""Exercise relay setup without starting Minecraft or binding a port."""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from computercraft_live_mcp import relay


class RelaySetupTest(unittest.TestCase):
    def run_setup(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["computercraft-live-mcp-relay", "--setup", *args]),
            patch.object(relay, "load_tokens", return_value={"computer": "test-token"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            relay.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_wildcard_bind_advertises_detected_address(self):
        for bind, address, url_host in [
            ("0.0.0.0", "192.168.1.25", "192.168.1.25"),
            ("::", "fd00::25", "[fd00::25]"),
        ]:
            with self.subTest(bind=bind):
                with patch.object(relay, "_default_route_address", return_value=address) as detect:
                    output, warning = self.run_setup("--host", bind, "--port", "9876")
                detect.assert_called_once_with(bind)
                self.assertIn(f"wget http://{url_host}:9876/bridge.lua", output)
                self.assertIn(f"bridge ws://{url_host}:9876/ws test-token", output)
                self.assertIn(f'[[http.rules]]\n        host = "{address}"\n        port = 9876', output)
                self.assertIn("--advertise-host", warning)

    def test_explicit_advertised_host_skips_detection(self):
        with patch.object(relay, "_default_route_address") as detect:
            output, warning = self.run_setup("--host", "0.0.0.0", "--advertise-host", "relay.local")
        detect.assert_not_called()
        self.assertIn("http://relay.local:8765/bridge.lua", output)
        self.assertEqual(warning, "")

    def test_default_setup_still_uses_loopback(self):
        output, warning = self.run_setup()
        self.assertIn("http://127.0.0.1:8765/bridge.lua", output)
        self.assertEqual(warning, "")

    def test_missing_route_explains_how_to_supply_an_address(self):
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["computercraft-live-mcp-relay", "--setup", "--host", "::"]),
            patch.object(relay, "_default_route_address", side_effect=OSError),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_error,
        ):
            relay.main()
        self.assertEqual(exit_error.exception.code, 2)
        self.assertIn("set --advertise-host", stderr.getvalue())
