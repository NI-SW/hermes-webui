from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

os.environ.setdefault("VECTOR_SEARCH_HOST", "http://127.0.0.1:8900")
os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

from config import Settings


class VectorSearchHostConfigTests(unittest.TestCase):
    secret_settings = {
        "session_hmac_secret": "session-secret-32-bytes-for-tests!!",
        "gateway_bridge_token": "gateway-token-32-bytes-for-tests!!!",
    }

    def test_file_store_defaults_are_under_app_data(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(
                _env_file=None,
                vector_search_host="http://127.0.0.1:8900",
                **self.secret_settings,
            )

        self.assertEqual(settings.file_store_dir, Path("/app/data/agent-console/files"))
        self.assertEqual(settings.inbox_file_store_dir, Path("/app/data/agent-console/inbox-files"))

    def test_dashboard_agent_defaults_to_8641_and_redacts_its_key(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://127.0.0.1:8900",
            dashboard_hermes_api_key="stream-qa-secret",
            **self.secret_settings,
        )

        self.assertEqual(settings.dashboard_hermes_base_url, "http://127.0.0.1:8641")
        self.assertEqual(settings.dashboard_hermes_api_key.get_secret_value(), "stream-qa-secret")
        self.assertNotIn("stream-qa-secret", repr(settings))

    def test_feedback_hermes_api_key_is_redacted(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://127.0.0.1:8900",
            hermes_api_key="feedback-hermes-secret",
            **self.secret_settings,
        )

        self.assertEqual(
            settings.hermes_api_key.get_secret_value(),
            "feedback-hermes-secret",
        )
        self.assertNotIn("feedback-hermes-secret", repr(settings))

    def test_vector_search_host_accepts_http_host_port(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://192.168.34.65:8900",
            **self.secret_settings,
        )

        self.assertEqual(settings.vector_search_host, "http://192.168.34.65:8900")

    def test_vector_search_host_strips_trailing_slash(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://192.168.34.65:8900/",
            **self.secret_settings,
        )

        self.assertEqual(settings.vector_search_host, "http://192.168.34.65:8900")

    def test_vector_search_host_rejects_missing_port(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                vector_search_host="http://192.168.34.65",
                **self.secret_settings,
            )

    def test_vector_service_base_url_is_not_supported(self) -> None:
        with patch.dict(os.environ, {"VECTOR_SERVICE_BASE_URL": "http://192.168.34.65:8900"}, clear=True):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_bridge_secrets_are_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError) as raised:
                Settings(_env_file=None, vector_search_host="http://127.0.0.1:8900")

        errors = raised.exception.errors()
        self.assertEqual(
            {error["loc"] for error in errors},
            {("session_hmac_secret",), ("gateway_bridge_token",)},
        )

    def test_bridge_secrets_require_at_least_32_utf8_bytes(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            Settings(
                _env_file=None,
                vector_search_host="http://127.0.0.1:8900",
                session_hmac_secret="short",
                gateway_bridge_token="also-short",
            )

        self.assertEqual(
            {error["loc"] for error in raised.exception.errors()},
            {("session_hmac_secret",), ("gateway_bridge_token",)},
        )

    def test_bridge_secret_values_are_redacted_from_repr(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://127.0.0.1:8900",
            **self.secret_settings,
        )

        representation = repr(settings)
        self.assertNotIn(self.secret_settings["session_hmac_secret"], representation)
        self.assertNotIn(self.secret_settings["gateway_bridge_token"], representation)

    def test_webui_feedback_bridge_token_is_redacted(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://127.0.0.1:8900",
            webui_feedback_bridge_token="native-feedback-secret-at-least-32-bytes",
            **self.secret_settings,
        )

        self.assertEqual(
            settings.webui_feedback_bridge_token.get_secret_value(),
            "native-feedback-secret-at-least-32-bytes",
        )
        self.assertNotIn("native-feedback-secret-at-least-32-bytes", repr(settings))

    def test_configured_webui_feedback_bridge_token_requires_32_bytes(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            Settings(
                _env_file=None,
                vector_search_host="http://127.0.0.1:8900",
                webui_feedback_bridge_token="short",
                **self.secret_settings,
            )

        self.assertEqual(
            {error["loc"] for error in raised.exception.errors()},
            {("webui_feedback_bridge_token",)},
        )

    def test_datacop_uses_built_in_agent_project_defaults(self) -> None:
        settings = Settings(
            _env_file=None,
            vector_search_host="http://127.0.0.1:8900",
            **self.secret_settings,
        )

        self.assertEqual(settings.datacop_base_url, "http://192.168.34.65:5173/api")
        self.assertEqual(settings.datacop_project_id, 13)
        self.assertEqual(settings.datacop_username, "root")
        self.assertEqual(settings.datacop_password.get_secret_value(), "admin123")
        self.assertEqual(settings.datacop_timeout_seconds, 30.0)
        self.assertEqual(settings.dialog_interaction_queue_size, 32)
        self.assertEqual(settings.dialog_interaction_job_ttl_seconds, 900.0)
        self.assertNotIn("admin123", repr(settings))

    def test_datacop_defaults_ignore_environment_overrides(self) -> None:
        overrides = {
            "DATACOP_BASE_URL": "http://127.0.0.1:9999/api",
            "DATACOP_PROJECT_ID": "999",
            "DATACOP_USERNAME": "other-user",
            "DATACOP_PASSWORD": "other-password",
            "DATACOP_TIMEOUT_SECONDS": "1",
            "DIALOG_INTERACTION_QUEUE_SIZE": "1",
            "DIALOG_INTERACTION_JOB_TTL_SECONDS": "1",
        }
        with patch.dict(os.environ, overrides, clear=False):
            settings = Settings(
                _env_file=None,
                vector_search_host="http://127.0.0.1:8900",
                **self.secret_settings,
            )

        self.assertEqual(settings.datacop_base_url, "http://192.168.34.65:5173/api")
        self.assertEqual(settings.datacop_project_id, 13)
        self.assertEqual(settings.datacop_username, "root")
        self.assertEqual(settings.datacop_password.get_secret_value(), "admin123")
        self.assertEqual(settings.datacop_timeout_seconds, 30.0)
        self.assertEqual(settings.dialog_interaction_queue_size, 32)
        self.assertEqual(settings.dialog_interaction_job_ttl_seconds, 900.0)


if __name__ == "__main__":
    unittest.main()
