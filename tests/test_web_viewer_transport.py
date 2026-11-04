"""Tests for the Socket.IO transport and origin decisions the web viewer makes.

These are what makes live data reach a browser through a reverse proxy. The
option `[Web_Viewer] websocket_enabled` was still documented in
config.ini.example while the merge dropped the code that read it, so the server
went back to accepting the polling-to-websocket upgrade and the client went back
to attempting it; behind a proxy that does not forward Upgrade/Connection, every
attempt fails and leaves another long-polling connection behind. The empty-CORS
fallback is the other half: the Origin a browser sends through a proxy is the
public one, not the address the viewer listens on.
"""

from __future__ import annotations

import configparser
import json
from unittest.mock import patch

from modules.web_viewer.app import BotDataViewer


def _viewer(tmp_path, web_viewer_options: dict[str, str]) -> BotDataViewer:
    """BotDataViewer against a temp config + empty DB, with [Web_Viewer] options set."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Web_Viewer")
    for key, value in web_viewer_options.items():
        config.set("Web_Viewer", key, value)

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as handle:
        config.write(handle)

    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        instance = BotDataViewer(
            db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)
    instance.app.testing = True
    return instance


class TestTransportConfig:
    def test_polling_only_by_default(self, tmp_path):
        """Default: no upgrade offered, which is what a proxy without Upgrade can serve."""
        viewer = _viewer(tmp_path, {"web_viewer_password": ""})
        assert viewer._websocket_enabled() is False
        assert viewer.socketio.server.eio.transports == ["polling"]

    def test_enabling_it_offers_the_upgrade(self, tmp_path):
        """Only when the proxy forwards Upgrade/Connection, per the option's own comment."""
        viewer = _viewer(tmp_path, {"websocket_enabled": "true", "web_viewer_password": ""})
        assert viewer._websocket_enabled() is True
        assert viewer.socketio.server.eio.transports == ["websocket", "polling"]

    def test_empty_cors_falls_back_to_any_origin(self, tmp_path):
        """Through a proxy the Origin is the public host, not the listener address."""
        viewer = _viewer(tmp_path, {"web_viewer_password": ""})
        assert viewer.socketio.server.eio.cors_allowed_origins == "*"

    def test_explicit_cors_is_honoured(self, tmp_path):
        """The fallback is for an unset option, not an override of an operator's list."""
        viewer = _viewer(
            tmp_path,
            {"cors_allowed_origins": "https://bot.tigro.fr", "web_viewer_password": ""},
        )
        assert viewer.socketio.server.eio.cors_allowed_origins == ["https://bot.tigro.fr"]

    def test_handshake_does_not_advertise_an_upgrade_it_refuses(self, tmp_path):
        """The advertisement has to be turned off with the transport, not left at its default.

        engineio builds the handshake's "upgrades" from its async driver table, not from the
        transports it was given, so a polling-only server would still promise websocket and a
        client would attempt something this server refuses.
        """
        viewer = _viewer(tmp_path, {"web_viewer_password": ""})
        body = viewer.app.test_client().get("/socket.io/?EIO=4&transport=polling").data.decode()
        assert "websocket" not in json.loads(body[1:])["upgrades"]

    def test_handshake_advertises_the_upgrade_when_it_is_enabled(self, tmp_path):
        viewer = _viewer(tmp_path, {"websocket_enabled": "true", "web_viewer_password": ""})
        body = viewer.app.test_client().get("/socket.io/?EIO=4&transport=polling").data.decode()
        assert "websocket" in json.loads(body[1:])["upgrades"]


class TestClientFollowsTheFlag:
    def test_client_does_not_attempt_an_upgrade_when_disabled(self, tmp_path):
        """A client told to upgrade against a polling-only server wastes attempts."""
        body = _viewer(tmp_path, {"web_viewer_password": ""}).app.test_client().get("/").data.decode()
        assert 'data-ws-enabled="false"' in body
        assert "transports: WS_ENABLED ? ['websocket', 'polling'] : ['polling']" in body
        assert "upgrade: WS_ENABLED" in body

    def test_client_sees_the_flag_when_enabled(self, tmp_path):
        body = _viewer(
            tmp_path, {"websocket_enabled": "true", "web_viewer_password": ""}
        ).app.test_client().get("/").data.decode()
        assert 'data-ws-enabled="true"' in body
