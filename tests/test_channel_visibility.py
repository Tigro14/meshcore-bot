"""Channel web visibility is opt-in and enforced before data leaves the server."""

import json
import sqlite3
import time
from unittest.mock import Mock

import flask_socketio
import pytest

from modules.web_viewer.channel_visibility import (
    can_publish,
    channel_identity,
    encrypted_packet_view,
    message_identity,
    set_visibility,
)
from tests.test_web_viewer import managed_socketio_clients as managed_socketio_clients
from tests.test_web_viewer import socketio_viewer as socketio_viewer
from tests.test_web_viewer_app import viewer_with_db as viewer_with_db


def add_channel(viewer, name="#test", key="ab" * 16, idx=1, visible=False):
    identity = channel_identity(name, key)
    with sqlite3.connect(viewer.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO channels (channel_idx, channel_name, channel_type, channel_key_hex) VALUES (?, ?, ?, ?)",
            (idx, name, "custom", key),
        )
        set_visibility(conn, identity, visible)
    return identity


def message(identity, channel="#test", **extra):
    return dict(channel_visibility_id=identity, channel=channel, content="private content", is_dm=False, **extra)


def test_unconfigured_channels_are_hidden_and_no_private_key_in_decoder_api(viewer_with_db):
    viewer = viewer_with_db
    add_channel(viewer)
    client = viewer.app.test_client()
    channels = client.get("/radio/api/channels").get_json()["channels"]
    assert channels[0]["live_messages_visible"] is False
    assert client.get("/api/channels/live").get_json() == {"channels": []}
    result = client.put(
        "/radio/api/channels/live-visibility",
        json=dict(channel_idx=1, channel_name="#test", live_messages_visible=True),
    )
    assert result.status_code == 200
    visible = client.get("/api/channels/live").get_json()["channels"]
    assert visible == [{"name": "#test", "type": "custom"}]
    assert "ab" * 16 not in json.dumps(visible)


@pytest.mark.parametrize("value", ["false", 1, None, [], {}])
def test_visibility_requires_boolean(viewer_with_db, value):
    add_channel(viewer_with_db)
    response = viewer_with_db.app.test_client().put(
        "/radio/api/channels/live-visibility",
        json=dict(channel_idx=1, channel_name="#test", live_messages_visible=value),
    )
    assert response.status_code == 400


def test_unchecked_create_removes_previous_consent(viewer_with_db):
    viewer = viewer_with_db
    key = "ab" * 16
    identity = add_channel(viewer, name="private", visible=True)
    viewer._add_channel_for_web = Mock(return_value={"success": True, "pending": True, "operation_id": 3})
    with sqlite3.connect(viewer.db_path) as conn:
        assert can_publish(conn, message(identity, "private"))
    result = viewer.app.test_client().post(
        "/radio/api/channels", json=dict(name="private", channel_idx=1, channel_key=key)
    )
    assert result.status_code == 200
    with sqlite3.connect(viewer.db_path) as conn:
        assert not can_publish(conn, message(identity, "private"))


def test_checked_create_stores_consent_before_pending_operation(viewer_with_db):
    viewer = viewer_with_db
    viewer._add_channel_for_web = Mock(return_value={"success": True, "pending": True, "operation_id": 3})
    response = viewer.app.test_client().post(
        "/radio/api/channels", json=dict(name="#new", channel_idx=3, live_messages_visible=True)
    )
    assert response.status_code == 200
    # Simulate the radio's later asynchronous confirmation.
    with sqlite3.connect(viewer.db_path) as conn:
        key = __import__("hashlib").sha256(b"#new").hexdigest()[:32]
        conn.execute(
            "INSERT INTO channels(channel_idx,channel_name,channel_type,channel_key_hex) VALUES(3,'#new','hashtag',?)",
            (key,),
        )
        assert can_publish(conn, message(channel_identity("#new"), "#new"))


def test_permission_survives_sync_but_not_key_replacement_or_ambiguous_names(viewer_with_db):
    viewer = viewer_with_db
    identity = add_channel(viewer, visible=True)
    with sqlite3.connect(viewer.db_path) as conn:
        assert can_publish(conn, message(identity))
        assert not can_publish(conn, dict(channel="#test", content="legacy"))
        assert not can_publish(conn, message(identity) | {"is_dm": True})
    add_channel(viewer, key="cd" * 16)
    with sqlite3.connect(viewer.db_path) as conn:
        assert not can_publish(conn, message(identity))
    add_channel(viewer, visible=True)
    add_channel(viewer, name="test", key="cd" * 16, idx=2)
    with sqlite3.connect(viewer.db_path) as conn:
        assert message_identity(conn, "#test") is None
        assert not can_publish(conn, message(identity))


