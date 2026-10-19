"""Regression tests for configurable LND TLS hostname verification."""

import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch


class LNDTLSHostnameTests(TestCase):
    def check_hostname(self, hostname, expected_options):
        source = Path(__file__).resolve().parents[1] / "lightning" / "lnd.py"

        config_values = {
            "LND_DIR": "/nonexistent-lnd-test-directory",
            "LND_CERT_BASE64": "dGVzdC1jZXJ0",
            "LND_MACAROON_BASE64": "dGVzdC1tYWNhcm9vbg==",
            "LND_GRPC_HOST": "127.0.0.1:10009",
            "LND_TLS_SERVER_NAME": hostname,
        }

        def fake_config(key, default=None, **kwargs):
            return config_values.get(key, default)

        spec = importlib.util.spec_from_file_location(
            "api.lightning._lnd_tls_test",
            source,
        )
        module = importlib.util.module_from_spec(spec)

        with (
            patch(
                "decouple.config",
                side_effect=fake_config,
            ),
            patch(
                "grpc.ssl_channel_credentials",
                return_value=MagicMock(),
            ),
            patch(
                "grpc.metadata_call_credentials",
                return_value=MagicMock(),
            ),
            patch(
                "grpc.composite_channel_credentials",
                return_value=MagicMock(),
            ),
            patch(
                "grpc.secure_channel",
                return_value=MagicMock(),
            ) as mock_channel,
        ):
            spec.loader.exec_module(module)

        self.assertEqual(
            mock_channel.call_args.kwargs["options"],
            expected_options,
        )

    def test_custom_hostname(self):
        self.check_hostname(
            "umbrel.local",
            [("grpc.ssl_target_name_override", "umbrel.local")],
        )

    def test_empty_hostname(self):
        self.check_hostname("", [])

    def test_whitespace_hostname(self):
        self.check_hostname("   ", [])
