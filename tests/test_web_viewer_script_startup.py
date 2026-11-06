"""Exercise routes while the viewer's script entry point is serving requests."""

import json
import logging
import runpy
import sqlite3
import sys
import threading
from configparser import ConfigParser
from contextlib import closing
from pathlib import Path

import pytest
from flask_socketio import SocketIO


def test_script_startup_can_add_clock_target(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "modules/web_viewer/app.py"
    database = tmp_path / "viewer.db"
    config_path = tmp_path / "config.ini"
    config = ConfigParser()
    config["Bot"] = {"db_path": str(database), "bot_name": "TestBot"}
    config["Web_Viewer"] = {"host": "127.0.0.1"}
    config["Logging"] = {"log_file": str(tmp_path / "bot.log")}
    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)

    class ServerStarted(BaseException):
        """Stop at the blocking server call, before module execution can resume."""

    public_key = "ab" * 32
    responses = []
    helpers = {}

    def serve_requests(_socketio, app, **_kwargs):
        namespace = app.view_functions["api_clock_sync_targets_admin_add"].__globals__
        helpers["validator"] = callable(namespace.get("_validate_clock_sync_target"))
        helpers["storage"] = callable(namespace.get("_storage_text"))
        with app.test_client() as client:
            for body in (
                {"target": "Test_relay", "public_key": public_key},
                {"target": "Test_relay", "public_key": public_key},
                {"target": ""},
            ):
                response = client.post(
                    "/api/clock-sync-targets-admin",
                    json=body,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                responses.append((response.status_code, json.loads(response.data)))
        raise ServerStarted()

    monkeypatch.setattr(sys, "argv", [str(script), "--config", str(config_path)])
    monkeypatch.setattr(threading.Thread, "start", lambda _self: None)
    monkeypatch.setattr(SocketIO, "run", serve_requests)
    logger = logging.getLogger("modern_web_viewer")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    try:
        with pytest.raises(ServerStarted):
            runpy.run_path(str(script), run_name="__main__")
    finally:
        for handler in logger.handlers:
            handler.close()

    assert helpers == {"validator": True, "storage": True}
    assert [status for status, _ in responses] == [201, 409, 400]
    assert responses[0][1]["target"] == public_key
    assert responses[2][1]["error"] == "target is required"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT target FROM clock_sync_targets").fetchall() == [
            (public_key,)
        ]