@pytest.mark.usefixtures("managed_socketio_clients")
def test_live_and_history_never_emit_hidden_or_dm_messages(socketio_viewer):
    viewer = socketio_viewer
    hidden = add_channel(viewer)
    allowed = add_channel(viewer, name="#visible", idx=2, key="cd" * 16, visible=True)
    client = flask_socketio.SocketIOTestClient(viewer.app, viewer.socketio)
    now = time.time()
    rows = [
        message(hidden),
        message(allowed, "#visible"),
        message(allowed, "#visible") | {"is_dm": True},
        dict(content="unstamped legacy"),
    ]
    with sqlite3.connect(viewer.db_path) as conn:
        conn.executemany(
            "INSERT INTO packet_stream(timestamp,data,type) VALUES(?,?,?)",
            [(now + i, json.dumps(row), "message") for i, row in enumerate(rows)],
        )
    client.emit("subscribe_messages")
    events = [event for event in client.get_received() if event["name"] == "message_data"]
    assert len(events) == 1 and events[0]["args"][0]["channel"] == "#visible"
    viewer._handle_message_data(message(hidden))
    assert not any(event["name"] == "message_data" for event in client.get_received())
    viewer._handle_message_data(message(allowed, "#visible"))
    assert any(event["name"] == "message_data" for event in client.get_received())
    viewer.app.test_client().put(
        "/radio/api/channels/live-visibility",
        json=dict(channel_idx=2, channel_name="#visible", live_messages_visible=False),
    )
    client.get_received()
    viewer._handle_message_data(message(allowed, "#visible"))
    assert not any(event["name"] == "message_data" for event in client.get_received())


def test_hidden_commands_and_packet_plaintext_do_not_bypass_policy(viewer_with_db):
    viewer = viewer_with_db
    identity = add_channel(viewer)
    payload = message(identity) | dict(user_input="Baliz secret", response="secret answer")
    assert not viewer._can_publish_channel_data(payload, command=True)
    viewer.socketio.emit = Mock()
    viewer.connected_clients = {"test": {"subscribed_commands": True, "subscribed_messages": True}}
    viewer._handle_command_data(payload)
    viewer.socketio.emit.assert_not_called()
    packet = {
        "raw_hex": "1234",
        "decoded": {"decrypted": True, "text": "secret", "sender": "private", "channel_hash": "ab"},
        "decrypted_message": "secret",
    }
    clean = encrypted_packet_view(packet)
    assert "secret" not in json.dumps(clean) and "private" not in json.dumps(clean)
    assert clean["raw_hex"] == "1234"


def test_legacy_create_api_cannot_enable_publication(viewer_with_db):
    viewer = viewer_with_db
    viewer._add_channel_for_web = Mock()
    response = viewer.app.test_client().post("/api/channels", json=dict(name="#new", live_messages_visible=True))
    assert response.status_code == 403
    viewer._add_channel_for_web.assert_not_called()


def test_anonymous_api_only_lists_consented_channels_without_keys(viewer_with_db):
    viewer = viewer_with_db
    add_channel(viewer)
    add_channel(viewer, name="#visible", key="cd" * 16, idx=2, visible=True)
    public = viewer.app.test_client().get("/api/channels").get_json()
    assert public == {"channels": [{"name": "#visible", "type": "custom"}]}
    viewer.web_viewer_password = "configured-password"
    client = viewer.app.test_client()
    assert client.get("/radio/api/channels").status_code == 401
    assert client.put("/radio/api/channels/live-visibility", json=dict(live_messages_visible=True)).status_code == 401
    assert client.get("/api/channels/live").status_code == 200


def test_radio_sync_does_not_reset_visibility(viewer_with_db):
    viewer = viewer_with_db
    identity = add_channel(viewer, visible=True)
    with sqlite3.connect(viewer.db_path) as conn:
        conn.execute("DELETE FROM channels")
        conn.execute(
            "INSERT INTO channels(channel_idx,channel_name,channel_type,channel_key_hex) VALUES(3,'#test','custom',?)",
            ("ab" * 16,),
        )
        assert can_publish(conn, message(identity))


