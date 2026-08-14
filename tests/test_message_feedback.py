import json
import sqlite3
from collections import OrderedDict
from types import SimpleNamespace


def _client_message_ref(message):
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text") or part.get("content") or part.get("input_text") or "")
            if isinstance(part, dict)
            else str(part or "")
            for part in content
        )
    return json.dumps(
        {
            "role": str(message.get("role") or ""),
            "content": " ".join(str(content or "").split()),
            "timestamp": message.get("_ts") or message.get("timestamp") or "",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _isolated_session(tmp_path, monkeypatch):
    from api import message_feedback, models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(message_feedback, "STATE_DIR", tmp_path)

    messages = [
        {"role": "user", "content": "question", "timestamp": 10.0},
        {"role": "assistant", "content": "first answer", "timestamp": 11.0},
        {"role": "user", "content": "follow up", "timestamp": 12.0},
        {"role": "assistant", "content": "second answer", "timestamp": 13.0},
        {"role": "user", "content": "one more", "timestamp": 14.0},
        {"role": "assistant", "content": "intermediate", "timestamp": 15.0},
        {"role": "assistant", "content": "final answer", "timestamp": 16.0},
    ]
    session = Session(session_id="feedback-session", messages=messages)
    session.save(skip_index=True)
    return message_feedback, models, routes, messages


def _post_feedback(routes, monkeypatch, payload):
    captured = {}

    def capture(_handler, body, status=200, extra_headers=None):
        captured.update(payload=body, status=status)
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: payload)
    monkeypatch.setattr(routes, "j", capture)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: capture(handler, {"error": message}, status),
    )
    assert routes.handle_post(
        SimpleNamespace(command="POST"),
        SimpleNamespace(path="/api/message-feedback"),
    ) is True
    return captured


def test_message_feedback_three_state_round_trip(tmp_path, monkeypatch):
    message_feedback, _models, routes, messages = _isolated_session(tmp_path, monkeypatch)
    ref = _client_message_ref(messages[1])

    liked = _post_feedback(
        routes,
        monkeypatch,
        {"session_id": "feedback-session", "message_ref": ref, "feedback": "like"},
    )
    assert liked == {
        "status": 200,
        "payload": {"ok": True, "feedback": "like"},
    }
    stored = message_feedback.feedback_for_session("feedback-session")
    assert len(stored) == 1
    assert next(iter(stored.values())) == "like"

    disliked = _post_feedback(
        routes,
        monkeypatch,
        {"session_id": "feedback-session", "message_ref": ref, "feedback": "dislike"},
    )
    assert disliked == {
        "status": 200,
        "payload": {"ok": True, "feedback": "dislike"},
    }
    stored = message_feedback.feedback_for_session("feedback-session")
    assert len(stored) == 1
    assert next(iter(stored.values())) == "dislike"

    cleared = _post_feedback(
        routes,
        monkeypatch,
        {"session_id": "feedback-session", "message_ref": ref, "feedback": None},
    )
    assert cleared == {
        "status": 200,
        "payload": {"ok": True, "feedback": None},
    }
    assert message_feedback.feedback_for_session("feedback-session") == {}


def test_message_feedback_rejects_invalid_state_and_non_assistant_target(tmp_path, monkeypatch):
    _message_feedback, _models, routes, messages = _isolated_session(tmp_path, monkeypatch)

    invalid = _post_feedback(
        routes,
        monkeypatch,
        {
            "session_id": "feedback-session",
            "message_ref": _client_message_ref(messages[1]),
            "feedback": "helpful",
        },
    )
    assert invalid["status"] == 400
    assert invalid["payload"] == {"error": "feedback must be like, dislike, or null"}

    user_target = _post_feedback(
        routes,
        monkeypatch,
        {
            "session_id": "feedback-session",
            "message_ref": _client_message_ref(messages[0]),
            "feedback": "like",
        },
    )
    assert user_target["status"] == 404
    assert user_target["payload"] == {"error": "Assistant message not found"}

    intermediate_target = _post_feedback(
        routes,
        monkeypatch,
        {
            "session_id": "feedback-session",
            "message_ref": _client_message_ref(messages[5]),
            "feedback": "like",
        },
    )
    assert intermediate_target["status"] == 404
    assert intermediate_target["payload"] == {"error": "Assistant message not found"}


def test_session_projection_attaches_feedback_without_mutating_session_messages(tmp_path, monkeypatch):
    message_feedback, _models, routes, messages = _isolated_session(tmp_path, monkeypatch)
    first_ref = routes._normalize_anchor_scene_message_ref(_client_message_ref(messages[1]))
    second_ref = routes._normalize_anchor_scene_message_ref(_client_message_ref(messages[3]))
    message_feedback.set_feedback("feedback-session", first_ref, "like")
    message_feedback.set_feedback("feedback-session", second_ref, "dislike")

    projected = routes._messages_with_feedback("feedback-session", messages)

    assert projected[0].get("_feedback") is None
    assert projected[1]["_feedback"] == "like"
    assert projected[3]["_feedback"] == "dislike"
    assert all("_feedback" not in message for message in messages)

    db = sqlite3.connect(tmp_path / "message_feedback.db")
    try:
        rows = db.execute(
            "SELECT session_id, message_ref, feedback FROM message_feedback ORDER BY message_ref"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"feedback-session"}
    assert {row[2] for row in rows} == {"like", "dislike"}

    message_feedback.prune_feedback_for_session("feedback-session", {first_ref})
    assert message_feedback.feedback_for_session("feedback-session") == {first_ref: "like"}

    message_feedback.prune_feedback_for_session("feedback-session", set())
    assert message_feedback.feedback_for_session("feedback-session") == {}
