import io
import json

import pytest


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.response


def test_feedback_client_sends_server_owned_snapshot_with_dedicated_bearer(monkeypatch):
    from api import i2stream_feedback_client as client

    response = FakeResponse(
        {
            "code": 0,
            "feedback": "like",
            "status": "queued",
            "job_id": "a" * 32,
            "session_id": "session-one",
            "message_ref": "f" * 64,
        }
    )
    opener = FakeOpener(response)
    monkeypatch.setattr(client, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(client, "I2STREAM_FEEDBACK_BRIDGE_TOKEN", "s" * 64)
    monkeypatch.setattr(client, "build_opener", lambda *_handlers: opener)

    result = client.submit_webui_feedback(
        "e" * 64,
        "session-one",
        "f" * 64,
        "like",
        [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}],
    )

    assert result["job_id"] == "a" * 32
    request, timeout = opener.calls[0]
    assert request.full_url == (
        "http://127.0.0.1:50091/api/webui/sessions/session-one/messages/"
        + "f" * 64
        + "/feedback"
    )
    assert timeout == client.I2STREAM_CONSOLE_TIMEOUT_SECONDS
    assert request.get_header("Authorization") == "Bearer " + "s" * 64
    assert json.loads(request.data) == {
        "source_instance_id": "e" * 64,
        "feedback": "like",
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
    }


def test_feedback_client_fails_closed_without_bridge_token(monkeypatch):
    from api import i2stream_feedback_client as client

    monkeypatch.setattr(client, "I2STREAM_FEEDBACK_BRIDGE_TOKEN", "")
    with pytest.raises(client.FeedbackServiceError, match="token is not configured"):
        client.get_webui_feedback_job("e" * 64, "a" * 32)


def test_feedback_client_rejects_invalid_origin_port(monkeypatch):
    from api import i2stream_feedback_client as client

    monkeypatch.setattr(client, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:not-a-port")
    monkeypatch.setattr(client, "I2STREAM_FEEDBACK_BRIDGE_TOKEN", "s" * 64)

    with pytest.raises(client.FeedbackServiceError, match="Invalid i2Stream"):
        client.get_webui_feedback_job("e" * 64, "a" * 32)