def test_capture_keeps_hidden_messages_locally_with_their_channel_identity(viewer_with_db):
    from modules.models import MeshMessage
    from modules.web_viewer.integration import BotIntegration

    viewer = viewer_with_db
    identity = add_channel(viewer)
    capture = object.__new__(BotIntegration)
    capture._get_web_viewer_db_path = lambda: viewer.db_path
    capture._insert_packet_stream_row = Mock()
    capture.bot = Mock()
    capture.capture_channel_message(MeshMessage(content="local only", channel="#test", sender_id="node", is_dm=False))
    payload = json.loads(capture._insert_packet_stream_row.call_args.args[0])
    assert payload["content"] == "local only" and payload["channel_visibility_id"] == identity
    assert not viewer._can_publish_channel_data(payload)


@pytest.mark.usefixtures("managed_socketio_clients")
def test_message_broadcast_only_reaches_subscribed_clients(socketio_viewer):
    viewer = socketio_viewer
    identity = add_channel(viewer, visible=True)
    subscriber = flask_socketio.SocketIOTestClient(viewer.app, viewer.socketio)
    bystander = flask_socketio.SocketIOTestClient(viewer.app, viewer.socketio)
    subscriber.emit("subscribe_messages")
    subscriber.get_received()
    bystander.get_received()
    viewer._handle_message_data(message(identity))
    assert any(event["name"] == "message_data" for event in subscriber.get_received())
    assert not any(event["name"] == "message_data" for event in bystander.get_received())


def test_new_management_api_keeps_csrf_protection(viewer_with_db):
    viewer = viewer_with_db
    add_channel(viewer)
    viewer.app.testing = False
    client = viewer.app.test_client()
    payload = dict(channel_idx=1, channel_name="#test", live_messages_visible=True)
    assert client.put("/radio/api/channels/live-visibility", json=payload).status_code == 403
    assert (
        client.put(
            "/radio/api/channels/live-visibility", json=payload, headers={"X-Requested-With": "XMLHttpRequest"}
        ).status_code
        == 200
    )


@pytest.mark.usefixtures("managed_socketio_clients")
def test_command_history_and_recent_api_do_not_expose_hidden_channel_text(socketio_viewer):
    viewer = socketio_viewer
    hidden = add_channel(viewer)
    visible = add_channel(viewer, name="#visible", key="cd" * 16, idx=2, visible=True)
    rows = [
        message(hidden) | {"user_input": "HIDDEN QUESTION", "response": "HIDDEN ANSWER"},
        message(visible, "#visible") | {"user_input": "public question", "response": "public answer"},
    ]
    with sqlite3.connect(viewer.db_path) as conn:
        conn.executemany(
            "INSERT INTO packet_stream(timestamp,data,type) VALUES(?,?,?)",
            [(time.time(), json.dumps(row), "command") for row in rows],
        )
    client = flask_socketio.SocketIOTestClient(viewer.app, viewer.socketio)
    client.emit("subscribe_commands")
    events = [event for event in client.get_received() if event["name"] == "command_data"]
    assert len(events) == 1 and events[0]["args"][0]["channel"] == "#visible"
    response = viewer.app.test_client().get("/api/recent_commands").get_json()
    assert len(response["commands"]) == 1
    assert "HIDDEN" not in json.dumps(response)


@pytest.mark.usefixtures("managed_socketio_clients")
def test_packet_history_keeps_raw_radio_data_without_server_decrypted_text(socketio_viewer):
    viewer = socketio_viewer
    packet = {
        "raw_hex": "deadbeef",
        "snr": -4.5,
        "path": ["aa", "bb"],
        "decrypted_message": "HIDDEN MESSAGE",
        "decoded": {"decrypted": {"text": "HIDDEN MESSAGE"}, "payload_hex": "aabbcc"},
    }
    with sqlite3.connect(viewer.db_path) as conn:
        conn.execute(
            "INSERT INTO packet_stream(timestamp,data,type) VALUES(?,?,?)", (time.time(), json.dumps(packet), "packet")
        )
    client = flask_socketio.SocketIOTestClient(viewer.app, viewer.socketio)
    client.emit("subscribe_packets")
    events = [event for event in client.get_received() if event["name"] == "packet_data"]
    assert len(events) == 1
    data = events[0]["args"][0]
    assert "HIDDEN" not in json.dumps(data)
    assert data["raw_hex"] == "deadbeef" and data["path"] == ["aa", "bb"]
    assert data["snr"] == -4.5 and data["decoded"]["payload_hex"] == "aabbcc"
