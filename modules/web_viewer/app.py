#!/usr/bin/env python3
"""
MeshCore Bot Data Viewer
Bot montoring web interface using Flask-SocketIO 5.x
"""

import asyncio
import configparser
import functools
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timedelta, timezone  # noqa: F401  timedelta kept for modules.web_viewer.app.timedelta
from pathlib import Path
from typing import Any, NamedTuple, Optional  # noqa: F401  NamedTuple kept for modules.web_viewer.app.NamedTuple
from urllib.parse import urlparse

# When started as a script (`python modules/web_viewer/app.py`), Python puts the
# script's directory on sys.path, not the repo root — import modules.* fails
# unless we prepend the project root first.
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import (  # noqa: F401  kept for modules.web_viewer.app.disconnect and .emit
    SocketIO,
    disconnect,
    emit,
)

from modules import flood_scope, region_warning
from modules.channel_occupancy import (
    ChannelOccupancy,
    render_key_value,
)
from modules.database_restore import (
    DEFAULT_MAX_RESTORE_BYTES,
    DatabaseRestoreError,
    stage_database_restore,
)
from modules.db_retention import (  # noqa: F401  kept for modules.web_viewer.app.<name>
    delete_timestamp_rows_in_chunks,
    retention_delete_settings,
)
from modules.ini_writer import IniValueError, update_ini_values
from modules.io_health import IOHealthMonitor
from modules.maintenance import MaintenanceRunner
from modules.models import CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD, channel_body_limit
from modules.scheduled_message_admin import (
    SECTION as SCHEDULED_MESSAGES_SECTION,
)
from modules.scheduled_message_admin import (
    compose_value,
    describe_schedule,
    read_entries,
    validate_entry,
)
from modules.security_utils import (
    sanitize_name,
    validate_external_url,
    validate_pubkey_format,
    validate_sql_identifier,  # noqa: F401  kept for modules.web_viewer.app.validate_sql_identifier
)
from modules.settings_schema import (
    build_plugin_settings_view,
    field_id,
    to_config_string,
    validate_field,
)
from modules.settings_store import get_settings_store
from modules.template_reference import render_preview
from modules.version_info import resolve_application_version
from modules.web_viewer.dashboard_stats import (
    SERIES_METRICS,
    TOP_KINDS,
    DashboardStatsService,  # noqa: F401  kept for modules.web_viewer.app.DashboardStatsService
    humanize_span,  # noqa: F401  kept for modules.web_viewer.app.humanize_span
)

# RFC 8594 Sunset date advertised on the deprecated /api/stats endpoint.
STATS_ENDPOINT_SUNSET = "Fri, 01 Jan 2027 00:00:00 GMT"

# The bot rewrites system_health every 30 s; four missed beats means it has stopped.
BOT_HEARTBEAT_STALE_S = 120

# Packet lookup by hash (Real-time rows whose packet the page never loaded). A
# message is logged just after the packet that carried it and a command a little
# later still, so the window reaches back further than it reaches forward.
PACKET_HASH_RE = re.compile(r"[0-9A-Fa-f]{8,64}")
PACKET_LOOKUP_BEFORE_S = 120
PACKET_LOOKUP_AFTER_S = 30
PACKET_LOOKUP_LIMIT = 50


def _validate_dynamic_key(key: str) -> "str | None":
    """Validate a dynamic-section row key. Returns an error message or None.

    Keys become INI option names, so they must not contain the separators or
    comment markers that would corrupt the file on the next read.
    """
    if any(ch in key for ch in ('=', ':', '\n', '\r', '[', ']')):
        return f'Invalid key "{key}": cannot contain = : [ ] or newlines'
    if key[:1] in ('#', ';'):
        return f'Invalid key "{key}": cannot start with # or ;'
    return None


def _apply_werkzeug_websocket_fix() -> None:
    """Patch SimpleWebSocketWSGI to call start_response after WebSocket teardown.

    python-engineio's SimpleWebSocketWSGI.__call__ handles the WebSocket
    session directly on the raw socket and returns [] without ever calling
    start_response.  Werkzeug then calls write(b"") to flush an empty body,
    which triggers ``AssertionError: write() before start_response``.

    The fix calls start_response after the handler returns so that status_set
    is not None when write(b"") runs.  The subsequent attempt to write HTTP
    headers to the already-closed socket raises BrokenPipeError, which Werkzeug
    classifies as a dropped connection and silently ignores.
    """
    try:
        from engineio.async_drivers import _websocket_wsgi  # noqa: PLC0415
        _orig_call = _websocket_wsgi.SimpleWebSocketWSGI.__call__

        def _patched_call(self, environ, start_response):  # noqa: ANN001
            result = _orig_call(self, environ, start_response)
            try:
                start_response('200 OK', [('Content-Length', '0')])
            except Exception:  # noqa: BLE001
                pass
            return result

        _websocket_wsgi.SimpleWebSocketWSGI.__call__ = _patched_call
    except (ImportError, AttributeError):
        pass


_apply_werkzeug_websocket_fix()

from modules.config_snapshot import config_to_redacted_sections

# Imported for the feed helpers that moved to modules.web_viewer.feeds; kept so
# modules.web_viewer.app.<name> (and patches of it) keep resolving.
from modules.feed_filter_eval import (  # noqa: F401
    get_nested_value,
    item_passes_filter_config,  # noqa: F401
    parse_microsoft_date,
)
from modules.feed_format import format_feed_message, sort_feed_items  # noqa: F401
from modules.feed_manager import (  # noqa: F401
    DEFAULT_MAX_FEED_RESPONSE_BYTES,
    DEFAULT_MAX_PARSED_FEED_ITEMS,
    _useful_feed_content_type,  # noqa: F401
)
from modules.feed_parse import (  # noqa: F401
    api_item_fields,
    feed_allow_private_urls,
    feed_max_parsed_items,
    feed_max_response_bytes,
    rss_entry_published,
)
from modules.repeater_manager import RepeaterManager, validate_repeater_tables
from modules.security_utils import SafeUrlPolicy, create_safe_requests_session, safe_requests_request  # noqa: F401
from modules.utils import resolve_path
from modules.web_viewer.channels import ChannelAdminMixin
from modules.web_viewer.cleanup import CleanupSchedulerMixin
from modules.web_viewer.config_panels import CONFIG_PANELS, PANEL_CATEGORIES
from modules.web_viewer.dashboard import DashboardSnapshotMixin
from modules.web_viewer.database_admin import DatabaseAdminMixin
from modules.web_viewer.feeds import (  # noqa: F401  _read_limited_requests_response and _validate_feed_interval re-exported
    FeedSubscriptionsMixin,
    _read_limited_requests_response,
    _validate_feed_interval,
)
from modules.web_viewer.integration import normalized_web_viewer_password
from modules.web_viewer.live_stream import (  # noqa: F401  _ANSI_ESCAPE_RE re-exported
    _ANSI_ESCAPE_RE,
    LiveStreamMixin,
    _strip_ansi_codes,
)
from modules.web_viewer.mesh_evidence import (  # noqa: F401  NeighborEvidenceKeys re-exported
    MeshEvidenceMixin,
    NeighborEvidenceKeys,
)
from modules.web_viewer.multibyte_rollout import MultibyteRolloutMixin
from modules.web_viewer.socket_clients import SocketClientsMixin
from modules.web_viewer.tracking import ContactTrackingMixin


class BotDataViewer(DashboardSnapshotMixin, LiveStreamMixin, SocketClientsMixin, CleanupSchedulerMixin, ChannelAdminMixin, DatabaseAdminMixin, ContactTrackingMixin, MeshEvidenceMixin, MultibyteRolloutMixin, FeedSubscriptionsMixin):
    """Complete web interface using Flask-SocketIO 5.x best practices"""

    # Whitelist of allowed tables for security
    ALLOWED_TABLES = {
        'geocoding_cache',
        'generic_cache',
        'bot_metadata',
        'packet_stream',
        'message_stats',
        'command_stats',
        'greeted_users',
        'repeater_contacts',
        'complete_contact_tracking',
        'daily_stats',
        'unique_advert_packets',
        'purging_log',
        'mesh_connections',
        'observed_paths',
        'feed_subscriptions',
        'feed_activity',
        'feed_errors',
        'feed_message_queue',
        'channel_operations',
        'channels',
        'path_stats',
        'schema_version',
        'greeter_rollout',
        'bbs_messages',
        'daily_rollup',
        'dashboard_snapshot',
        'occupancy_samples',
        'occupancy_senders',
    }

    def __init__(self, db_path="meshcore_bot.db", repeater_db_path=None, config_path="config.ini"):
        # Set bot root directory (project root) for path validation
        # This is the directory containing the modules folder
        self.bot_root = Path(os.path.join(os.path.dirname(__file__), '..', '..')).resolve()
        # Resolve relative config path so viewer finds config when started as subprocess (cwd may differ)
        if not os.path.isabs(config_path):
            config_path = str(self.bot_root / config_path)

        self.config_path = config_path  # kept for config.ini write-back endpoints

        # Resolve db_path relative to the config file's directory — matches core.py's bot_root
        # property which is Path(config_file).parent.resolve().  Using self.bot_root (the project
        # code root, 2 dirs above app.py) as the base caused a mismatch when config.ini lived
        # elsewhere (e.g. a separate deployment directory), resulting in a blank realtime monitor
        # because the web viewer and bot opened different database files.
        self._config_base = Path(config_path).parent.resolve() if os.path.exists(config_path) else self.bot_root

        # Load configuration before logging so [Logging] log_file can select
        # journal/console-only vs file logging (same rules as the main bot).
        self.config = self._load_merged_config()

        self._setup_logging()

        self.app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'),
            static_url_path='/static'
        )
        import secrets as _secrets
        self.app.config['SECRET_KEY'] = _secrets.token_hex(32)
        self.app.config['SESSION_COOKIE_HTTPONLY'] = True
        self.app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
        self.app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours

        # Compress responses when the client supports it — /api/mesh/edges alone
        # is ~16 MB of JSON uncompressed (~4 MB gzipped) on a large mesh
        try:
            from flask_compress import Compress
            Compress(self.app)
        except ImportError:
            self.logger.warning(
                "flask-compress not installed; web viewer responses will be sent uncompressed"
            )

        # Flask-SocketIO configuration following 5.x best practices
        # CORS origins are configured after config is loaded; create without app for now
        self._socketio_kwargs = dict(
            max_http_buffer_size=1000000,  # 1MB buffer limit
            ping_timeout=20,               # 20 second ping timeout — 5s was too short when subscribe handlers replay DB history
            ping_interval=25,             # 25 second ping interval (Flask-SocketIO 5.x default)
            logger=False,                  # Disable verbose logging
            engineio_logger=False,        # Disable EngineIO logging
            async_mode='threading',       # Use threading for better stability
        )
        self.socketio = SocketIO()

        self.repeater_db_path = repeater_db_path

        # Connection management using Flask-SocketIO built-ins
        self.connected_clients = {}  # Track client metadata
        self._clients_lock = threading.Lock()  # Thread safety for connected_clients
        self.max_clients = 10

        # SQLite pragma handling (including the once-per-section WAL setup)
        # lives in DBManager; see _configure_db_connection.

        # The contacts list needs all-time multibyte hop-prefix evidence for its
        # capability badge.  Cache that derived set between requests and invalidate
        # it when the underlying multibyte-path population changes.
        # Keyed by the recent_days window (None = all time) so the contacts list
        # and the dashboard's 7-day view do not evict each other.
        self._contacts_badge_cache_lock = threading.Lock()
        self._contacts_badge_cache: dict[int | None, tuple[tuple, set[str]]] = {}

        # The multi-byte mesh endpoint derives lifetime edge identity from every
        # retained multi-byte observed path.  Cache that expensive aggregate and
        # ensure concurrent requests share one computation.  View-specific day
        # and observation filters remain cheap and are applied to the cached
        # lifetime result.
        try:
            mesh_cache_seconds = self.config.getint(
                'Web_Viewer',
                'mesh_graph_cache_seconds',
                fallback=30,
            )
        except (configparser.Error, ValueError, TypeError):
            mesh_cache_seconds = 30
        self._mesh_graph_cache_seconds = max(5, min(mesh_cache_seconds, 300))
        self._multibyte_graph_cache_condition = threading.Condition()
        self._multibyte_graph_cache_edges: list[dict[str, Any]] | None = None
        self._multibyte_graph_cache_created_at = 0.0
        self._multibyte_graph_cache_computing = False
        self._multibyte_graph_cache_failure_at = 0.0
        self._multibyte_graph_cache_failure: tuple[str, str] | None = None
        self._multibyte_graph_cache_retry_seconds = 5.0

        # Use [Bot] db_path when [Web_Viewer] db_path is unset
        bot_db = self.config.get('Bot', 'db_path', fallback='meshcore_bot.db')
        if (self.config.has_section('Web_Viewer') and self.config.has_option('Web_Viewer', 'db_path')
                and self.config.get('Web_Viewer', 'db_path', fallback='').strip()):
            use_db = self.config.get('Web_Viewer', 'db_path').strip()
        else:
            use_db = bot_db
        self.db_path = str(resolve_path(use_db, self._config_base))
        self.logger.info(f"Using database: {self.db_path}")

        # Optional password authentication for web viewer (BUG-001)
        self.web_viewer_password = normalized_web_viewer_password(self.config)
        if self.web_viewer_password:
            self.logger.info("Web viewer authentication enabled")
        else:
            self.logger.warning(
                "Web viewer has NO authentication. Set web_viewer_password in [Web_Viewer] config "
                "or restrict access with host = 127.0.0.1 and firewall rules."
            )

        # Optional feature page toggle (off by default)
        try:
            self.multibyte_monitor_enabled = self.config.getboolean(
                'Web_Viewer', 'multibyte_monitor_enabled', fallback=False
            )
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError, TypeError):
            self.multibyte_monitor_enabled = False

        self._init_dashboard_service()

        # Channel occupancy reader.  The windows are computed in the bot process and
        # reach the viewer through what the writer stored, so this instance carries the
        # configuration the reader needs — bucket size, quantiles, staleness threshold —
        # and recomputes nothing.  A reader that recomputed the windows by scanning
        # packet_stream would report a different number than the writer, because the
        # writer's window is a rolling monotonic window, not a table scan.
        self.occupancy = ChannelOccupancy(self.config, self.logger)

        # Configure CORS for SocketIO — default to same-origin (no cross-origin)
        cors_raw = self.config.get('Web_Viewer', 'cors_allowed_origins', fallback='').strip()
        if cors_raw:
            cors_origins = cors_raw if cors_raw == '*' else [o.strip() for o in cors_raw.split(',') if o.strip()]
            self._socketio_kwargs['cors_allowed_origins'] = cors_origins
        # Initialize SocketIO with Flask app now that config is loaded
        self.socketio.init_app(self.app, **self._socketio_kwargs)

        # Version info for footer (tag or branch/commit/date); computed once at startup
        self._version_info = self._get_version_info()

        # Setup template context processor for global template variables
        self._setup_template_context()

        # Initialize databases
        self._init_databases()

        # Setup routes and SocketIO handlers
        self._setup_routes()
        self._setup_socketio_handlers()

        # Start database polling for real-time data
        self._start_database_polling()

        # Start log file tailing for /logs page
        self._start_log_tailing()

        # Start periodic cleanup
        self._start_cleanup_scheduler()

        # Start the dashboard snapshot refresher (moves the landing page's
        # aggregate queries off the request path)
        self._start_dashboard_refresher()

        self.logger.info("BotDataViewer initialized with Flask-SocketIO 5.x best practices")

    def _setup_logging(self):
        """Setup logging; file handler only when [Logging] log_file is set.

        Empty log_file (or missing [Logging] section) means console/journal only,
        matching the main bot. When a log file is configured, viewer logs go next
        to it as web_viewer.log (e.g. /var/log/meshcore-bot/web_viewer.log).
        """
        from logging.handlers import RotatingFileHandler

        log_file = ''
        log_max_bytes = 5 * 1024 * 1024
        log_backup_count = 3
        if getattr(self, 'config', None) is not None and self.config.has_section('Logging'):
            log_file = self.config.get('Logging', 'log_file', fallback='').strip()
            try:
                log_max_bytes = self.config.getint('Logging', 'log_max_bytes', fallback=log_max_bytes)
            except (configparser.Error, ValueError, TypeError):
                pass
            try:
                log_backup_count = self.config.getint('Logging', 'log_backup_count', fallback=log_backup_count)
            except (configparser.Error, ValueError, TypeError):
                pass

        log_level_name = 'INFO'
        if getattr(self, 'config', None) is not None and self.config.has_section('Logging'):
            log_level_name = self.config.get(
                'Logging',
                'log_level',
                fallback='INFO',
            ).strip().upper()
        log_level = getattr(logging, log_level_name, logging.INFO)
        if not isinstance(log_level, int):
            log_level = logging.INFO

        # Get or create logger (don't use basicConfig as it may conflict with existing logging)
        self.logger = logging.getLogger('modern_web_viewer')
        self.logger.setLevel(log_level)

        # Remove existing handlers to avoid duplicates
        self.logger.handlers.clear()

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # Console handler (captured by journald under systemd)
        console_handler = logging.StreamHandler()
        # Keep DEBUG out of journald even when explicitly enabled for the
        # rotating diagnostic file, avoiding duplicate high-volume SD writes.
        console_handler.setLevel(max(log_level, logging.INFO))
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        # Prevent propagation to root logger to avoid duplicate messages
        self.logger.propagate = False

        if not log_file:
            self.logger.info("No log file specified, using console/journal logging only")
            return

        # Place viewer log beside the bot log (same directory as log_file)
        bot_log_path = Path(resolve_path(log_file, self._config_base))
        viewer_log_path = bot_log_path.parent / 'web_viewer.log'
        try:
            viewer_log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(viewer_log_path),
                maxBytes=log_max_bytes,
                backupCount=log_backup_count,
                encoding='utf-8',
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
            self.logger.info(
                "Web viewer logging initialized (file=%s, max=%s bytes, backups=%s)",
                viewer_log_path,
                log_max_bytes,
                log_backup_count,
            )
        except (OSError, PermissionError) as e:
            self.logger.warning(
                "Could not open web viewer log file %s: %s. Using console/journal only.",
                viewer_log_path,
                e,
            )

    def _load_config(self, config_path):
        """Load configuration from file"""
        config = configparser.ConfigParser()
        if os.path.exists(config_path):
            config.read(config_path)
        return config

    def _load_merged_config(self):
        """Load base config.ini plus its local overlay, mirroring core.py.

        core.py's ``_read_config_snapshot`` reads the base ``config.ini``,
        looks up ``[Bot] local_dir_path`` (fallback ``"local"``), resolves it
        relative to the bot root, and — if ``<local_dir_path>/config.ini``
        exists — reads it into the *same* parser so it overlays the base
        values section-by-section/key-by-key. The web viewer needs the same
        merged view so settings edited via the local overlay show up here.
        """
        base_parser = configparser.ConfigParser()
        if os.path.exists(self.config_path):
            base_parser.read(self.config_path, encoding="utf-8")
        base_sections = set(base_parser.sections())

        local_dir_path_str = base_parser.get("Bot", "local_dir_path", fallback="local")
        self.local_dir = Path(resolve_path(local_dir_path_str, self._config_base))
        self.local_config_path = str(self.local_dir / "config.ini")

        local_only = configparser.ConfigParser()
        if os.path.exists(self.local_config_path):
            local_only.read(self.local_config_path, encoding="utf-8")
        local_sections = set(local_only.sections())

        if os.path.exists(self.local_config_path):
            base_parser.read(self.local_config_path, encoding="utf-8")

        self._base_sections = base_sections
        self._local_sections = local_sections
        return base_parser

    def _get_version_info(self) -> dict[str, str | None]:
        """Get version info for footer via centralized version resolver. Never raises."""
        info = resolve_application_version()
        display = info.get("display")
        return {
            # 'display' is what the footer renders — same value !version reports,
            # so the two can't drift apart on dev or detached-tag checkouts. The
            # "unknown" sentinel is dropped so the footer omits the version
            # rather than advertising that we couldn't work it out.
            "display": None if display == "unknown" else display,
            "tag": info.get("tag"),
            "branch": info.get("branch"),
            "commit": info.get("commit"),
            "date": info.get("date"),
        }

    def _setup_template_context(self):
        """Setup template context processor to inject global variables"""
        version_info = self._version_info

        @self.app.context_processor
        def inject_template_vars():
            """Inject variables available to all templates. Never raises so templates always render."""
            try:
                try:
                    greeter_enabled = self.config.getboolean('Greeter_Command', 'enabled', fallback=False)
                except (configparser.NoSectionError, configparser.NoOptionError, ValueError, TypeError):
                    greeter_enabled = False
                try:
                    feed_manager_enabled = self.config.getboolean('Feed_Manager', 'feed_manager_enabled', fallback=False)
                except (configparser.NoSectionError, configparser.NoOptionError, ValueError, TypeError):
                    feed_manager_enabled = False
                try:
                    bot_name = (self.config.get('Bot', 'bot_name', fallback='MeshCore Bot') or '').strip() or 'MeshCore Bot'
                except (configparser.NoSectionError, configparser.NoOptionError):
                    bot_name = 'MeshCore Bot'
                try:
                    radio_zombie = self.db_manager.get_metadata('bot.radio_zombie') == 'true'
                    radio_zombie_since = self.db_manager.get_metadata('bot.radio_zombie_since') or None
                    radio_offline = self.db_manager.get_metadata('bot.radio_offline') == 'true'
                    radio_offline_since = self.db_manager.get_metadata('bot.radio_offline_since') or None
                    bot_initializing = self.db_manager.get_metadata('bot.initializing') == 'true'
                except Exception:
                    radio_zombie = False
                    radio_zombie_since = None
                    radio_offline = False
                    radio_offline_since = None
                    bot_initializing = False
                auth_enabled = bool(self.web_viewer_password)
                # Session bit only — missing password is not an admin session.
                is_admin = bool(session.get('authenticated_admin'))
                return {
                    'greeter_enabled': greeter_enabled,
                    'feed_manager_enabled': feed_manager_enabled,
                    'multibyte_monitor_enabled': self.multibyte_monitor_enabled,
                    'bot_name': bot_name,
                    'version_info': version_info,
                    'radio_zombie': radio_zombie,
                    'radio_zombie_since': radio_zombie_since,
                    'radio_offline': radio_offline,
                    'radio_offline_since': radio_offline_since,
                    'bot_initializing': bot_initializing,
                    'auth_enabled': auth_enabled,
                    'is_admin': is_admin,
                }
            except Exception as e:
                self.logger.exception("Template context processor failed: %s", e)
                return {
                    'greeter_enabled': False,
                    'feed_manager_enabled': False,
                    'multibyte_monitor_enabled': False,
                    'bot_name': 'MeshCore Bot',
                    'bot_initializing': False,
                    'version_info': version_info,
                    'radio_zombie': False,
                    'radio_zombie_since': None,
                    'radio_offline': False,
                    'radio_offline_since': None,
                    'auth_enabled': bool(getattr(self, 'web_viewer_password', '')),
                    'is_admin': False,
                }

    def _init_databases(self):
        """Initialize database connections"""
        try:
            # Initialize database manager for metadata access
            from modules.db_manager import DBManager
            # Create a minimal bot object for DBManager
            class MinimalBot:
                def __init__(self, logger, config, db_manager=None):
                    self.logger = logger
                    self.config = config
                    self.db_manager = db_manager

            # Create DBManager first
            minimal_bot = MinimalBot(self.logger, self.config)
            self.db_manager = DBManager(minimal_bot, self.db_path)

            # Now set db_manager on the minimal bot for RepeaterManager
            minimal_bot.db_manager = self.db_manager

            # The viewer runs as a separate process, so it cannot call the bot's
            # MessageScheduler directly. MaintenanceRunner only needs this small
            # bot facade for manual database backups.
            self._maintenance_runner = MaintenanceRunner(
                minimal_bot,
                get_current_time=datetime.now,
            )

            # The viewer only needs RepeaterManager for the manual geocode
            # endpoint, so defer its setup until that endpoint is actually used.
            self._repeater_manager_bot = minimal_bot
            self._repeater_manager_lock = threading.Lock()
            self.repeater_manager: RepeaterManager | None = None

            # RepeaterManager's constructor is what used to validate these at
            # startup. It is lazy now, so validate here: a missing migration is
            # a startup failure, not a mystery 500 from the geocode endpoint.
            validate_repeater_tables(self.db_manager, self.logger)

            # MeshGraph is only read by the two path-decoding routes, so build it
            # on first use rather than at startup. It is NOT optional for them:
            # without it decode_path_nodes() falls back to geographic-only
            # selection and disagrees with the bot's `path` command.
            self._mesh_graph_bot = minimal_bot
            self._mesh_graph_lock = threading.Lock()
            self.mesh_graph = None

            # Store database paths for direct connection
            self.db_path = self.db_path
            self.repeater_db_path = self.repeater_db_path
            self.logger.info("Database connections initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize databases: {e}")
            raise

    def _get_repeater_manager(self) -> RepeaterManager:
        """Lazily construct the manual-geocoding helper once."""
        if self.repeater_manager is not None:
            return self.repeater_manager
        with self._repeater_manager_lock:
            if self.repeater_manager is None:
                self.repeater_manager = RepeaterManager(self._repeater_manager_bot)
            return self.repeater_manager

    def _get_mesh_graph(self):
        """Lazily load the read-only mesh graph used to disambiguate path prefixes.

        Returns None only if the graph cannot be loaded, in which case path
        decoding degrades to geographic-only selection rather than failing the
        request. Loaded with capture disabled: the bot process owns edge
        capture, so the viewer must not write edges or run a batch writer.
        """
        if self.mesh_graph is not None:
            return self.mesh_graph
        with self._mesh_graph_lock:
            if self.mesh_graph is None:
                from modules.mesh_graph import MeshGraph
                try:
                    graph = MeshGraph(self._mesh_graph_bot, capture=False)
                except Exception as e:
                    self.logger.error(
                        f"Mesh graph unavailable; path decoding will fall back to "
                        f"geographic selection only: {e}"
                    )
                    return None
                self._mesh_graph_bot.mesh_graph = graph
                self.mesh_graph = graph
            return self.mesh_graph

    def _configure_db_connection(self, conn: sqlite3.Connection) -> None:
        """Apply SQLite pragmas to a viewer connection.

        Delegates to DBManager, which owns the single implementation and the
        once-per-section WAL bookkeeping. Safe because this DBManager was built
        for self.db_path, which is the file every viewer connection opens — the
        journal-mode state it tracks belongs to that same database.
        """
        self.db_manager._apply_sqlite_pragmas(conn, for_web_viewer=True)

    def _get_db_connection(self):
        """Get database connection - create new connection for each request to avoid threading issues"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=60)
            conn.row_factory = sqlite3.Row
            self._configure_db_connection(conn)
            return conn
        except Exception as e:
            self.logger.error(f"Failed to create database connection: {e}")
            raise

    def _read_meta_settings(
        self, prefix: str, fields: list[str], defaults: dict[str, str]
    ) -> dict[str, str]:
        """Read ``<prefix>.<field>`` values from bot_metadata, unset ones as ``''``.

        A field that is unset or empty takes its value from ``defaults``.
        """
        settings: dict[str, str] = {}
        for field in fields:
            val = self.db_manager.get_metadata(f'{prefix}.{field}')
            settings[field] = val if val is not None else ''
        for field, default in defaults.items():
            if not settings.get(field):
                settings[field] = default
        return settings

    def _write_meta_settings(self, prefix: str, allowed: Any, data: dict[str, Any]) -> list[str]:
        """Store each allowed field present in ``data`` as ``<prefix>.<field>``; return those saved."""
        saved = []
        for field in allowed:
            if field in data:
                self.db_manager.set_metadata(f'{prefix}.{field}', str(data[field]))
                saved.append(field)
        return saved

    def _api_errors(self, message: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Route decorator: log ``"<message>: <error>"`` and answer 500 with the error text.

        The response body is later rewritten to a generic message by
        set_security_headers, as for every 5xx JSON error.
        """
        def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(view)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return view(*args, **kwargs)
                except Exception as e:
                    self.logger.error(f"{message}: {e}")
                    return jsonify({'error': str(e)}), 500
            return wrapper
        return decorate

    def _queue_operation(self, operation_type: str, payload: Any = None) -> int | None:
        """Queue a bot-side operation in ``channel_operations`` and return its row id.

        The bot's scheduler polls this table; callers poll
        /api/channel-operations/<id> for the result. ``payload`` is stored as JSON
        in ``payload_data`` when given. Errors propagate to the caller.
        """
        with self.db_manager.connection() as conn:
            cursor = conn.cursor()
            if payload is None:
                cursor.execute(
                    "INSERT INTO channel_operations (operation_type, status) VALUES (?, 'pending')",
                    (operation_type,),
                )
            else:
                cursor.execute(
                    "INSERT INTO channel_operations (operation_type, payload_data, status) "
                    "VALUES (?, ?, 'pending')",
                    (operation_type, json.dumps(payload)),
                )
            conn.commit()
            return cursor.lastrowid

    @contextmanager
    def _db_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield ``_get_db_connection()`` and close it on exit.

        Unlike ``_with_db_connection``, this opens through ``_get_db_connection``,
        so it logs connect failures and honors tests that patch that method.
        """
        conn = self._get_db_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _with_db_connection(self):
        """Context manager that yields a configured connection and closes it on exit.
        Use this instead of _get_db_connection() in with-statements to avoid leaking file descriptors.
        """
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        self._configure_db_connection(conn)
        try:
            yield conn
        finally:
            conn.close()


    # Nodes in the neighbor tables are stored as full 32-byte public keys, so the
    # graph's highest resolution (3 bytes) is always available for edge identity.
    NEIGHBOR_PREFIX_HEX_CHARS = 6


    def _resolve_path(self, path_input: str) -> dict[str, Any]:
        """Resolve a hex path to repeater names/locations for the mesh map.

        Thin wrapper over the shared engine (modules.path_inference.decode_path_nodes). Previously
        this duplicated the decode logic and crashed ("must be real number, not NoneType") on
        repeaters without coordinates; the shared engine guards that.
        """
        if not hasattr(self, "db_manager") or not self.db_manager:
            return {"node_ids": [], "repeaters": [], "valid": False,
                    "error": "Database manager not initialized"}
        from modules.path_inference import decode_path_nodes
        nodes = decode_path_nodes(
            path_input,
            None,
            config=self.config,
            db_manager=self.db_manager,
            logger=self.logger,
            mesh_graph=self._get_mesh_graph(),
            include_location=True,
        )
        node_ids = [n["node_id"] for n in nodes]
        if not node_ids:
            return {"node_ids": [], "repeaters": [], "valid": False,
                    "error": "No valid hex values found"}
        return {"node_ids": node_ids, "repeaters": nodes, "valid": True}

    def _setup_routes(self):
        """Setup all Flask routes - complete feature parity"""
        # Log full traceback for 500 errors so service logs show the real cause
        @self.app.errorhandler(500)
        def internal_error(e):
            self.logger.exception("Unhandled exception (500): %s", e)
            if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
                return make_response(jsonify({'error': 'An internal error occurred — see server logs'}), 500)
            return make_response(render_template('error.html',
                error_code=500,
                error_title='Internal Server Error',
                error_message='Something went wrong on our end. The error has been logged.',
            ), 500)

        # Authentication middleware (BUG-001).
        # Fail closed when a password is configured: only the public allowlist
        # below is reachable without authenticated_admin. Everything else
        # (config, logs, radio, mutations, channel keys, sockets) stays admin.
        _EXEMPT_PATHS = frozenset([
            '/login', '/logout',
            '/apple-touch-icon.png', '/favicon-32x32.png', '/favicon-16x16.png',
            '/site.webmanifest', '/favicon.ico',
            # Bot→viewer ingest uses X-Stream-Token, not the admin session.
            '/api/stream_data',
        ])

        # Issue #240 public HTML surface. Realtime is admin-only: its live socket
        # is, so an anonymous visit would only ever show an empty page.
        _PUBLIC_PAGE_PATHS = frozenset([
            '/', '/contacts', '/mesh',
        ])

        # Anonymous-safe GET APIs: mesh-visible / aggregate data only. No channel
        # keys, config, logs, backups, or private message firehose.
        _PUBLIC_API_GET_PATHS = frozenset([
            '/api/health',
            # Storage monitoring probe. Read by an external agent, so it is public; the
            # payload carries aggregate counters only, never mount paths.
            '/api/health/storage',
            '/api/banner-status',
            '/api/stats',
            '/api/dashboard/summary',
            '/api/dashboard/series',
            '/api/dashboard/top',
            '/api/dashboard/windows',
            # Channel occupancy probes. Read by an external agent, so they are public;
            # the payload carries aggregate airtime counters and sender keys already
            # visible on /api/contacts, nothing private.
            '/api/telemetry/occupancy',
            '/api/telemetry/occupancy/history',
            '/api/contacts',
            '/api/contact-detail',
            '/api/mesh/nodes',
            '/api/mesh/edges',
            '/api/mesh/stats',
        ])

        # Read-only POST helpers used by public Contacts / Mesh info panels.
        _PUBLIC_API_POST_PATHS = frozenset([
            '/api/decode-path',
            '/api/mesh/resolve-path',
        ])

        def _is_local_redirect(url: str) -> bool:
            # Browsers read '//host' and '/\host' as another origin, and strip
            # tabs/newlines before parsing, so '/\t/host' becomes '//host' too.
            if not url.startswith('/') or any(c in url for c in '\\\t\r\n'):
                return False
            if url.startswith('//'):
                return False
            parsed = urlparse(url)
            return not (parsed.scheme or parsed.netloc)

        def _normalize_request_path(path: str) -> str:
            if path != '/' and path.endswith('/'):
                path = path.rstrip('/')
            return path or '/'

        @self.app.before_request
        def create_csp_nonce():
            """Create a per-response nonce for templates migrated off inline-script CSP."""
            g.csp_nonce = secrets.token_urlsafe(24)

        @self.app.before_request
        def require_auth():
            """Enforce admin auth except for the explicit public allowlist."""
            if not self.web_viewer_password:
                return  # Auth disabled — no password configured (legacy open mode)
            path = _normalize_request_path(request.path)
            if path in _EXEMPT_PATHS or path.startswith('/static/'):
                return
            if session.get('authenticated_admin'):
                return
            # HEAD and OPTIONS are answered by Flask from the GET route with no
            # body, so they are as safe as the GET they shadow.
            if request.method in ('GET', 'HEAD', 'OPTIONS') and (
                path in _PUBLIC_PAGE_PATHS or path in _PUBLIC_API_GET_PATHS
            ):
                return
            if request.method in ('POST', 'OPTIONS') and path in _PUBLIC_API_POST_PATHS:
                return
            if path.startswith('/api/'):
                return make_response(jsonify({'error': 'Admin authentication required'}), 401)
            return redirect(url_for('login', next=path))

        @self.app.before_request
        def csrf_protection():
            """Reject cross-origin state-changing requests.

            For API endpoints (JSON), require the X-Requested-With header.
            Browsers block cross-origin custom headers without a CORS preflight,
            and our CORS policy restricts allowed origins — so the presence of
            this header proves the request is same-origin or from an allowed origin.
            Form-based POST to /login is exempt (uses session cookie + redirect).
            """
            if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
                return
            if current_app.config.get('TESTING'):
                return  # Skip CSRF in test mode
            if request.path == '/login':
                return  # Login form uses traditional POST
            if request.headers.get('X-Requested-With'):
                return  # Custom header present — same-origin or CORS-approved
            if request.path.startswith('/api/'):
                return make_response(
                    jsonify({'error': 'Missing X-Requested-With header'}), 403
                )

        @self.app.after_request
        def set_security_headers(response):
            # Security headers
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
            # Every script, stylesheet and font is served from static/ (see
            # static/vendor/README.md); the only external hosts are the map tiles.
            # The highest-risk admin screens have migrated their inline handlers
            # and authorize their remaining template scripts with a per-request
            # nonce. Other legacy screens retain unsafe-inline until their inline
            # scripts/handlers are migrated, rather than silently breaking them.
            nonce_hardened_endpoints = {
                'index',
                'feeds',
                'config_page',
                'radio',
                'realtime',
                'contacts',
                'plugins_page',
                'greeter',
                'region_warnings_page',
                'regions_page',
                'logs',
                'multibyte_rollout',
                'mesh',
                'api_explorer',
            }
            if request.endpoint in nonce_hardened_endpoints:
                script_source = f"script-src 'self' 'nonce-{g.csp_nonce}'; "
            else:
                script_source = "script-src 'self' 'unsafe-inline'; "
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                + script_source
                + "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https://*.tile.openstreetmap.org "
                "https://tiles.openfreemap.org; "
                "connect-src 'self' ws: wss: https://tiles.openfreemap.org; "
                # MapLibre GL runs its renderer in a worker spawned from a blob: URL.
                # Without these it falls back to default-src 'self' and the dark
                # basemap fails to start.
                "worker-src 'self' blob:; "
                "child-src 'self' blob:; "
                "font-src 'self'"
            )

            # Sanitize error details from 5xx JSON responses to prevent info disclosure.
            # The full exception is already logged server-side; clients only need a
            # generic message.  Preserve 'error' key presence so callers can detect
            # failure, but strip internal details (file paths, DB errors, etc.).
            if response.status_code >= 500 and response.content_type and 'json' in response.content_type:
                try:
                    data = response.get_json(silent=True)
                    if data and isinstance(data, dict) and 'error' in data:
                        data['error'] = 'An internal error occurred'
                        data.pop('traceback', None)
                        response.set_data(json.dumps(data))
                except Exception:
                    pass  # Don't break the response if sanitization fails

            return response

        @self.app.route('/login', methods=['GET', 'POST'])
        def login():
            """Login page for admin authentication"""
            if not self.web_viewer_password:
                return redirect(url_for('index'))
            if request.method == 'POST':
                password = request.form.get('password', '')
                # Hash both sides so compare_digest always sees equal-length
                # digests (avoids TypeError / length short-circuit on ==).
                expected = hmac.new(
                    b'web-viewer-login',
                    self.web_viewer_password.encode('utf-8'),
                    'sha256',
                ).digest()
                provided = hmac.new(
                    b'web-viewer-login',
                    password.encode('utf-8'),
                    'sha256',
                ).digest()
                if hmac.compare_digest(provided, expected):
                    session.clear()
                    session['authenticated_admin'] = True
                    # Ties this login's Socket.IO connections together so logout
                    # can drop them (a socket keeps its connect-time session).
                    session['admin_login_id'] = secrets.token_urlsafe(16)
                    next_url = request.args.get('next', '/')
                    if not _is_local_redirect(next_url):
                        next_url = '/'
                    return redirect(next_url)
                return render_template('login.html', error='Invalid password')
            return render_template('login.html')

        @self.app.route('/logout')
        def logout():
            """Logout, clear session, and drop this login's live sockets"""
            login_id = session.get('admin_login_id')
            session.clear()
            if login_id:
                self._disconnect_login_sockets(login_id)
            return redirect(url_for('index'))

        @self.app.route('/')
        def index():
            """Main dashboard.

            Server-side config reaches the page as a JSON data block rather than
            an inline script, so the dashboard's JavaScript can live in a static
            file that ``script-src 'self'`` already covers.
            """
            database_size = None
            with suppress(OSError):
                database_size = os.path.getsize(self.db_path)
            return render_template(
                'index.html',
                dashboard_boot={
                    'database_size': database_size,
                    'snapshot_interval_seconds': self.dashboard_snapshot_interval,
                    'snapshot_enabled': self.dashboard_snapshot_enabled,
                },
            )

        @self.app.route('/realtime')
        def realtime():
            """Real-time monitoring dashboard"""
            return render_template('realtime.html')

        @self.app.route('/logs')
        def logs():
            """Live log viewer"""
            return render_template('logs.html')

        @self.app.route('/contacts')
        def contacts():
            """Contacts page - unified contact management and tracking"""
            return render_template('contacts.html')

        @self.app.route('/cache')
        def cache():
            """Legacy cache URL redirects to the database config panel."""
            return redirect('/config#database')


        @self.app.route('/multibyte-rollout')
        def multibyte_rollout():
            """Multibyte hash rollout analytics page"""
            if not self.multibyte_monitor_enabled:
                return abort(404)
            return render_template('multibyte_rollout.html')

        @self.app.route('/greeter')
        def greeter():
            """Greeter management page"""
            return render_template('greeter.html')

        @self.app.route('/region-warnings')
        def region_warnings_page():
            """Regional flood scope monitoring and warning settings."""
            return render_template('region_warnings.html')

        @self.app.route('/feeds')
        def feeds():
            """Feed management page"""
            return render_template('feeds.html')

        @self.app.route('/schedule')
        def schedule_page():
            """Scheduled message management page"""
            return render_template('schedule.html')

        @self.app.route('/radio')
        def radio():
            """Radio settings page.

            Passes config-governance flags so the Node Settings card doesn't
            offer device settings the bot itself manages from config.ini.
            """
            auto_manage = 'false'
            bot_name = ''
            name_managed = False
            if self.config:
                auto_manage = self.config.get('Bot', 'auto_manage_contacts', fallback='device').lower()
                bot_name = (self.config.get('Bot', 'bot_name', fallback='') or '').strip()
                try:
                    auto_update_name = self.config.getboolean('Bot', 'auto_update_device_name', fallback=True)
                except ValueError:
                    auto_update_name = True
                name_managed = bool(bot_name) and auto_update_name
            return render_template(
                'radio.html',
                auto_manage_contacts=auto_manage,
                device_name_managed=name_managed,
                bot_name=bot_name,
            )

        @self.app.route('/config')
        def config_page():
            """Bot configuration page"""
            return render_template(
                'config.html',
                config_panels=sorted(CONFIG_PANELS, key=lambda panel: panel['order']),
                panel_categories=PANEL_CATEGORIES,
            )

        # ── Plugins settings panel ───────────────────────────────────────────

        @self.app.route('/plugins')
        def plugins_page():
            """Plugin & command settings page."""
            return render_template('plugins.html')

        @self.app.route('/api/plugins')
        def api_plugins_get():
            """Return the settings view for every discovered command/service."""
            try:
                # Re-read config from disk so the UI reflects external edits.
                self.config = self._load_merged_config()
                view = build_plugin_settings_view(
                    self.config,
                    logger=self.logger,
                    local_commands_dir=str(self.local_dir / "commands"),
                    local_services_dir=str(self.local_dir / "service_plugins"),
                )
                return jsonify({'plugins': view})
            except Exception:
                self.logger.exception("Error building plugin settings view")
                return jsonify({'error': 'Internal error — see server logs'}), 500

        @self.app.route('/api/plugins/<kind>/<name>', methods=['POST'])
        def api_plugins_save(kind: str, name: str):
            """Validate and persist one plugin's settings, then queue a reload.

            Body: ``{"section": str, "enabled": bool, "values": {key: raw}}``.
            Validation mirrors the plugin's ``settings_schema`` server-side.
            """
            try:
                data = request.get_json(silent=True) or {}
                # Locate the plugin entry so we have its schema + section.
                self.config = self._load_merged_config()
                view = build_plugin_settings_view(
                    self.config,
                    logger=self.logger,
                    local_commands_dir=str(self.local_dir / "commands"),
                    local_services_dir=str(self.local_dir / "service_plugins"),
                )
                entry = next(
                    (e for e in view if e['kind'] == kind and e['name'] == name),
                    None,
                )
                if entry is None:
                    return jsonify({'success': False, 'error': 'Unknown plugin'}), 404

                section = entry['section']
                # Fields are addressed by field_id (key, or key@Section for a shared
                # section). A bare key still resolves when only one field has it, so
                # scripted saves written before field ids keep working.
                schema_by_id = {field_id(f): f for f in entry['fields']}
                key_counts: dict[str, int] = {}
                for f in entry['fields']:
                    key_counts[f['key']] = key_counts.get(f['key'], 0) + 1
                for f in entry['fields']:
                    if key_counts[f['key']] == 1:
                        schema_by_id.setdefault(f['key'], f)
                raw_values = data.get('values', {}) or {}

                errors: dict[str, str] = {}
                # Schema-typed keys are validated and routed to their (possibly
                # shared) target section; any other submitted key is written raw to
                # the plugin's own section (covers dynamic/legacy keys not in the
                # schema, so a partial schema never hides remaining settings).
                updates: dict[str, dict[str, str]] = {section: {}}
                deletes: dict[str, list[str]] = {}
                for submitted, val in raw_values.items():
                    field = schema_by_id.get(submitted)
                    if field is not None:
                        key = field['key']
                        ok, coerced, err = validate_field(field, val)
                        if not ok:
                            errors[submitted] = err
                        else:
                            tsec = field.get('section') or section
                            if field.get('type') in ('int', 'float', 'enum') and coerced == '':
                                # A cleared number, or an enum's "" (inherit) option,
                                # means unset: `key =` would make getint/getfloat
                                # raise, or read as an invalid choice.
                                deletes.setdefault(tsec, []).append(key)
                            else:
                                updates.setdefault(tsec, {})[key] = to_config_string(field, coerced)
                    else:
                        updates[section][str(submitted)] = '' if val is None else str(val)

                if errors:
                    return jsonify({'success': False, 'errors': errors}), 400

                # The enable toggle is always written to the plugin's own section.
                enabled = bool(data.get('enabled', entry['enabled']))
                updates[section]['enabled'] = 'true' if enabled else 'false'
                submitted_dyn = data.get('dynamic_sections', {}) or {}
                for ds in entry.get('dynamic_sections', []):
                    dsec = ds['section']
                    prefix = ds.get('key_prefix', '') or ''
                    if dsec not in submitted_dyn:
                        # Payload didn't include this editor's rows (partial or
                        # scripted save) — leave the managed keys untouched
                        # rather than treating absence as "delete everything".
                        continue
                    rows = submitted_dyn.get(dsec) or []
                    new_full: dict[str, str] = {}
                    seen_full: set[str] = set()
                    seen_disp: set[str] = set()
                    for row in rows:
                        rkey = (str(row.get('key', '')) or '').strip()
                        rval = row.get('value', '')
                        rval = '' if rval is None else str(rval)
                        if not rkey:
                            continue  # skip blank rows
                        key_err = _validate_dynamic_key(rkey)
                        if key_err:
                            return jsonify({'success': False, 'error': key_err}), 400
                        if rkey.lower() in seen_disp:
                            return jsonify({'success': False,
                                            'error': f'Duplicate key "{rkey}" in {ds["label"]}'}), 400
                        seen_disp.add(rkey.lower())
                        full = f"{prefix}{rkey}"
                        new_full[full] = rval
                        seen_full.add(full.lower())
                    # Merge into the target section (own section keeps schema fields).
                    updates.setdefault(dsec, {}).update(new_full)
                    # Delete existing managed keys that are no longer present.
                    existing = self.config.items(dsec, raw=True) if self.config.has_section(dsec) else []
                    pl = prefix.lower()
                    del_keys = [
                        k for k, _ in existing
                        if (not prefix or k.lower().startswith(pl)) and k.lower() not in seen_full
                    ]
                    deletes.setdefault(dsec, []).extend(del_keys)

                # Repeating structured blocks (e.g. PacketCapture mqttN_*). Blocks
                # are renumbered contiguously from 1 (the service stops scanning at
                # the first missing index), validated per sub-schema, and unknown
                # sub-keys are passed through so they survive the save.
                submitted_blocks = data.get('repeating_blocks', {}) or {}
                for rb in entry.get('repeating_blocks', []):
                    bid = rb['id']
                    if bid not in submitted_blocks:
                        # Same defensive rule as dynamic sections: absent from
                        # the payload means "don't touch", not "delete all".
                        continue
                    enabled_field = rb['enabled_field']
                    field_by_key = {f['key']: f for f in rb['fields']}
                    written: set[str] = set()
                    for i, block in enumerate(submitted_blocks.get(bid) or [], start=1):
                        bvals = block.get('values', {}) or {}
                        for k, val in bvals.items():
                            full = f"{bid}{i}_{k}"
                            field = field_by_key.get(k)
                            if field is not None:
                                ok, coerced, err = validate_field(field, val)
                                if not ok:
                                    return jsonify({'success': False,
                                                    'error': f'{rb["label"]} #{i}: {err}'}), 400
                                if field.get('type') in ('int', 'float', 'enum') and coerced == '':
                                    # Unset, as for top-level fields; leaving it out of
                                    # `written` deletes any existing key below.
                                    continue
                                updates[section][full] = to_config_string(field, coerced)
                            else:
                                updates[section][full] = '' if val is None else str(val)
                            written.add(full.lower())
                        en = f"{bid}{i}_{enabled_field}"
                        updates[section][en] = 'true' if block.get('enabled', True) else 'false'
                        written.add(en.lower())
                    # Delete any existing block keys (old higher indices / removed).
                    brx = re.compile(rf"^{re.escape(bid)}\d+_", re.IGNORECASE)
                    existing = self.config.items(section, raw=True) if self.config.has_section(section) else []
                    for k, _ in existing:
                        if brx.match(k) and k.lower() not in written:
                            deletes.setdefault(section, []).append(k)

                if section in self._local_sections:
                    target_path = self.local_config_path
                elif section in self._base_sections:
                    target_path = self.config_path
                else:
                    # Brand-new section: local commands default into the local
                    # overlay, everything else into the base config.
                    target_path = (
                        self.local_config_path if entry.get('source') == 'local' else self.config_path
                    )

                if target_path == self.local_config_path and not os.path.exists(target_path):
                    # update_ini_values() requires the target file to already
                    # exist (it reads + backs up before writing) — local/config.ini
                    # may not exist yet on a fresh install.
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with open(target_path, 'w', encoding='utf-8') as f:
                        f.write('')

                store = get_settings_store(self.config, target_path, self.db_manager)
                result = store.write_sections(updates, deletes)
                backup_path = result.get('backup_path', '') if isinstance(result, dict) else ''

                # A service's start/stop only takes effect on bot restart.
                restart_required = (kind == 'service' and enabled != entry['enabled'])

                # Queue a hot reload via the channel_operations table (the bot's
                # scheduler polls this — same pattern as radio reconnect).
                reload_queued = False
                try:
                    self._queue_operation('config_reload')
                    reload_queued = True
                except Exception:
                    self.logger.exception("Failed to queue config reload")

                self.logger.info(
                    "Plugin settings saved: %s [%s] (backup=%s)",
                    name, section, os.path.basename(backup_path) if backup_path else 'none',
                )
                return jsonify({
                    'success': True,
                    'backup_path': backup_path,
                    'reload_queued': reload_queued,
                    'restart_required': restart_required,
                })
            except IniValueError as exc:
                # A submitted key/value would corrupt the INI (newline, [ ], …).
                # Nothing was written; report it as a client error.
                return jsonify({'success': False, 'error': str(exc)}), 400
            except Exception:
                self.logger.exception("Error saving plugin settings")
                return jsonify({'success': False, 'error': 'Internal error — see server logs'}), 500

        # (kind, name, key) -> template spec. Specs are class attributes, so they
        # don't change with config and needn't be rediscovered on every keystroke.
        template_specs: dict[tuple[str, str, str], dict | None] = {}

        @self.app.route('/api/plugins/<kind>/<name>/template-preview', methods=['POST'])
        def api_plugins_template_preview(kind: str, name: str):
            """Render a piped template field against sample messages.

            Body: ``{"key": str, "template": str}``. Nothing is saved.
            """
            data = request.get_json(silent=True) or {}
            key = str(data.get('key', ''))
            template = data.get('template', '')
            if not isinstance(template, str) or len(template) > 2000:
                return jsonify({'error': 'Template must be text of at most 2000 characters'}), 400
            cache_key = (kind, name, key)
            if cache_key not in template_specs:
                view = build_plugin_settings_view(
                    self.config,
                    logger=self.logger,
                    local_commands_dir=str(self.local_dir / "commands"),
                    local_services_dir=str(self.local_dir / "service_plugins"),
                )
                for entry in view:
                    for field in entry['fields']:
                        if field.get('template'):
                            template_specs[(entry['kind'], entry['name'], field['key'])] = field['template']
                template_specs.setdefault(cache_key, None)
            spec = template_specs[cache_key]
            if not spec or not spec.get('previewable'):
                return jsonify({'error': 'This field has no preview'}), 404
            try:
                return jsonify(render_preview(spec, template, self.config))
            except Exception:
                self.logger.exception("Error rendering template preview")
                return jsonify({'error': 'Internal error — see server logs'}), 500

        @self.app.route('/api/plugins/reload-status')
        def api_plugins_reload_status():
            """Return the status of the most recent config_reload operation."""
            try:
                rows = self.db_manager.execute_query(
                    "SELECT status, result_data, processed_at FROM channel_operations "
                    "WHERE operation_type = 'config_reload' ORDER BY id DESC LIMIT 1"
                )
                if not rows:
                    return jsonify({'status': None})
                row = rows[0]
                return jsonify({
                    'status': row.get('status'),
                    'result_data': row.get('result_data'),
                    'processed_at': row.get('processed_at'),
                })
            except Exception:
                self.logger.exception("Error reading reload status")
                return jsonify({'status': None}), 500

        @self.app.route('/api/config/notifications')
        def api_config_notifications_get():
            """Return current notification settings from bot_metadata."""
            settings = self._read_meta_settings(
                'notif',
                [
                    'smtp_host', 'smtp_port', 'smtp_security',
                    'smtp_user', 'smtp_password',
                    'from_name', 'from_email',
                    'recipients', 'nightly_enabled',
                    'allow_local_smtp',
                ],
                defaults={'smtp_port': '587', 'smtp_security': 'starttls', 'nightly_enabled': 'false'},
            )
            return jsonify(settings)

        @self.app.route('/api/config/notifications', methods=['POST'])
        def api_config_notifications_post():
            """Save notification settings to bot_metadata."""
            data = request.get_json(silent=True) or {}
            allowed = {
                'smtp_host', 'smtp_port', 'smtp_security',
                'smtp_user', 'smtp_password',
                'from_name', 'from_email',
                'recipients', 'nightly_enabled', 'allow_local_smtp',
            }
            saved = self._write_meta_settings('notif', allowed, data)
            self.logger.info(f"Notification settings updated: {', '.join(saved)}")
            return jsonify({'success': True, 'saved': saved})

        @self.app.route('/api/config/notifications/test', methods=['POST'])
        def api_config_notifications_test():
            """Send a test email using the saved SMTP settings."""
            import smtplib
            import ssl as _ssl
            from email.message import EmailMessage

            def _get(key):
                return self.db_manager.get_metadata(f'notif.{key}') or ''

            smtp_host     = _get('smtp_host')
            smtp_port     = int(_get('smtp_port') or 587)
            smtp_security = _get('smtp_security') or 'starttls'
            smtp_user     = _get('smtp_user')
            smtp_password = _get('smtp_password')
            from_name     = _get('from_name') or 'MeshCore Bot'
            from_email    = _get('from_email')
            recipients    = [r.strip() for r in _get('recipients').split(',') if r.strip()]

            if not smtp_host:
                return jsonify({'error': 'SMTP host is not configured'}), 400
            if not from_email:
                return jsonify({'error': 'Sender email is not configured'}), 400
            if not recipients:
                return jsonify({'error': 'No recipients configured'}), 400

            # Validate SMTP host for SSRF protection
            # allow_local_smtp=true permits private/internal SMTP hosts (e.g., local Postfix)
            allow_local_smtp = _get('allow_local_smtp').lower() == 'true'
            if not validate_external_url(f'http://{smtp_host}', allow_private=allow_local_smtp):
                if allow_local_smtp:
                    return jsonify({'error': 'Invalid or unsafe SMTP host'}), 400
                return jsonify({'error': 'Invalid or unsafe SMTP host (private/internal IP blocked)'}), 400

            try:
                msg = EmailMessage()
                msg['Subject'] = 'MeshCore Bot — test email'
                msg['From']    = f'{from_name} <{from_email}>'
                msg['To']      = ', '.join(recipients)
                msg.set_content(
                    'This is a test email from MeshCore Bot.\n\n'
                    'If you received this, your SMTP settings are working correctly.\n'
                )

                context = _ssl.create_default_context()

                if smtp_security == 'ssl':
                    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as s:
                        if smtp_user and smtp_password:
                            s.login(smtp_user, smtp_password)
                        s.send_message(msg)
                else:
                    with smtplib.SMTP(smtp_host, smtp_port) as s:
                        if smtp_security == 'starttls':
                            s.ehlo()
                            s.starttls(context=context)
                            s.ehlo()
                        if smtp_user and smtp_password:
                            s.login(smtp_user, smtp_password)
                        s.send_message(msg)

                self.logger.info(f"Test email sent to {recipients}")
                return jsonify({'success': True, 'message': f'Test email sent to {", ".join(recipients)}'})

            except Exception as e:
                self.logger.error(f"Test email failed: {e}")
                return jsonify({'error': str(e)}), 500

        # ── Logging config ───────────────────────────────────────────────────

        @self.app.route('/api/config/logging')
        def api_config_logging_get():
            """Return log rotation settings from bot_metadata."""
            settings = self._read_meta_settings(
                'maint',
                ['log_max_bytes', 'log_backup_count'],
                defaults={'log_max_bytes': str(5 * 1024 * 1024), 'log_backup_count': '3'},
            )
            return jsonify(settings)

        @self.app.route('/api/config/logging', methods=['POST'])
        def api_config_logging_post():
            """Save log rotation settings to bot_metadata."""
            data = request.get_json(silent=True) or {}
            allowed = {'log_max_bytes', 'log_backup_count'}
            saved = self._write_meta_settings('maint', allowed, data)
            self.logger.info(f"Log rotation config updated: {', '.join(saved)}")
            return jsonify({'success': True, 'saved': saved})

        # ── Maintenance config ───────────────────────────────────────────────

        @self.app.route('/api/config/maintenance')
        def api_config_maintenance_get():
            """Return DB backup and email hook settings from bot_metadata."""
            settings = self._read_meta_settings(
                'maint',
                [
                    'db_backup_enabled', 'db_backup_schedule',
                    'db_backup_time', 'db_backup_retention_count',
                    'db_backup_dir', 'email_attach_log',
                ],
                defaults={
                    'db_backup_enabled': 'false',
                    'db_backup_schedule': 'daily',
                    'db_backup_time': '02:00',
                    'db_backup_retention_count': '7',
                    'db_backup_dir': '/data/backups',
                    'email_attach_log': 'false',
                },
            )
            return jsonify(settings)

        @self.app.route('/api/config/maintenance', methods=['POST'])
        def api_config_maintenance_post():
            """Save DB backup and email hook settings to bot_metadata."""
            data = request.get_json(silent=True) or {}
            # Validate db_backup_dir before saving anything
            if 'db_backup_dir' in data:
                backup_dir = str(data['db_backup_dir']).strip()
                if backup_dir and not os.path.isdir(backup_dir):
                    return jsonify({
                        'error': f"Backup directory does not exist: {backup_dir}",
                    }), 400
            allowed = {
                'db_backup_enabled', 'db_backup_schedule', 'db_backup_time',
                'db_backup_retention_count', 'db_backup_dir', 'email_attach_log',
            }
            saved = self._write_meta_settings('maint', allowed, data)
            self.logger.info(f"Maintenance config updated: {', '.join(saved)}")
            return jsonify({'success': True, 'saved': saved})

        # ── Zombie radio alert config ────────────────────────────────────────

        @self.app.route('/api/config/zombie-alert')
        def api_config_zombie_alert_get() -> "Response":
            """Return zombie alert settings.

            Response includes both ``bot_metadata`` values (set via web UI) and
            ``config_ini`` values (read from config.ini) so the browser can
            show config.ini as the baseline defaults.
            """
            meta: dict[str, str] = {}
            for key in ('zombie.alert_enabled', 'zombie.alert_email'):
                short = key.split('.', 1)[1]
                val = self.db_manager.get_metadata(key)
                meta[short] = val if isinstance(val, str) else ''
            if not meta.get('alert_enabled'):
                meta['alert_enabled'] = 'false'
            ini: dict[str, str] = {
                'alert_enabled': (
                    'true'
                    if self.config.getboolean(
                        'Connection',
                        'radio_zombie_alert_enabled',
                        fallback=self.config.getboolean('Bot', 'radio_zombie_alert_enabled', fallback=False),
                    )
                    else 'false'
                ),
                'alert_email': self.config.get(
                    'Connection',
                    'radio_zombie_alert_email',
                    fallback=self.config.get('Bot', 'radio_zombie_alert_email', fallback=''),
                ),
            }
            return jsonify({'meta': meta, 'config_ini': ini})

        @self.app.route('/api/config/zombie-alert', methods=['POST'])
        def api_config_zombie_alert_post() -> "Response":
            """Save zombie alert settings to bot_metadata.

            If ``write_to_config`` is ``true`` in the request body, the values
            are also written back to config.ini under ``[Connection]``.  The config
            object in memory is updated immediately so the scheduler reads the
            new values without a restart.
            """
            data = request.get_json(silent=True) or {}
            allowed = {'alert_enabled', 'alert_email'}
            saved = self._write_meta_settings('zombie', allowed, data)
            self.logger.info("Zombie alert config updated (metadata): %s", ', '.join(saved))

            write_to_config = str(data.get('write_to_config', '')).lower() == 'true'
            config_saved = False
            if write_to_config:
                try:
                    if not self.config.has_section('Connection'):
                        self.config.add_section('Connection')
                    ini_updates: dict[str, str] = {}
                    if 'alert_enabled' in data:
                        ini_updates['radio_zombie_alert_enabled'] = (
                            'true' if str(data['alert_enabled']).lower() == 'true' else 'false'
                        )
                    if 'alert_email' in data:
                        ini_updates['radio_zombie_alert_email'] = str(data['alert_email'])
                    if ini_updates:
                        # Persist first: alert_email is free-form client JSON, so
                        # a rejected value must not leave the in-memory config
                        # holding something that was never written to disk.
                        update_ini_values(self.config_path, {'Connection': ini_updates})
                        for ini_key, ini_val in ini_updates.items():
                            self.config.set('Connection', ini_key, ini_val)
                    config_saved = True
                    self.logger.info("Zombie alert settings written to config.ini")
                except IniValueError as exc:
                    # Not an OSError — without this the route would 500.
                    self.logger.warning("Rejected zombie alert config value: %s", exc)
                    return jsonify({'success': False, 'error': str(exc)}), 400
                except OSError as exc:
                    self.logger.error("Failed to write zombie alert settings to config.ini: %s", exc)
                    return jsonify({
                        'success': False,
                        'error': 'Could not write config.ini — check file permissions',
                    }), 500

            return jsonify({'success': True, 'saved': saved, 'config_saved': config_saved})

        # ── Zombie recover ───────────────────────────────────────────────────

        @self.app.route('/api/admin/zombie-recover', methods=['POST'])
        def api_admin_zombie_recover() -> "Response":
            """Clear zombie state so bot resumes processing after a radio power cycle.

            Clears the ``_radio_zombie_detected`` flag on the live bot object (if
            accessible) and removes the persisted flag from bot_metadata so the
            web-viewer banner disappears on the next page load.
            """
            try:
                self.db_manager.set_metadata('bot.radio_zombie', 'false')
                self.db_manager.set_metadata('bot.radio_zombie_since', '')
                bot = getattr(self, 'bot', None)
                if bot is not None:
                    bot._radio_zombie_detected = False
                    bot._radio_fail_count = 0
                    bot._last_radio_probe = 0  # force probe on next cycle
                self.logger.info("Zombie state cleared via web UI recover action")
                return jsonify({'success': True, 'message': 'Zombie state cleared; bot will resume'})
            except Exception:
                self.logger.exception("Error clearing zombie state")
                return jsonify({'success': False, 'error': 'Internal error — see server logs'}), 500

        # ── Radio debug config ───────────────────────────────────────────────

        @self.app.route('/api/config/radio-debug')
        def api_config_radio_debug_get() -> "Response":
            """Return current radio debug logging setting.

            Response includes both ``bot_metadata`` value (set via web UI) and
            ``config_ini`` value (read from config.ini) so the browser can show
            which is the persistent baseline.
            """
            meta_val = self.db_manager.get_metadata('radio.debug')
            meta_enabled = meta_val if isinstance(meta_val, str) else ''
            ini_enabled = (
                'true'
                if self.config.getboolean('Connection', 'radio_debug', fallback=False)
                else 'false'
            )
            return jsonify({'meta': {'enabled': meta_enabled}, 'config_ini': {'enabled': ini_enabled}})

        @self.app.route('/api/config/radio-debug', methods=['POST'])
        def api_config_radio_debug_post() -> "Response":
            """Save radio debug logging setting.

            Body fields:
            - ``enabled``: ``'true'`` or ``'false'``
            - ``write_to_config``: ``'true'`` to also write ``[Connection]
              radio_debug`` to config.ini
            - ``reconnect``: ``'true'`` to queue a radio reconnect so the
              change takes effect immediately (the debug flag is only applied
              at connection time)
            """
            try:
                data = request.get_json(silent=True) or {}
                enabled = str(data.get('enabled', 'false')).lower() == 'true'
                write_to_config = str(data.get('write_to_config', 'false')).lower() == 'true'
                do_reconnect = str(data.get('reconnect', 'false')).lower() == 'true'

                self.db_manager.set_metadata('radio.debug', 'true' if enabled else 'false')
                config_saved = False

                if write_to_config:
                    try:
                        if not self.config.has_section('Connection'):
                            self.config.add_section('Connection')
                        val = 'true' if enabled else 'false'
                        self.config.set('Connection', 'radio_debug', val)
                        update_ini_values(self.config_path, {'Connection': {'radio_debug': val}})
                        config_saved = True
                        self.logger.info(
                            "radio_debug=%s written to config.ini by web UI", val
                        )
                    except OSError as exc:
                        self.logger.error("Failed to write radio_debug to config.ini: %s", exc)
                        return jsonify({
                            'success': False,
                            'error': 'Could not write config.ini — check file permissions',
                        }), 500

                op_id = None
                if do_reconnect:
                    op_id = self._queue_operation('radio_connect')
                    self.logger.info("Radio reconnect queued (op_id=%s) to apply radio_debug=%s", op_id, enabled)

                return jsonify({'success': True, 'config_saved': config_saved, 'op_id': op_id})
            except Exception as exc:
                self.logger.exception("Error saving radio debug config")
                return jsonify({'success': False, 'error': str(exc)}), 500

        # ── Radio probe config ───────────────────────────────────────────────

        @self.app.route('/api/config/radio-probe')
        def api_config_radio_probe_get() -> "Response":
            """Return radio probe settings."""
            try:
                return jsonify({
                    'probe_interval_seconds': self.db_manager.get_metadata('radio.probe_interval_seconds') or
                        self.config.getint('Connection', 'radio_probe_interval_seconds', fallback=300),
                    'probe_fail_threshold': self.db_manager.get_metadata('radio.probe_fail_threshold') or
                        self.config.getint('Connection', 'radio_probe_fail_threshold', fallback=3),
                })
            except Exception as exc:
                self.logger.exception("Error getting radio probe config")
                return jsonify({'success': False, 'error': str(exc)}), 500

        @self.app.route('/api/config/radio-probe', methods=['POST'])
        def api_config_radio_probe_post() -> "Response":
            """Save radio probe settings to bot_metadata."""
            try:
                data = request.get_json(silent=True) or {}
                probe_interval = int(data.get('probe_interval_seconds', 300))
                probe_fail_threshold = int(data.get('probe_fail_threshold', 3))

                # Validate ranges
                if not (300 <= probe_interval <= 900):
                    return jsonify({'success': False, 'error': 'probe_interval_seconds must be 300-900'}), 400
                if not (1 <= probe_fail_threshold <= 10):
                    return jsonify({'success': False, 'error': 'probe_fail_threshold must be 1-10'}), 400

                saved = []
                self.db_manager.set_metadata('radio.probe_interval_seconds', str(probe_interval))
                saved.append('probe_interval_seconds')
                self.db_manager.set_metadata('radio.probe_fail_threshold', str(probe_fail_threshold))
                saved.append('probe_fail_threshold')

                self.logger.info("Radio probe config updated (metadata): %s", ', '.join(saved))

                # Optionally save to config.ini
                config_saved = False
                if data.get('save_to_config', False):
                    try:
                        self.config.set('Connection', 'radio_probe_interval_seconds', str(probe_interval))
                        self.config.set('Connection', 'radio_probe_fail_threshold', str(probe_fail_threshold))
                        update_ini_values(self.config_path, {'Connection': {
                            'radio_probe_interval_seconds': str(probe_interval),
                            'radio_probe_fail_threshold': str(probe_fail_threshold),
                        }})
                        config_saved = True
                        self.logger.info("Radio probe settings written to config.ini")
                    except Exception as exc:
                        self.logger.error("Failed to write radio probe settings to config.ini: %s", exc)

                return jsonify({'success': True, 'saved': saved, 'config_saved': config_saved})
            except Exception as exc:
                self.logger.exception("Error saving radio probe config")
                return jsonify({'success': False, 'error': str(exc)}), 500

        # ── Radio offline alert config ───────────────────────────────────────

        @self.app.route('/api/config/radio-offline-alert')
        def api_config_radio_offline_alert_get() -> "Response":
            """Return radio offline alert settings."""
            try:
                return jsonify({
                    'offline_threshold': self.db_manager.get_metadata('radio.offline_threshold') or
                        self.config.getint('Connection', 'radio_offline_threshold', fallback=3),
                    'alert_enabled': self.db_manager.get_metadata('radio.offline_alert_enabled') == 'true' or
                        self.config.getboolean('Connection', 'radio_offline_alert_enabled', fallback=False),
                    'alert_email': self.db_manager.get_metadata('radio.offline_alert_email') or
                        self.config.get('Connection', 'radio_offline_alert_email', fallback=''),
                })
            except Exception as exc:
                self.logger.exception("Error getting radio offline alert config")
                return jsonify({'success': False, 'error': str(exc)}), 500

        @self.app.route('/api/config/radio-offline-alert', methods=['POST'])
        def api_config_radio_offline_alert_post() -> "Response":
            """Save radio offline alert settings to bot_metadata."""
            try:
                data = request.get_json(silent=True) or {}
                offline_threshold = int(data.get('offline_threshold', 3))
                alert_enabled = bool(data.get('alert_enabled', False))
                alert_email = str(data.get('alert_email', '')).strip()

                # Validate ranges
                if not (1 <= offline_threshold <= 10):
                    return jsonify({'success': False, 'error': 'offline_threshold must be 1-10'}), 400

                saved = []
                self.db_manager.set_metadata('radio.offline_threshold', str(offline_threshold))
                saved.append('offline_threshold')
                self.db_manager.set_metadata('radio.offline_alert_enabled', 'true' if alert_enabled else 'false')
                saved.append('alert_enabled')
                self.db_manager.set_metadata('radio.offline_alert_email', alert_email)
                saved.append('alert_email')

                self.logger.info("Radio offline alert config updated (metadata): %s", ', '.join(saved))

                # Optionally save to config.ini
                config_saved = False
                if data.get('save_to_config', False):
                    try:
                        enabled_val = 'true' if alert_enabled else 'false'
                        offline_ini = {
                            'radio_offline_threshold': str(offline_threshold),
                            'radio_offline_alert_enabled': enabled_val,
                            'radio_offline_alert_email': alert_email,
                        }
                        # Persist first: alert_email is free-form client input, so
                        # a rejected value must not leave the in-memory config
                        # holding something that was never written to disk.
                        update_ini_values(self.config_path, {'Connection': offline_ini})
                        for ini_key, ini_val in offline_ini.items():
                            self.config.set('Connection', ini_key, ini_val)
                        config_saved = True
                        self.logger.info("Radio offline alert settings written to config.ini")
                    except Exception as exc:
                        self.logger.error("Failed to write radio offline alert settings to config.ini: %s", exc)

                return jsonify({'success': True, 'saved': saved, 'config_saved': config_saved})
            except Exception as exc:
                self.logger.exception("Error saving radio offline alert config")
                return jsonify({'success': False, 'error': str(exc)}), 500

        # ── Radio offline clear ──────────────────────────────────────────────

        @self.app.route('/api/admin/radio-offline-clear', methods=['POST'])
        def api_admin_radio_offline_clear() -> "Response":
            """Clear the radio-offline flag so the bot resumes outbound sends."""
            try:
                self.db_manager.set_metadata('bot.radio_offline', 'false')
                self.db_manager.set_metadata('bot.radio_offline_since', '')
                bot = getattr(self, 'bot', None)
                if bot is not None:
                    clear = getattr(bot, '_clear_radio_offline_state', None)
                    if callable(clear):
                        clear()
                    else:
                        bot._radio_offline = False
                        bot._send_consecutive_failures = 0
                self.logger.info("Radio-offline state cleared via web UI action")
                return jsonify({'success': True, 'message': 'Radio-offline flag cleared; sends will resume'})
            except Exception:
                self.logger.exception("Error clearing radio-offline state")
                return jsonify({'success': False, 'error': 'Internal error — see server logs'}), 500

        # ── Maintenance status ───────────────────────────────────────────────

        @self.app.route('/api/maintenance/backup_now', methods=['POST'])
        def api_maintenance_backup_now():
            """Trigger an immediate DB backup outside the normal schedule."""
            try:
                runner = getattr(self, '_maintenance_runner', None)
                if runner is None:
                    return jsonify({'success': False, 'error': 'Maintenance runner not available'}), 503
                runner.run_db_backup()
                # Read the outcome written by MaintenanceRunner.
                path = self.db_manager.get_metadata('maint.status.db_backup_path') or ''
                outcome = self.db_manager.get_metadata('maint.status.db_backup_outcome') or ''
                if outcome.startswith('error'):
                    return jsonify({'success': False, 'error': outcome}), 500
                return jsonify({'success': True, 'path': path, 'outcome': outcome})
            except Exception as e:
                self.logger.error(f"Error in backup_now: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/maintenance/restore', methods=['POST'])
        def api_maintenance_restore():
            """Stage a verified DB backup for the next full service restart.

            Body: {"db_file": "/absolute/path/to/backup.db"}
            The active DB is never modified by this request.  Normal bot startup
            applies the sibling pending file before any DB connection is opened.
            """
            try:
                data = request.get_json(silent=True) or {}
                db_file = str(data.get('db_file', '')).strip()
                if not db_file:
                    return jsonify({'error': 'db_file is required'}), 400

                # Validate path is within the configured backup directory
                backup_dir_str = self.db_manager.get_metadata('maint.db_backup_dir') or ''
                if not backup_dir_str or not os.path.isdir(backup_dir_str):
                    return jsonify({'error': 'No valid backup directory configured'}), 400

                # Validate path is within the configured backup directory
                # First check for dangerous system paths, then check if path is within backup dir
                backup_dir = Path(backup_dir_str).resolve()
                src = Path(db_file).resolve()

                # Check for dangerous system paths first (returns 400)
                target_str = str(src).lower()
                dangerous_prefixes = [
                    '/etc', '/private/etc',
                    '/sys', '/proc', '/dev', '/bin', '/sbin', '/boot',
                ]
                if any(target_str.startswith(prefix) for prefix in dangerous_prefixes):
                    return jsonify({'error': 'Access to system directory denied'}), 400

                # Check if path is within the backup directory (prevents traversal)
                try:
                    src.relative_to(backup_dir)
                except ValueError:
                    # Path is outside backup directory - return 403
                    return jsonify({'error': 'Restore path must be within the configured backup directory'}), 403

                if not src.exists():
                    return jsonify({'error': f'File not found: {db_file}'}), 400
                try:
                    max_restore_bytes = self.config.getint(
                        'Web_Viewer',
                        'restore_max_bytes',
                        fallback=DEFAULT_MAX_RESTORE_BYTES,
                    )
                    pending = stage_database_restore(
                        src,
                        self.db_path,
                        max_bytes=max_restore_bytes,
                    )
                except (DatabaseRestoreError, OSError, ValueError) as exc:
                    self.logger.warning("Database restore staging rejected for %s: %s", src, exc)
                    return jsonify({'error': str(exc)}), 400

                self.logger.warning(
                    "Database restore staged from %s at %s; a full service restart is required",
                    src,
                    pending,
                )
                return jsonify({
                    'success': True,
                    'staged_from': db_file,
                    'pending_path': str(pending),
                    'active_db': self.db_path,
                    'requires_restart': True,
                    'warning': (
                        'Restore verified and staged. Restart the complete MeshCore Bot service '
                        'to apply it before any database writer starts.'
                    ),
                }), 202
            except Exception as e:
                self.logger.error(f"Error in restore: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/maintenance/list_backups')
        @self._api_errors('Error listing backups')
        def api_maintenance_list_backups():
            """List available backup files from the configured backup directory."""
            backup_dir_str = self.db_manager.get_metadata('maint.db_backup_dir') or ''
            if not backup_dir_str or not os.path.isdir(backup_dir_str):
                return jsonify({'backups': []})
            backup_dir = Path(backup_dir_str)
            db_stem = Path(self.db_path).stem
            files = sorted(
                backup_dir.glob(f'{db_stem}_*.db'),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            backups = [
                {
                    'path': str(f),
                    'name': f.name,
                    'size_mb': round(f.stat().st_size / 1_048_576, 2),
                    'mtime': f.stat().st_mtime,
                }
                for f in files
            ]
            return jsonify({'backups': backups})

        @self.app.route('/api/maintenance/purge', methods=['POST'])
        def api_maintenance_purge():
            """Delete aged rows from time-series tables.

            Body: {"keep_days": <int>|"all", "tables": [<name>, ...] optional}

            If ``tables`` is omitted or null, all purgeable tables are included.
            If ``tables`` is a non-empty list, only those table names are purged
            (each must be one of the known purgeable tables).
            An empty ``tables`` list is invalid (400).

            Valid keep_days values: "all", 1, 7, 14, 30, 60, 90
            Returns: {"deleted": {<table>: <count>, ...}} — only tables that were purged
            """
            _VALID_KEEP_DAYS = {"all", 1, 7, 14, 30, 60, 90}
            # (table, timestamp column) — some tables are created lazily.
            _purge_ops = [
                ('packet_stream', 'timestamp'),
                ('message_stats', 'timestamp'),
                ('complete_contact_tracking', 'last_heard'),
                ('purging_log', 'timestamp'),
                ('mesh_connections', 'last_seen'),
                ('daily_stats', 'date'),
            ]
            _PURGEABLE = {table for table, _ in _purge_ops}
            try:
                data = request.get_json(silent=True) or {}
                raw = data.get('keep_days', 'all')
                if raw == 'all' or raw == 'All':
                    keep_days: str | int = 'all'
                else:
                    try:
                        keep_days = int(raw)
                    except (TypeError, ValueError):
                        return jsonify({'error': f'Invalid keep_days: {raw!r}'}), 400
                if keep_days not in _VALID_KEEP_DAYS:
                    return jsonify({'error': f'keep_days must be one of {sorted(v for v in _VALID_KEEP_DAYS if isinstance(v, int))} or "all"'}), 400

                tables_filter: list[str] | None = None
                if 'tables' in data:
                    tf = data.get('tables')
                    if tf is None:
                        tables_filter = None
                    elif not isinstance(tf, list):
                        return jsonify({'error': 'tables must be a list of table names or null'}), 400
                    elif len(tf) == 0:
                        return jsonify({'error': 'tables cannot be empty; omit tables to purge all tables'}), 400
                    else:
                        bad = [x for x in tf if not isinstance(x, str) or x not in _PURGEABLE]
                        if bad:
                            return jsonify({
                                'error': f'Invalid table name(s): {bad!r}; allowed: {sorted(_PURGEABLE)}',
                            }), 400
                        seen: set[str] = set()
                        tables_filter = []
                        for name in tf:
                            if name not in seen:
                                seen.add(name)
                                tables_filter.append(name)

                deleted: dict[str, int] = {}
                if keep_days == 'all':
                    # Nothing to delete — keep everything
                    return jsonify({'deleted': deleted})

                assert isinstance(keep_days, int)
                from datetime import timedelta as _timedelta
                cutoff_unix = time.time() - keep_days * 86400
                _cutoff_dt = datetime.now(timezone.utc) - _timedelta(days=keep_days)
                cutoff_iso = _cutoff_dt.strftime('%Y-%m-%d %H:%M:%S')
                cutoff_date = _cutoff_dt.strftime('%Y-%m-%d')

                _params_for = {
                    'packet_stream': (cutoff_unix,),
                    'message_stats': (int(cutoff_unix),),
                    'complete_contact_tracking': (cutoff_iso,),
                    'purging_log': (cutoff_iso,),
                    'mesh_connections': (cutoff_iso,),
                    'daily_stats': (cutoff_date,),
                }

                if tables_filter is None:
                    ops_to_run = [
                        (table, column, _params_for[table][0])
                        for table, column in _purge_ops
                    ]
                else:
                    want = set(tables_filter)
                    ops_to_run = [
                        (table, column, _params_for[table][0])
                        for table, column in _purge_ops
                        if table in want
                    ]

                for table, column, cutoff in ops_to_run:
                    try:
                        deleted[table] = (
                            self.db_manager.delete_timestamp_rows_in_chunks(
                                table,
                                column,
                                cutoff,
                                progress_label=f'manual {table.replace("_", " ")} purge',
                            )
                        )
                    except Exception:
                        deleted[table] = 0

                total = sum(deleted.values())
                self.logger.info(
                    f"Purge completed: keep_days={keep_days}, tables={tables_filter!r}, "
                    f"total_deleted={total}, by_table={deleted}"
                )
                return jsonify({'deleted': deleted})

            except Exception as e:
                self.logger.error(f"Error in purge: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/maintenance/status')
        def api_maintenance_status():
            """Return last-run times and outcomes for all maintenance jobs."""
            status_keys = [
                'maint.status.data_retention_ran_at',
                'maint.status.data_retention_outcome',
                'maint.status.nightly_email_ran_at',
                'maint.status.nightly_email_outcome',
                'maint.status.db_backup_ran_at',
                'maint.status.db_backup_outcome',
                'maint.status.db_backup_path',
                'maint.status.log_rotation_applied_at',
            ]
            result = {}
            for k in status_keys:
                short = k[len('maint.status.'):]
                val = self.db_manager.get_metadata(k)
                result[short] = val if val is not None else ''
            return jsonify(result)

        @self.app.route('/api-explorer')
        def api_explorer():
            """API Explorer — browse all endpoints with curl examples."""
            return render_template('api_explorer.html')

        @self.app.route('/admin/config')
        def admin_config():
            """Resolved config viewer — shows effective config.ini values with sensitive fields redacted."""
            sections = config_to_redacted_sections(self.config)
            return render_template('admin_config.html', sections=sections, config_path=self.config_path)

        @self.app.route('/mesh')
        def mesh():
            """Mesh graph visualization page"""
            prefix_hex_chars = self.config.getint('Bot', 'prefix_bytes', fallback=1) * 2
            if prefix_hex_chars <= 0:
                prefix_hex_chars = 2
            return render_template(
                'mesh.html',
                prefix_hex_chars=prefix_hex_chars
            )

        # Favicon routes
        @self.app.route('/apple-touch-icon.png')
        def apple_touch_icon():
            """Apple touch icon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'apple-touch-icon.png'
            )

        @self.app.route('/favicon-32x32.png')
        def favicon_32x32():
            """32x32 favicon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'favicon-32x32.png'
            )

        @self.app.route('/favicon-16x16.png')
        def favicon_16x16():
            """16x16 favicon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'favicon-16x16.png'
            )

        @self.app.route('/site.webmanifest')
        def site_webmanifest():
            """Web manifest file"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'site.webmanifest',
                mimetype='application/manifest+json'
            )

        @self.app.route('/favicon.ico')
        def favicon():
            """Default favicon"""
            return send_from_directory(
                os.path.join(os.path.dirname(__file__), 'static', 'ico'),
                'favicon.ico'
            )


        # API Routes
        @self.app.route('/api/health')
        def api_health():
            """Health check endpoint"""
            # Get bot uptime
            bot_uptime = self._get_bot_uptime()

            with self._clients_lock:
                client_count = len(self.connected_clients)

            radio_zombie = self.db_manager.get_metadata('bot.radio_zombie') == 'true'
            radio_zombie_since = self.db_manager.get_metadata('bot.radio_zombie_since') or None

            return jsonify({
                'status': 'degraded' if radio_zombie else 'healthy',
                'connected_clients': client_count,
                'max_clients': self.max_clients,
                'timestamp': time.time(),
                'bot_uptime': bot_uptime,
                'version': 'modern_2.0',
                'radio_zombie': radio_zombie,
                'radio_zombie_since': radio_zombie_since,
            })

        @self.app.route('/api/banner-status')
        def api_banner_status():
            """Return current banner states, and whether the station is up, for live JS polling."""
            try:
                radio_zombie = self.db_manager.get_metadata('bot.radio_zombie') == 'true'
                radio_zombie_since = self.db_manager.get_metadata('bot.radio_zombie_since') or None
                radio_offline = self.db_manager.get_metadata('bot.radio_offline') == 'true'
                radio_offline_since = self.db_manager.get_metadata('bot.radio_offline_since') or None
                bot_initializing = self.db_manager.get_metadata('bot.initializing') == 'true'
                station = self._station_state()
            except Exception:
                radio_zombie = False
                radio_zombie_since = None
                radio_offline = False
                radio_offline_since = None
                bot_initializing = False
                station = {'bot_running': None, 'bot_last_seen': None, 'bot_started': None, 'radio_connected': None}
            return jsonify({
                'radio_zombie': radio_zombie,
                'radio_zombie_since': radio_zombie_since,
                'radio_offline': radio_offline,
                'radio_offline_since': radio_offline_since,
                'bot_initializing': bot_initializing,
                **station,
            })

        @self.app.route('/api/system-health')
        def api_system_health():
            """Get comprehensive system health status from database"""
            try:
                # Read health data from database (consistent with how other data is accessed)
                health_data = self.db_manager.get_system_health()

                if not health_data:
                    # If no health data in database, return minimal status
                    return jsonify({
                        'status': 'unknown',
                        'timestamp': time.time(),
                        'message': 'Health data not available yet',
                        'components': {}
                    })

                # Update timestamp to reflect current time (data may be slightly stale)
                health_data['timestamp'] = time.time()

                # Recalculate uptime if start_time is available
                start_time = self.db_manager.get_bot_start_time()
                if start_time:
                    health_data['uptime_seconds'] = time.time() - start_time

                # Inject zombie radio state from shared metadata
                radio_zombie = self.db_manager.get_metadata('bot.radio_zombie') == 'true'
                health_data['radio_zombie'] = radio_zombie
                health_data['radio_zombie_since'] = (
                    self.db_manager.get_metadata('bot.radio_zombie_since') or None
                )
                if radio_zombie:
                    health_data['status'] = 'degraded'

                return jsonify(health_data)

            except Exception as e:
                self.logger.error(f"Error getting system health: {e}")
                import traceback
                self.logger.debug(traceback.format_exc())
                return jsonify({
                    'error': str(e),
                    'status': 'error'
                }), 500

        @self.app.route('/api/stats')
        @self._api_errors('Error getting stats')
        def api_stats():
            """Deprecated: whole-database statistics in one payload.

            Superseded by /api/dashboard/summary (snapshot-backed) and
            /api/dashboard/top (one narrow query per leaderboard).  Retained
            with every key name intact for external consumers, and scheduled for
            removal at the next major version.
            """
            # Get optional time window parameters for analytics
            top_users_window = request.args.get('top_users_window', 'all')
            top_commands_window = request.args.get('top_commands_window', 'all')
            top_paths_window = request.args.get('top_paths_window', 'all')
            top_channels_window = request.args.get('top_channels_window', 'all')
            stats = self._get_database_stats(
                top_users_window=top_users_window,
                top_commands_window=top_commands_window,
                top_paths_window=top_paths_window,
                top_channels_window=top_channels_window
            )
            stats['deprecated'] = True
            response = jsonify(stats)
            response.headers['Deprecation'] = 'true'
            response.headers['Sunset'] = STATS_ENDPOINT_SUNSET
            response.headers['Link'] = '</api/dashboard/summary>; rel="successor-version"'
            return response


        @self.app.route('/api/dashboard/summary')
        @self._api_errors('Error reading dashboard summary')
        def api_dashboard_summary():
            """Snapshot-backed dashboard payload — one row read, no aggregation.

            Sparkline series are folded in so first paint costs two requests
            (/api/health plus this) instead of the six the old page made.
            """
            with self._with_db_connection() as conn:
                payload = self.dashboard_stats.read_summary(conn)
            if payload is None:
                # No snapshot yet: the refresher runs a couple of seconds
                # after startup, so tell the client to retry rather than
                # recomputing everything on the request path.
                response = jsonify({
                    'error': 'Dashboard snapshot not generated yet',
                    'pending': True,
                })
                response.status_code = 503
                response.headers['Retry-After'] = '5'
                return response

            # ETags.contains() wants the bare tag; the quotes belong only in
            # the header itself.
            tag = str(payload['generated_at'])
            if request.if_none_match.contains(tag):
                response = self.app.response_class(status=304)
            else:
                response = jsonify(payload)
            response.headers['ETag'] = f'"{tag}"'
            response.headers['Cache-Control'] = 'private, max-age=15'
            return response

        @self.app.route('/api/dashboard/series')
        def api_dashboard_series():
            """Full-history points for one metric, for the expand-chart interaction."""
            metric = request.args.get('metric', 'messages')
            if metric not in SERIES_METRICS:
                return jsonify({'error': f'Unknown metric: {metric}'}), 400
            try:
                days = int(request.args.get('days', 30))
            except (TypeError, ValueError):
                days = 30
            try:
                with self._with_db_connection() as conn:
                    payload = self.dashboard_stats.read_series(conn, metric, days)
                tag = f'{metric}:{days}:{payload.pop("etag_source", None)}'
                if request.if_none_match.contains(tag):
                    response = self.app.response_class(status=304)
                else:
                    response = jsonify(payload)
                response.headers['ETag'] = f'"{tag}"'
                response.headers['Cache-Control'] = 'private, max-age=300'
                return response
            except Exception as e:
                self.logger.error(f"Error reading dashboard series: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/dashboard/top')
        def api_dashboard_top():
            """One leaderboard, one narrow query — replaces four whole-payload reads."""
            kind = request.args.get('kind', 'users')
            if kind not in TOP_KINDS:
                return jsonify({'error': f'Unknown kind: {kind}'}), 400
            window = request.args.get('window', 'all')
            if window not in ('24h', '7d', '30d', '90d', 'all'):
                window = 'all'
            try:
                limit = int(request.args.get('limit', 15))
            except (TypeError, ValueError):
                limit = 15
            try:
                with self._with_db_connection() as conn:
                    payload = self.dashboard_stats.read_top(conn, kind, window, limit)
                response = jsonify(payload)
                response.headers['Cache-Control'] = 'private, max-age=30'
                return response
            except Exception as e:
                self.logger.error(f"Error reading dashboard top {kind}: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/dashboard/windows')
        @self._api_errors('Error deriving dashboard windows')
        def api_dashboard_windows():
            """Selector options derived from retention, so no label overclaims."""
            response = jsonify(self.dashboard_stats.derive_windows())
            response.headers['Cache-Control'] = 'private, max-age=300'
            return response

        @self.app.route('/api/telemetry/occupancy')
        @self._api_errors('Error reading channel occupancy')
        def api_channel_occupancy():
            """Live airtime windows, as the writer last left them.

            Airtime here is an estimate from the modulation, not a measurement of the
            channel being busy, and the rx and tx shares are reported apart — merging
            them would present a node's local hearing as the channel's airtime.  The
            payload carries the snapshot's age and a stale flag, because a window that
            has not been refreshed is not a measurement: without the age, a quiet
            channel and a bot that stopped recording look identical.
            """
            with self._with_db_connection() as conn:
                snapshot = self.occupancy.read_live(conn)
                payload = {
                    'enabled': self.occupancy.enabled,
                    'age_seconds': snapshot['age_seconds'],
                    'stale': snapshot['stale'],
                    **snapshot['fields'],
                }
                if snapshot['fields']:
                    try:
                        limit = int(request.args.get('limit', 10))
                    except (TypeError, ValueError):
                        limit = 10
                    payload['top_senders'] = self.occupancy.top_senders(conn, limit)

            if request.args.get('format') == 'text':
                # Only scalar fields, one per line: the sender list would become a comma
                # blob on one line, which is not a field a shell can read.
                text_fields = {k: v for k, v in payload.items() if not isinstance(v, (list, dict))}
                response = self.app.response_class(render_key_value(text_fields), mimetype='text/plain')
            else:
                response = jsonify(payload)
            response.headers['Cache-Control'] = 'private, max-age=30'
            return response

        @self.app.route('/api/telemetry/occupancy/history')
        @self._api_errors('Error reading channel occupancy history')
        def api_channel_occupancy_history():
            """Bucketed airtime as sums and counts.

            A bucket with no packet is absent from the array rather than 0.0.  Those
            are two different facts — "nothing seen" and "a bucket that was observed
            and held nothing on air" — and a chart that renders the first as the second
            shows a cliff at the retention boundary that reads as an outage.
            """
            try:
                days = int(request.args.get('days', 1))
            except (TypeError, ValueError):
                days = 1
            days = max(1, min(days, 90))

            end_ts = time.time()
            with self._with_db_connection() as conn:
                payload = self.occupancy.history(conn, end_ts - days * 86400, end_ts)

            if request.args.get('format') == 'text':
                summary = {
                    'sample_seconds': payload['sample_seconds'],
                    'buckets': len(payload['buckets']),
                    'start_time': payload['start_time'],
                    'end_time': payload['end_time'],
                    'total_rx_ms': payload['total_rx_ms'],
                    'total_tx_ms': payload['total_tx_ms'],
                    'packets': payload['packets'],
                    'p50_rx_ms': payload['p50_rx_ms'],
                    'p95_rx_ms': payload['p95_rx_ms'],
                }
                response = self.app.response_class(render_key_value(summary), mimetype='text/plain')
            else:
                response = jsonify(payload)
            response.headers['Cache-Control'] = 'private, max-age=30'
            return response

        @self.app.route('/api/dashboard/refresh', methods=['POST'])
        def api_dashboard_refresh():
            """Force a snapshot refresh. Used by the health strip's manual control."""
            try:
                with closing(self._dashboard_connection()) as conn:
                    result = self.dashboard_stats.refresh(conn)
                return jsonify({'success': True, **result})
            except Exception as e:
                self.logger.error(f"Error refreshing dashboard snapshot: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/stats/rate_limiters')
        @self._api_errors('Error getting rate limiter stats')
        def api_rate_limiter_stats():
            """Return current rate limiter statistics from the running bot."""
            bot = getattr(self, 'bot', None)
            stats: dict[str, Any] = {}
            if bot is None:
                return jsonify(stats)
            if hasattr(bot, 'rate_limiter') and bot.rate_limiter:
                stats['message'] = bot.rate_limiter.get_stats()
            if hasattr(bot, 'bot_tx_rate_limiter') and bot.bot_tx_rate_limiter:
                stats['tx'] = bot.bot_tx_rate_limiter.get_stats()
            if hasattr(bot, 'per_user_rate_limiter') and bot.per_user_rate_limiter:
                rl = bot.per_user_rate_limiter
                stats['per_user'] = {
                    'seconds': rl.seconds,
                    'tracked_users': len(rl._last_send),
                    'max_entries': rl.max_entries,
                }
            if hasattr(bot, 'channel_rate_limiter') and bot.channel_rate_limiter:
                stats['channels'] = bot.channel_rate_limiter.get_stats()
            if hasattr(bot, 'nominatim_rate_limiter') and bot.nominatim_rate_limiter:
                stats['nominatim'] = bot.nominatim_rate_limiter.get_stats()
            return jsonify(stats)

        @self.app.route('/api/connected_clients')
        @self._api_errors('Error getting connected clients')
        def api_connected_clients():
            """Return list of currently connected web viewer clients."""
            with self._clients_lock:
                clients = [
                    {
                        'client_id': cid[:8] + '…' if len(cid) > 8 else cid,
                        'connected_at': info.get('connected_at'),
                        'last_activity': info.get('last_activity'),
                    }
                    for cid, info in self.connected_clients.items()
                ]
            return jsonify(clients)

        @self.app.route('/api/contacts')
        @self._api_errors('Error getting contacts')
        def api_contacts():
            """Get filtered contact data, optionally paginated for the interactive list."""
            since = request.args.get('since', '30d')
            if since not in ('24h', '7d', '30d', '90d', 'all'):
                since = '30d'
            paginate = 'page' in request.args or 'page_size' in request.args
            page = None
            page_size = None
            if paginate:
                try:
                    page = max(1, int(request.args.get('page', '1')))
                except (TypeError, ValueError):
                    page = 1
                try:
                    page_size = max(1, min(200, int(request.args.get('page_size', '100'))))
                except (TypeError, ValueError):
                    page_size = 100
            search = request.args.get('search', '').strip()[:100]
            path_bytes = request.args.get('path_bytes', '').strip()
            device_role = request.args.get('device_role', '').strip()
            hop_filter = request.args.get('hop_filter', '').strip()
            location_filter = request.args.get('location_filter', '').strip()
            starred = request.args.get('starred', '').strip()
            sort = request.args.get('sort', 'last_seen')
            direction = request.args.get('direction', 'desc').lower()
            contacts = self._get_tracking_data(
                since=since,
                page=page,
                page_size=page_size,
                search=search,
                path_bytes=path_bytes,
                device_role=device_role,
                hop_filter=hop_filter,
                location_filter=location_filter,
                starred=starred,
                sort=sort,
                direction=direction,
            )
            return jsonify(contacts)

        @self.app.route('/api/contact-detail')
        @self._api_errors('Error getting contact detail')
        def api_contact_detail():
            """On-demand per-contact detail (recent advert paths + advertisement data) for the
            contacts UI modals. These are intentionally excluded from the /api/contacts list
            payload. Query param: user_id (the contact's public key)."""
            public_key = request.args.get('user_id', '').strip()
            if not public_key:
                return jsonify({'error': 'user_id is required'}), 400
            return jsonify(self._get_contact_detail(public_key))

        @self.app.route('/api/multibyte-rollout')
        def api_multibyte_rollout():
            """Multibyte hash rollout analytics. since=24h|7d|30d|90d|all, node_type=all|repeater|roomserver."""
            if not self.multibyte_monitor_enabled:
                return abort(404)
            try:
                since = request.args.get('since', '30d')
                if since not in ('24h', '7d', '30d', '90d', 'all'):
                    since = '30d'
                node_type = request.args.get('node_type', 'all')
                if node_type not in ('all', 'repeater', 'roomserver'):
                    node_type = 'all'
                data = self._get_multibyte_rollout_data(since=since, node_type=node_type)
                return jsonify(data)
            except Exception as e:
                self.logger.error(f"Error getting multibyte rollout data: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/cache')
        @self._api_errors('Error getting cache')
        def api_cache():
            """Get cache data"""
            cache_data = self._get_cache_data()
            return jsonify(cache_data)

        @self.app.route('/api/database')
        @self._api_errors('Error getting database info')
        def api_database():
            """Get database information"""
            db_info = self._get_database_info()
            return jsonify(db_info)

        @self.app.route('/api/optimize-database', methods=['POST'])
        def api_optimize_database():
            """Optimize database using VACUUM, ANALYZE, and REINDEX"""
            try:
                result = self._optimize_database()
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error optimizing database: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/mesh/nodes')
        def api_mesh_nodes():
            """Get all repeater nodes with locations and metadata. Prefix length from query param or [Bot] prefix_bytes.

            ``days`` limits nodes to the selected Node Timeframe; keeping the filter here
            also lets the country selector be built from countries heard in that period.
            """
            try:
                prefix_hex_chars = request.args.get('prefix_hex_chars', type=int)
                days = request.args.get('days', type=int)
                if days is not None and days <= 0:
                    days = None
                if prefix_hex_chars not in (2, 4, 6):
                    prefix_hex_chars = self.config.getint('Bot', 'prefix_bytes', fallback=1) * 2
                if prefix_hex_chars <= 0:
                    prefix_hex_chars = 2
                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    query = f'''
                    SELECT
                        public_key,
                        SUBSTR(public_key, 1, {prefix_hex_chars}) as prefix,
                        name,
                        latitude,
                        longitude,
                        city,
                        state,
                        country,
                        role,
                        is_starred,
                        last_heard,
                        last_advert_timestamp,
                        raw_advert_data
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                    AND latitude IS NOT NULL
                    AND longitude IS NOT NULL
                    AND latitude != 0
                    AND longitude != 0
                '''
                    params = []
                    if days is not None:
                        query += '''
                    AND COALESCE(NULLIF(last_heard, ''), last_advert_timestamp)
                        >= datetime("now", "-" || ? || " days")
                    '''
                        params.append(days)
                    query += ' ORDER BY name'

                    cursor.execute(query, params)
                    rows = cursor.fetchall()

                    nodes = []
                    for row in rows:
                        # adv_name (Meshcore short name) is stored inside raw_advert_data JSON.
                        # The web viewer runs as a separate process, so read it from the DB.
                        adv_name = None
                        raw = row['raw_advert_data']
                        if raw:
                            try:
                                parsed = json.loads(raw)
                                if isinstance(parsed, dict):
                                    candidate = (parsed.get('adv_name') or '').strip()
                                    if candidate:
                                        adv_name = candidate
                            except (ValueError, TypeError):
                                adv_name = None
                        nodes.append({
                            'public_key': row['public_key'],
                            'prefix': row['prefix'].lower(),
                            'name': row['name'] or f"Node {row['prefix']}",
                            'adv_name': adv_name,
                            'latitude': float(row['latitude']),
                            'longitude': float(row['longitude']),
                            'city': row['city'],
                            'state': row['state'],
                            'country': row['country'],
                            'role': row['role'],
                            'is_starred': bool(row['is_starred']),
                            'last_heard': row['last_heard'],
                            'last_advert_timestamp': row['last_advert_timestamp']
                        })

                    return jsonify({'nodes': nodes})
            except Exception as e:
                self.logger.error(f"Error getting mesh nodes: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/mesh/edges')
        def api_mesh_edges():
            """Get all graph edges with metadata.

            evidence=multibyte derives edges purely from unique multi-byte path
            observations (observed_paths, bytes_per_hop >= 2), bypassing the
            mesh_connections merge heuristics that single-byte evidence feeds into.

            evidence=neighbors derives edges purely from confirmed zero-hop
            discovery (neighbor_links) — full public keys on both ends plus a
            measured SNR, the strongest evidence class available.
            """
            try:
                # Get optional query parameters
                min_observations = request.args.get('min_observations', type=int)
                days = request.args.get('days', type=int)
                min_distance = request.args.get('min_distance', type=float)
                max_distance = request.args.get('max_distance', type=float)
                evidence = request.args.get('evidence', 'all')
                force_refresh = request.args.get('refresh') == '1'

                if evidence == 'multibyte':
                    edges, prefix_hex_chars = self._derive_multibyte_evidence_graph(
                        days=days,
                        min_observations=min_observations,
                        force_refresh=force_refresh,
                    )
                    return jsonify({
                        'edges': edges,
                        'prefix_hex_chars': prefix_hex_chars,
                        'evidence': 'multibyte',
                    })

                if evidence == 'neighbors':
                    edges, prefix_hex_chars = self._derive_neighbor_evidence_graph(
                        days=days,
                        min_observations=min_observations,
                    )
                    return jsonify({
                        'edges': edges,
                        'prefix_hex_chars': prefix_hex_chars,
                        'evidence': 'neighbors',
                    })

                # Combined view: mesh_connections cannot record *why* an edge
                # exists, so re-derive the strongest label from neighbor_links.
                # Same window as the edges themselves, so stale evidence cannot
                # claim a recent edge is a current direct neighbor.
                neighbor_keys = self._neighbor_evidence_edge_keys(days=days)

                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    # Edge windows control visibility, but node identity must retain
                    # the lifetime graph's prefix resolution. Otherwise an older
                    # multi-byte edge disappearing from the window can collapse
                    # distinct nodes onto one shorter prefix in the browser.
                    cursor.execute(
                        '''
                    SELECT COALESCE(
                        MAX(
                            CASE
                                WHEN LENGTH(from_prefix) > LENGTH(to_prefix)
                                THEN LENGTH(from_prefix)
                                ELSE LENGTH(to_prefix)
                            END
                        ),
                        2
                    ) AS prefix_hex_chars
                    FROM mesh_connections
                    '''
                    )
                    prefix_hex_chars = cursor.fetchone()['prefix_hex_chars']

                    query = '''
                    SELECT
                        from_prefix,
                        to_prefix,
                        from_public_key,
                        to_public_key,
                        observation_count,
                        first_seen,
                        last_seen,
                        avg_hop_position,
                        geographic_distance
                    FROM mesh_connections
                    WHERE 1=1
                '''
                    params = []

                    if min_observations is not None:
                        query += ' AND observation_count >= ?'
                        params.append(min_observations)

                    if days is not None:
                        query += ' AND last_seen >= datetime("now", "-" || ? || " days")'
                        params.append(days)

                    if min_distance is not None:
                        query += ' AND geographic_distance >= ?'
                        params.append(min_distance)

                    if max_distance is not None:
                        query += ' AND geographic_distance <= ?'
                        params.append(max_distance)

                    query += ' ORDER BY last_seen DESC'

                    cursor.execute(query, params)
                    rows = cursor.fetchall()

                    edges = []
                    for row in rows:
                        fp, tp = row['from_prefix'], row['to_prefix']
                        prefix_hex_chars = max(prefix_hex_chars, len(fp) if fp else 0, len(tp) if tp else 0)
                        # Edges keyed at 4+ hex chars were necessarily created (or promoted)
                        # by a multi-byte path observation; 2-char keys carry only ambiguous
                        # single-byte evidence.
                        is_multibyte = bool(fp) and bool(tp) and len(fp) >= 4 and len(tp) >= 4
                        from_lower = fp.lower() if fp else ''
                        to_lower = tp.lower() if tp else ''
                        from_key = (row['from_public_key'] or '').lower()
                        to_key = (row['to_public_key'] or '').lower()
                        if (
                            (from_lower, to_lower) in neighbor_keys.prefixes
                            or (from_key and to_key
                                and (from_key, to_key) in neighbor_keys.public_keys)
                        ):
                            edge_evidence = 'neighbors'
                        elif is_multibyte:
                            edge_evidence = 'multibyte'
                        else:
                            edge_evidence = 'singlebyte'
                        edges.append({
                            'from_prefix': from_lower,
                            'to_prefix': to_lower,
                            'from_public_key': row['from_public_key'],
                            'to_public_key': row['to_public_key'],
                            'observation_count': row['observation_count'],
                            'first_seen': row['first_seen'],
                            'last_seen': row['last_seen'],
                            'avg_hop_position': row['avg_hop_position'],
                            'geographic_distance': row['geographic_distance'],
                            'evidence': edge_evidence
                        })

                    return jsonify({'edges': edges, 'prefix_hex_chars': prefix_hex_chars or 2})
            except Exception as e:
                self.logger.error(f"Error getting mesh edges: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/mesh/stats')
        def api_mesh_stats():
            """Get graph statistics"""
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()

                # Get node count
                cursor.execute('''
                    SELECT COUNT(*) as count
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                    AND latitude IS NOT NULL
                    AND longitude IS NOT NULL
                    AND latitude != 0
                    AND longitude != 0
                ''')
                node_count = cursor.fetchone()['count']

                # Get edge statistics
                cursor.execute('''
                    SELECT
                        COUNT(*) as total_edges,
                        SUM(observation_count) as total_observations,
                        AVG(observation_count) as avg_observations,
                        AVG(geographic_distance) as avg_distance,
                        MIN(geographic_distance) as min_distance,
                        MAX(geographic_distance) as max_distance,
                        COUNT(CASE WHEN from_public_key IS NOT NULL THEN 1 END) as edges_with_from_key,
                        COUNT(CASE WHEN to_public_key IS NOT NULL THEN 1 END) as edges_with_to_key,
                        COUNT(CASE WHEN from_public_key IS NOT NULL AND to_public_key IS NOT NULL THEN 1 END) as edges_with_both_keys,
                        COUNT(CASE WHEN LENGTH(from_prefix) >= 4 AND LENGTH(to_prefix) >= 4 THEN 1 END) as multibyte_edges,
                        COUNT(CASE WHEN last_seen >= datetime("now", "-1 days") THEN 1 END) as recent_edges_24h
                    FROM mesh_connections
                ''')
                edge_stats = cursor.fetchone()

                # Get most connected nodes
                cursor.execute('''
                    SELECT
                        LOWER(prefix) AS prefix,
                        SUM(connection_count) AS connection_count
                    FROM (
                        SELECT from_prefix AS prefix, COUNT(*) AS connection_count
                        FROM mesh_connections
                        GROUP BY from_prefix
                        UNION ALL
                        SELECT to_prefix AS prefix, COUNT(*) AS connection_count
                        FROM mesh_connections
                        GROUP BY to_prefix
                    )
                    GROUP BY LOWER(prefix)
                    ORDER BY connection_count DESC, prefix
                    LIMIT 10
                ''')
                top_connected = [
                    (row['prefix'], row['connection_count'])
                    for row in cursor.fetchall()
                ]

                stats = {
                    'node_count': node_count,
                    'total_edges': edge_stats['total_edges'] or 0,
                    'total_observations': edge_stats['total_observations'] or 0,
                    'avg_observations': round(edge_stats['avg_observations'] or 0, 2),
                    'avg_distance': round(edge_stats['avg_distance'] or 0, 2) if edge_stats['avg_distance'] else None,
                    'min_distance': round(edge_stats['min_distance'] or 0, 2) if edge_stats['min_distance'] else None,
                    'max_distance': round(edge_stats['max_distance'] or 0, 2) if edge_stats['max_distance'] else None,
                    'edges_with_from_key': edge_stats['edges_with_from_key'] or 0,
                    'edges_with_to_key': edge_stats['edges_with_to_key'] or 0,
                    'edges_with_both_keys': edge_stats['edges_with_both_keys'] or 0,
                    'multibyte_edges': edge_stats['multibyte_edges'] or 0,
                    'top_connected': [{'prefix': prefix, 'count': count} for prefix, count in top_connected],
                    'recent_edges_24h': edge_stats['recent_edges_24h'] or 0
                }

                # Bot's own position (config [Bot] bot_latitude/bot_longitude), used by
                # the mesh page to frame the initial map view on the home mesh
                bot_lat = self.config.getfloat('Bot', 'bot_latitude', fallback=None)
                bot_lon = self.config.getfloat('Bot', 'bot_longitude', fallback=None)
                if bot_lat is not None and bot_lon is not None:
                    stats['bot_location'] = {'latitude': bot_lat, 'longitude': bot_lon}

                return jsonify(stats)
            except Exception as e:
                self.logger.error(f"Error getting mesh stats: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/mesh/resolve-path', methods=['POST'])
        def api_resolve_path():
            """Resolve a hex path to repeater names and locations using the same algorithm as path command"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'JSON body required'}), 400

                path_input = data.get('path', '').strip()
                if not path_input:
                    return jsonify({'error': 'Path input required'}), 400

                # Check if db_manager is initialized
                if not hasattr(self, 'db_manager') or not self.db_manager:
                    self.logger.error("db_manager not initialized")
                    return jsonify({'error': 'Database not initialized'}), 500

                resolved_path = self._resolve_path(path_input)
                return jsonify(resolved_path)
            except Exception as e:
                import traceback
                error_trace = traceback.format_exc()
                self.logger.error(f"Error resolving path: {e}\n{error_trace}")
                return jsonify({'error': str(e), 'traceback': error_trace}), 500

        @self.app.route('/api/stream_data', methods=['POST'])
        @self._api_errors('Error in stream_data endpoint')
        def api_stream_data():
            """API endpoint for receiving real-time data from bot.

            Requires a valid X-Stream-Token header matching the token stored
            in DB metadata by BotIntegration.  This prevents unauthenticated
            callers from injecting fake stream data when the web viewer is
            network-accessible.
            """
            if not current_app.config.get('TESTING'):
                token = request.headers.get('X-Stream-Token', '')
                expected = self.db_manager.get_metadata('internal.stream_token') if self.db_manager else None
                if not expected or not token or token != expected:
                    return jsonify({'error': 'Unauthorized'}), 401

            data = request.get_json()
            if not data:
                return jsonify({'error': 'No data provided'}), 400

            data_type = data.get('type')
            if data_type == 'command':
                self._handle_command_data(data.get('data', {}))
            elif data_type == 'packet':
                self._handle_packet_data(data.get('data', {}))
            elif data_type == 'mesh_edge':
                self._handle_mesh_edge_data(data.get('data', {}))
            elif data_type == 'mesh_node':
                self._handle_mesh_node_data(data.get('data', {}))
            else:
                return jsonify({'error': 'Invalid data type'}), 400

            return jsonify({'status': 'success'})

        @self.app.route('/api/recent_commands')
        @self._api_errors('Error getting recent commands')
        def api_recent_commands():
            """API endpoint to get recent commands from database"""
            import json
            import sqlite3
            import time

            # Get commands from last 60 minutes
            cutoff_time = time.time() - (60 * 60)  # 60 minutes ago

            with closing(sqlite3.connect(self.db_path, timeout=60)) as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT data FROM packet_stream
                    WHERE type = 'command' AND timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT 100
                ''', (cutoff_time,))

                rows = cursor.fetchall()

                # Parse and return commands
                commands = []
                for (data_json,) in rows:
                    try:
                        command_data = json.loads(data_json)
                        commands.append(command_data)
                    except Exception as e:
                        self.logger.debug(f"Error parsing command data: {e}")

                return jsonify({'commands': commands})

        @self.app.route('/api/packets/by-hash/<packet_hash>')
        @self._api_errors('Error looking up packet by hash')
        def api_packets_by_hash(packet_hash):
            """Every stored reception of one packet, oldest first, heard near ``near``.

            Real-time asks for this when a message or command row has a hash but
            the page never loaded its packet: the replay on load reaches back
            far less for packets than for messages. The hash is only inside the
            row's JSON, so ``near`` (the message's epoch time) bounds the scan to
            the timestamp index. Rows come back marked as replayed, like the
            socket replay's.
            """
            if not PACKET_HASH_RE.fullmatch(packet_hash):
                return jsonify({'error': 'Invalid packet hash'}), 400
            try:
                near = float(request.args.get('near', ''))
            except ValueError:
                return jsonify({'error': 'near must be an epoch time'}), 400
            if not math.isfinite(near):
                return jsonify({'error': 'near must be an epoch time'}), 400

            with self._db_connection() as conn:
                rows = conn.execute(
                    "SELECT timestamp, data FROM packet_stream"
                    " WHERE type = 'packet' AND timestamp BETWEEN ? AND ?"
                    " AND json_valid(data) AND json_extract(data, '$.packet_hash') = ?"
                    " ORDER BY timestamp LIMIT ?",
                    (
                        near - PACKET_LOOKUP_BEFORE_S,
                        near + PACKET_LOOKUP_AFTER_S,
                        packet_hash.upper(),
                        PACKET_LOOKUP_LIMIT,
                    ),
                ).fetchall()

            packets = []
            for row in rows:
                data = json.loads(row['data'])
                if isinstance(data, dict):
                    data['stream_ts'] = row['timestamp']
                    data['replayed'] = True
                    packets.append(data)
            return jsonify({'packets': packets})

        # ── Export ──────────────────────────────────────────────────────────

        @self.app.route('/api/export/contacts')
        def api_export_contacts():
            """Export contact tracking data as CSV or JSON.
            Query params: format=csv|json (default json), since=24h|7d|30d|90d|all (default 30d)."""
            import csv
            import io
            fmt = request.args.get('format', 'json').lower()
            since = request.args.get('since', '30d')
            if since not in ('24h', '7d', '30d', '90d', 'all'):
                since = '30d'
            try:
                result = self._get_tracking_data(since=since, include_detail=True)
                contacts = result.get('tracking_data', [])
                if fmt == 'csv':
                    fields = [
                        'user_id', 'username', 'role', 'device_type',
                        'latitude', 'longitude', 'city', 'state', 'country',
                        'snr', 'hop_count', 'first_heard', 'last_seen',
                        'advert_count', 'total_messages', 'distance', 'is_starred',
                    ]
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
                    w.writeheader()
                    w.writerows(contacts)
                    return Response(
                        buf.getvalue(),
                        mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="contacts_{since}.csv"'},
                    )
                else:
                    import json as _json
                    body = _json.dumps(contacts, indent=2, default=str)
                    return Response(
                        body,
                        mimetype='application/json',
                        headers={'Content-Disposition': f'attachment; filename="contacts_{since}.json"'},
                    )
            except Exception as e:
                self.logger.error(f"Error exporting contacts: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/export/paths')
        def api_export_paths():
            """Export observed path data as CSV or JSON.
            Query params: format=csv|json (default json), since=24h|7d|30d|90d|all (default 30d)."""
            import csv
            import io
            import json as _json
            import sqlite3
            fmt = request.args.get('format', 'json').lower()
            since = request.args.get('since', '30d')
            if since not in ('24h', '7d', '30d', '90d', 'all'):
                since = '30d'
            try:
                days_map = {'24h': 1, '7d': 7, '30d': 30, '90d': 90}
                where = (
                    f" AND op.last_seen >= datetime('now', 'localtime', '-{days_map[since]} days')"
                    if since != 'all' else ''
                )
                with closing(sqlite3.connect(self.db_path, timeout=60)) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(f"""
                        SELECT op.public_key, c.name AS contact_name,
                               op.path_hex, op.path_length, op.observation_count,
                               op.last_seen, op.from_prefix, op.to_prefix,
                               op.bytes_per_hop, op.packet_type
                        FROM observed_paths op
                        LEFT JOIN complete_contact_tracking c ON op.public_key = c.public_key
                        WHERE op.packet_type = 'advert' AND op.public_key IS NOT NULL
                        {where}
                        ORDER BY op.last_seen DESC
                        LIMIT 10000
                    """)
                    rows = [dict(r) for r in cursor.fetchall()]
                if fmt == 'csv':
                    fields = [
                        'public_key', 'contact_name', 'path_hex', 'path_length',
                        'observation_count', 'last_seen', 'from_prefix', 'to_prefix',
                        'bytes_per_hop', 'packet_type',
                    ]
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
                    w.writeheader()
                    w.writerows(rows)
                    return Response(
                        buf.getvalue(),
                        mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="paths_{since}.csv"'},
                    )
                else:
                    body = _json.dumps(rows, indent=2, default=str)
                    return Response(
                        body,
                        mimetype='application/json',
                        headers={'Content-Disposition': f'attachment; filename="paths_{since}.json"'},
                    )
            except Exception as e:
                self.logger.error(f"Error exporting paths: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/geocode-contact', methods=['POST'])
        def api_geocode_contact():
            """Manually geocode a contact by public_key"""
            try:
                data = request.get_json()
                if not data or 'public_key' not in data:
                    return jsonify({'error': 'public_key is required'}), 400

                public_key = data['public_key']

                # Get contact data from database
                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    cursor.execute('''
                    SELECT latitude, longitude, name, city, state, country
                    FROM complete_contact_tracking
                    WHERE public_key = ?
                ''', (public_key,))

                    contact = cursor.fetchone()
                    if not contact:
                        return jsonify({'error': 'Contact not found'}), 404

                    lat = contact['latitude']
                    lon = contact['longitude']
                    name = contact['name']

                    # Check if we have valid coordinates
                    if lat is None or lon is None or lat == 0.0 or lon == 0.0:
                        return jsonify({'error': 'Contact does not have valid coordinates'}), 400

                    # Perform geocoding
                    self.logger.info(f"Manual geocoding requested for {name} ({public_key[:16]}...) at coordinates {lat}, {lon}")
                    # sqlite3.Row objects use dictionary-style access with []
                    current_city = contact['city']
                    current_state = contact['state']
                    current_country = contact['country']
                    self.logger.debug(f"Current location data - city: {current_city}, state: {current_state}, country: {current_country}")

                    # Outside the try below: a failure to build the manager is a
                    # setup problem, not a geocoding one, and must not be reported
                    # to the user as "Geocoding exception".
                    repeater_manager = self._get_repeater_manager()

                    try:
                        location_info = repeater_manager._get_full_location_from_coordinates(lat, lon)
                        self.logger.debug(f"Geocoding result for {name}: {location_info}")
                    except Exception as geocode_error:
                        self.logger.error(f"Exception during geocoding for {name} at {lat}, {lon}: {geocode_error}", exc_info=True)
                        return jsonify({
                            'success': False,
                            'error': f'Geocoding exception: {str(geocode_error)}',
                            'location': {}
                        }), 500

                    # Check if geocoding returned any useful data
                    has_location_data = location_info.get('city') or location_info.get('state') or location_info.get('country')

                    if not has_location_data:
                        self.logger.warning(f"Geocoding returned no location data for {name} at {lat}, {lon}. Result: {location_info}")
                        return jsonify({
                            'success': False,
                            'error': 'Geocoding returned no location data. The coordinates may be invalid or the geocoding service may be unavailable.',
                            'location': location_info
                        }), 500

                    # Update database with new location data
                    cursor.execute('''
                    UPDATE complete_contact_tracking
                    SET city = ?, state = ?, country = ?
                    WHERE public_key = ?
                ''', (
                        location_info.get('city'),
                        location_info.get('state'),
                        location_info.get('country'),
                        public_key
                    ))

                    conn.commit()

                    # Build success message with what was found
                    found_parts = []
                    if location_info.get('city'):
                        found_parts.append(f"city: {location_info['city']}")
                    if location_info.get('state'):
                        found_parts.append(f"state: {location_info['state']}")
                    if location_info.get('country'):
                        found_parts.append(f"country: {location_info['country']}")

                    success_message = f'Successfully geocoded {name} - Found {", ".join(found_parts)}'
                    self.logger.info(f"Successfully geocoded {name}: {location_info}")

                    return jsonify({
                        'success': True,
                        'location': location_info,
                        'message': success_message
                    })

            except Exception as e:
                self.logger.error(f"Error geocoding contact: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/toggle-star-contact', methods=['POST'])
        def api_toggle_star_contact():
            """Toggle star status for any contact by public_key."""
            try:
                data = request.get_json()
                if not data or 'public_key' not in data:
                    return jsonify({'error': 'public_key is required'}), 400

                public_key = data['public_key']

                # Get contact data from database
                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    cursor.execute('''
                    SELECT name, is_starred, role FROM complete_contact_tracking
                    WHERE public_key = ?
                ''', (public_key,))

                    contact = cursor.fetchone()
                    if not contact:
                        return jsonify({'error': 'Contact not found'}), 404

                    # Toggle star status
                    current_starred = contact['is_starred']
                    new_star_status = 1 if not current_starred else 0
                    cursor.execute('''
                    UPDATE complete_contact_tracking
                    SET is_starred = ?
                    WHERE public_key = ?
                ''', (new_star_status, public_key))

                    conn.commit()

                    action = 'starred' if new_star_status else 'unstarred'
                    self.logger.info(f"Contact {contact['name']} ({public_key[:16]}...) {action}")

                    return jsonify({
                        'success': True,
                        'is_starred': bool(new_star_status),
                        'message': f'Contact {action} successfully'
                    })

            except Exception as e:
                self.logger.error(f"Error toggling star status: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/decode-path', methods=['POST'])
        def api_decode_path():
            """Decode path hex string to repeater names (similar to path command).
            Optional bytes_per_hop (1, 2, or 3): use when path came from a packet with multi-byte hops
            so decoding and graph selection use the correct prefix length."""
            try:
                data = request.get_json()
                if not data or 'path_hex' not in data:
                    return jsonify({'error': 'path_hex is required'}), 400

                path_hex = data['path_hex']
                if not path_hex:
                    return jsonify({'error': 'path_hex cannot be empty'}), 400

                bytes_per_hop = data.get('bytes_per_hop')
                if bytes_per_hop is not None:
                    try:
                        bytes_per_hop = int(bytes_per_hop)
                        if bytes_per_hop not in (1, 2, 3):
                            bytes_per_hop = None
                    except (TypeError, ValueError):
                        bytes_per_hop = None

                # Decode the path (use bytes_per_hop when provided, e.g. from packet/contact)
                decoded_path = self._decode_path_hex(path_hex, bytes_per_hop=bytes_per_hop)

                return jsonify({
                    'success': True,
                    'path': decoded_path
                })

            except Exception as e:
                self.logger.error(f"Error decoding path: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/delete-contact', methods=['POST'])
        def api_delete_contact():
            """Delete a contact from the complete contact tracking database"""
            try:
                data = request.get_json()
                if not data or 'public_key' not in data:
                    return jsonify({'error': 'public_key is required'}), 400

                public_key = data['public_key']

                # Get contact data from database to log what we're deleting
                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    # Check if contact exists
                    cursor.execute('''
                    SELECT name, role, device_type FROM complete_contact_tracking
                    WHERE public_key = ?
                ''', (public_key,))

                    contact = cursor.fetchone()
                    if not contact:
                        return jsonify({'error': 'Contact not found'}), 404

                    contact_name = contact['name']
                    contact_role = contact['role']
                    contact_device_type = contact['device_type']

                    # Delete from all related tables
                    deleted_counts = {}

                    # Delete from complete_contact_tracking
                    cursor.execute('DELETE FROM complete_contact_tracking WHERE public_key = ?', (public_key,))
                    deleted_counts['complete_contact_tracking'] = cursor.rowcount

                    # Delete from daily_stats
                    cursor.execute('DELETE FROM daily_stats WHERE public_key = ?', (public_key,))
                    deleted_counts['daily_stats'] = cursor.rowcount

                    # Delete from repeater_contacts if it exists
                    try:
                        cursor.execute('DELETE FROM repeater_contacts WHERE public_key = ?', (public_key,))
                        deleted_counts['repeater_contacts'] = cursor.rowcount
                    except sqlite3.OperationalError:
                        # Table might not exist, that's okay
                        deleted_counts['repeater_contacts'] = 0

                    conn.commit()

                    # Log the deletion
                    self.logger.info(f"Contact deleted: {contact_name} ({public_key[:16]}...) - Role: {contact_role}, Device: {contact_device_type}")
                    self.logger.debug(f"Deleted counts: {deleted_counts}")

                    return jsonify({
                        'success': True,
                        'message': f'Contact "{contact_name}" has been deleted successfully',
                        'deleted_counts': deleted_counts
                    })

            except Exception as e:
                self.logger.error(f"Error deleting contact: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/contacts/purge-preview')
        def api_contacts_purge_preview():
            """Return count and sample of contacts not heard within the last N days."""
            days = request.args.get('days', 30, type=int)
            if days < 1:
                return jsonify({'error': 'days must be >= 1'}), 400
            try:
                with self._db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                    SELECT COUNT(*) AS cnt FROM complete_contact_tracking
                    WHERE last_heard < datetime('now', 'localtime', ? || ' days')
                ''', (f'-{days}',))
                    count = cursor.fetchone()['cnt']
                    cursor.execute('''
                    SELECT name, role, last_heard FROM complete_contact_tracking
                    WHERE last_heard < datetime('now', 'localtime', ? || ' days')
                    ORDER BY last_heard ASC
                    LIMIT 5
                ''', (f'-{days}',))
                    samples = [dict(r) for r in cursor.fetchall()]
                    return jsonify({'count': count, 'days': days, 'samples': samples})
            except Exception as e:
                self.logger.error(f"Error in purge preview: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/contacts/purge', methods=['POST'])
        def api_contacts_purge():
            """Delete all contacts not heard within the last N days."""
            data = request.get_json(silent=True) or {}
            days = data.get('days', 30)
            try:
                days = int(days)
            except (TypeError, ValueError):
                return jsonify({'error': 'days must be an integer'}), 400
            if days < 1:
                return jsonify({'error': 'days must be >= 1'}), 400
            try:
                with self._db_connection() as conn:
                    cursor = conn.cursor()
                    cutoff = f'-{days} days'
                    # Collect public_keys to purge so we can cascade
                    cursor.execute('''
                    SELECT public_key FROM complete_contact_tracking
                    WHERE last_heard < datetime('now', 'localtime', ?)
                ''', (cutoff,))
                    keys = [r['public_key'] for r in cursor.fetchall()]
                    if not keys:
                        return jsonify({'success': True, 'deleted': 0, 'message': 'No contacts matched the threshold'})
                    placeholders = ','.join('?' * len(keys))
                    cursor.execute(f'DELETE FROM complete_contact_tracking WHERE public_key IN ({placeholders})', keys)
                    deleted = cursor.rowcount
                    cursor.execute(f'DELETE FROM daily_stats WHERE public_key IN ({placeholders})', keys)
                    try:
                        cursor.execute(f'DELETE FROM repeater_contacts WHERE public_key IN ({placeholders})', keys)
                    except sqlite3.OperationalError:
                        pass
                    conn.commit()
                    self.logger.info(f"Purged {deleted} contact(s) not heard in {days}+ days")
                    return jsonify({'success': True, 'deleted': deleted,
                                    'message': f'Purged {deleted} contact(s) not heard in {days}+ days'})
            except Exception as e:
                self.logger.error(f"Error purging contacts: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/greeter')
        def api_greeter():
            """Get greeter data including rollout status, settings, and greeted users"""
            try:
                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    # Check if greeter tables exist
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='greeter_rollout'")
                    if not cursor.fetchone():
                        return jsonify({
                            'enabled': False,
                            'rollout_active': False,
                            'settings': {},
                            'greeted_users': [],
                            'error': 'Greeter tables not found'
                        })

                    # Get active rollout status
                    cursor.execute('''
                    SELECT id, rollout_started_at, rollout_days, rollout_completed,
                           datetime(rollout_started_at, '+' || rollout_days || ' days') as end_date,
                           datetime('now') as current_time
                    FROM greeter_rollout
                    WHERE rollout_completed = 0
                    ORDER BY rollout_started_at DESC
                    LIMIT 1
                ''')
                    rollout = cursor.fetchone()

                    rollout_active = False
                    rollout_data = None
                    time_remaining = None

                    if rollout:
                        rollout_id = rollout['id']
                        started_at_str = rollout['rollout_started_at']
                        rollout_days = rollout['rollout_days']
                        end_date_str = rollout['end_date']
                        current_time_str = rollout['current_time']

                        end_date = datetime.fromisoformat(end_date_str)
                        current_time = datetime.fromisoformat(current_time_str)

                        if current_time < end_date:
                            rollout_active = True
                            remaining_seconds = (end_date - current_time).total_seconds()
                            time_remaining = {
                                'days': int(remaining_seconds // 86400),
                                'hours': int((remaining_seconds % 86400) // 3600),
                                'minutes': int((remaining_seconds % 3600) // 60),
                                'seconds': int(remaining_seconds % 60),
                                'total_seconds': int(remaining_seconds)
                            }
                            rollout_data = {
                                'id': rollout_id,
                                'started_at': started_at_str,
                                'days': rollout_days,
                                'end_date': end_date_str
                            }

                    # Get greeter settings from config
                    settings = {
                        'enabled': self.config.getboolean('Greeter_Command', 'enabled', fallback=False),
                        'greeting_message': self.config.get('Greeter_Command', 'greeting_message',
                                                           fallback='Welcome to the mesh, {sender}!'),
                        'rollout_days': self.config.getint('Greeter_Command', 'rollout_days', fallback=7),
                        'include_mesh_info': self.config.getboolean('Greeter_Command', 'include_mesh_info',
                                                                   fallback=True),
                        'mesh_info_format': self.config.get('Greeter_Command', 'mesh_info_format',
                                                          fallback='\n\nMesh Info: {total_contacts} contacts, {repeaters} repeaters'),
                        'per_channel_greetings': self.config.getboolean('Greeter_Command', 'per_channel_greetings',
                                                                       fallback=False)
                    }

                    # Generate sample greeting — use str.replace() instead of .format()
                    # to avoid KeyError / info leaks from user-controlled templates
                    sample_greeting = settings['greeting_message'].replace('{sender}', 'SampleUser')
                    if settings['include_mesh_info']:
                        sample_mesh_info = (
                            settings['mesh_info_format']
                            .replace('{total_contacts}', '100')
                            .replace('{repeaters}', '5')
                            .replace('{companions}', '95')
                            .replace('{recent_activity_24h}', '10')
                        )
                        sample_greeting += sample_mesh_info

                    # Check if message_stats table exists for last seen data
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message_stats'")
                    has_message_stats = cursor.fetchone() is not None

                    # Get greeted users - use GROUP BY to ensure only one entry per (sender_id, channel)
                    # This handles any potential duplicates that might exist in the database
                    # We use MIN(greeted_at) to get the earliest (first) greeting time
                    # If per_channel_greetings is False, we'll still show one entry per user (channel will be NULL)
                    # If per_channel_greetings is True, we'll show one entry per user per channel
                    cursor.execute('''
                    SELECT sender_id, channel, MIN(greeted_at) as greeted_at,
                           MAX(rollout_marked) as rollout_marked
                    FROM greeted_users
                    GROUP BY sender_id, channel
                    ORDER BY MIN(greeted_at) DESC
                    LIMIT 500
                ''')
                    greeted_users_rows = cursor.fetchall()
                    greeted_users = []

                    for row in greeted_users_rows:
                        # Access row data - handle both dict-style (Row) and tuple access
                        try:
                            sender_id = row['sender_id'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[0]
                            channel_raw = row['channel'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[1]
                            greeted_at = row['greeted_at'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[2]
                            rollout_marked = row['rollout_marked'] if isinstance(row, dict) or hasattr(row, '__getitem__') else row[3]
                        except (KeyError, IndexError, TypeError) as e:
                            self.logger.error(f"Error accessing row data: {e}, row type: {type(row)}")
                            continue

                        sender_id = str(sender_id) if sender_id else ''
                        channel = str(channel_raw) if channel_raw else '(global)'

                        # Get last seen timestamp from message_stats if available
                        last_seen = None
                        if has_message_stats:
                            # Get the most recent channel message (not DM) for this user
                            # If per_channel_greetings is enabled, match the specific channel
                            # Otherwise, get the most recent message from any channel
                            if channel_raw:  # Use the raw channel value, not the formatted one
                                cursor.execute('''
                                SELECT MAX(timestamp) as last_seen
                                FROM message_stats
                                WHERE sender_id = ?
                                  AND channel = ?
                                  AND is_dm = 0
                                  AND channel IS NOT NULL
                            ''', (sender_id, channel_raw))
                            else:
                                # Global greeting - get last seen from any channel
                                cursor.execute('''
                                SELECT MAX(timestamp) as last_seen
                                FROM message_stats
                                WHERE sender_id = ?
                                  AND is_dm = 0
                                  AND channel IS NOT NULL
                            ''', (sender_id,))

                            result = cursor.fetchone()
                            if result and result['last_seen']:
                                last_seen = result['last_seen']

                        greeted_users.append({
                            'sender_id': sender_id,
                            'channel': channel,
                            'greeted_at': str(greeted_at),
                            'rollout_marked': bool(rollout_marked),
                            'last_seen': last_seen
                        })

                    return jsonify({
                        'enabled': settings['enabled'],
                        'rollout_active': rollout_active,
                        'rollout_data': rollout_data,
                        'time_remaining': time_remaining,
                        'settings': settings,
                        'sample_greeting': sample_greeting,
                        'greeted_users': greeted_users,
                        'total_greeted': len(greeted_users)
                    })

            except Exception as e:
                self.logger.error(f"Error getting greeter data: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/greeter/end-rollout', methods=['POST'])
        def api_end_rollout():
            """End the active onboarding period"""
            try:
                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    # Find active rollout
                    cursor.execute('''
                    SELECT id FROM greeter_rollout
                    WHERE rollout_completed = 0
                    ORDER BY rollout_started_at DESC
                    LIMIT 1
                ''')
                    rollout = cursor.fetchone()

                    if not rollout:
                        return jsonify({'success': False, 'error': 'No active rollout found'}), 404

                    rollout_id = rollout['id']

                    # Mark rollout as completed
                    cursor.execute('''
                    UPDATE greeter_rollout
                    SET rollout_completed = 1
                    WHERE id = ?
                ''', (rollout_id,))

                    conn.commit()

                    self.logger.info(f"Greeter rollout {rollout_id} ended manually via web viewer")

                    return jsonify({
                        'success': True,
                        'message': 'Onboarding period ended successfully'
                    })

            except Exception as e:
                self.logger.error(f"Error ending rollout: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/greeter/ungreet', methods=['POST'])
        def api_ungreet_user():
            """Mark a user as ungreeted (remove from greeted_users table)"""
            try:
                data = request.get_json()
                if not data or 'sender_id' not in data:
                    return jsonify({'error': 'sender_id is required'}), 400

                sender_id = data['sender_id']
                channel = data.get('channel')  # Optional - if None, removes global greeting

                with self._db_connection() as conn:
                    cursor = conn.cursor()

                    # Check if user exists
                    if channel and channel != '(global)':
                        cursor.execute('''
                        SELECT id FROM greeted_users
                        WHERE sender_id = ? AND channel = ?
                    ''', (sender_id, channel))
                    else:
                        cursor.execute('''
                        SELECT id FROM greeted_users
                        WHERE sender_id = ? AND channel IS NULL
                    ''', (sender_id,))

                    if not cursor.fetchone():
                        return jsonify({'error': 'User not found in greeted users'}), 404

                    # Delete the record
                    if channel and channel != '(global)':
                        cursor.execute('''
                        DELETE FROM greeted_users
                        WHERE sender_id = ? AND channel = ?
                    ''', (sender_id, channel))
                    else:
                        cursor.execute('''
                        DELETE FROM greeted_users
                        WHERE sender_id = ? AND channel IS NULL
                    ''', (sender_id,))

                    conn.commit()

                    self.logger.info(f"User {sender_id} marked as ungreeted (channel: {channel or 'global'})")

                    return jsonify({
                        'success': True,
                        'message': f'User {sender_id} marked as ungreeted'
                    })

            except Exception as e:
                self.logger.error(f"Error ungreeting user: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── Region warnings (regional flood scope) ───────────────────────────

        def _region_warning_channel_limit() -> int:
            """Channel body budget for a region warning sent on a channel.

            The device's own name is authoritative for the command layer, but
            the viewer is a separate process with no radio, so it falls back to
            the configured one. They match on any install where the bot manages
            the device name.

            The warning's scope can differ per channel (``flood_scope.<channel>``),
            and the page shows one number, so any regional scope that could
            apply costs the regional overhead. Under-promising here only means
            the operator writes a message that fits everywhere.
            """
            name = (self.config.get('Bot', 'bot_name', fallback='Bot') or 'Bot').strip()
            limit = channel_body_limit(name or 'Bot')
            candidates = []
            if self.config.has_section(region_warning.CONFIG_SECTION):
                candidates.append(
                    self.config.get(region_warning.CONFIG_SECTION, 'flood_scope', fallback='', raw=True)
                )
            if self.config.has_section('Channels'):
                explicit = (candidates[0] if candidates else '').strip()
                if not explicit:
                    for key, value in self.config.items('Channels', raw=True):
                        if key == 'outgoing_flood_scope_override' or key.startswith('flood_scope.'):
                            candidates.append(value)
            if any(not flood_scope.is_global_marker((c or '').strip()) for c in candidates):
                limit -= CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD
            return limit

        @self.app.route('/api/region-warnings')
        def api_region_warnings():
            """Settings, traffic tallies, budget and recent decisions for the page."""
            try:
                # Re-read from disk so the page reflects edits made elsewhere.
                self.config = self._load_merged_config()
                settings = region_warning.load_settings(self.config)
                try:
                    days = max(1, min(int(request.args.get('days', 14)), 90))
                except (TypeError, ValueError):
                    days = 14

                known_channels = []
                try:
                    known_channels = [
                        c.get('name') for c in self._get_channels() if c.get('name')
                    ]
                except Exception:
                    pass

                return jsonify({
                    'settings': region_warning.settings_to_config_values(settings),
                    'defaults': region_warning.settings_to_config_values(
                        region_warning.RegionWarningSettings()
                    ),
                    'default_message': region_warning.DEFAULT_MESSAGE,
                    'traffic': region_warning.traffic_summary(
                        self.db_manager, self.config, days, self.logger
                    ),
                    'series': region_warning.daily_series(
                        self.db_manager, self.config, days, self.logger
                    ),
                    'budget': region_warning.warning_budget(
                        self.db_manager, settings, self.config, self.logger
                    ),
                    'events': region_warning.recent_events(self.db_manager, 50),
                    'limits': {
                        'dm': region_warning.DM_BODY_LIMIT,
                        'channel': _region_warning_channel_limit(),
                    },
                    'known_channels': known_channels,
                })
            except Exception:
                self.logger.exception("Error building region warning view")
                return jsonify({'error': 'Internal error — see server logs'}), 500

        @self.app.route('/api/region-warnings/settings', methods=['POST'])
        def api_region_warnings_save():
            """Persist [Region_Warnings] and queue a hot config reload."""
            data = request.get_json(silent=True) or {}

            def _as_bool(key, default):
                raw = data.get(key, default)
                if isinstance(raw, bool):
                    return raw
                return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

            def _as_number(key, default, minimum=0.0, maximum=None):
                raw = data.get(key, default)
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    raise ValueError(f'{key} must be a number')
                if value < minimum:
                    raise ValueError(f'{key} must be at least {minimum:g}')
                if maximum is not None and value > maximum:
                    raise ValueError(f'{key} must be at most {maximum:g}')
                return value

            try:
                delivery = str(data.get('delivery', 'dm')).strip().lower()
                if delivery not in (region_warning.DELIVERY_DM, region_warning.DELIVERY_CHANNEL):
                    raise ValueError('delivery must be "dm" or "channel"')

                message = str(data.get('message') or '').strip() or region_warning.DEFAULT_MESSAGE
                if '\n' in message or '\r' in message:
                    raise ValueError('message must be a single line')
                if '%' in message:
                    # config.ini is read with configparser's interpolation on, so a
                    # bare % raises on every later read of the section — including
                    # the bot's own config validation, which would then reject every
                    # hot reload until someone hand-edited the file.
                    raise ValueError('message cannot contain "%"; write "percent" instead')
                if len(message) > 500:
                    # Far above the 158-byte send budget, but this lands in
                    # config.ini and in every timestamped backup of it.
                    raise ValueError('message must be 500 characters or fewer')

                channels = data.get('channels')
                if isinstance(channels, list):
                    channel_parts = channels
                else:
                    channel_parts = str(channels or '').split(',')
                normalized_channels = []
                for part in channel_parts:
                    name = region_warning.normalize_channel(part)
                    if name and name not in normalized_channels:
                        normalized_channels.append(name)

                settings = region_warning.RegionWarningSettings(
                    enabled=_as_bool('enabled', False),
                    dry_run=_as_bool('dry_run', True),
                    delivery=delivery,
                    channels=tuple(normalized_channels),
                    message=message,
                    min_unscoped_messages=int(_as_number('min_unscoped_messages', 3, 1, 100)),
                    per_sender_cooldown_hours=_as_number('per_sender_cooldown_hours', 168, 0, 8760),
                    mesh_cooldown_minutes=_as_number('mesh_cooldown_minutes', 30, 0, 10080),
                    max_warnings_per_day=int(_as_number('max_warnings_per_day', 6, 0, 1000)),
                    track_traffic=_as_bool('track_traffic', True),
                )
            except ValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400

            section = region_warning.CONFIG_SECTION
            target_path = (
                self.local_config_path
                if section in self._local_sections
                else self.config_path
            )
            try:
                store = get_settings_store(self.config, target_path, self.db_manager)
                result = store.write_values(
                    section, region_warning.settings_to_config_values(settings)
                )
            except IniValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400
            except Exception:
                self.logger.exception("Error saving region warning settings")
                return jsonify({'success': False, 'error': 'Internal error — see server logs'}), 500

            backup_path = result.get('backup_path', '') if isinstance(result, dict) else ''
            reload_queued = _queue_config_reload()

            self.logger.info(
                "Region warning settings saved (enabled=%s, dry_run=%s, delivery=%s)",
                settings.enabled, settings.dry_run, settings.delivery,
            )
            return jsonify({
                'success': True,
                'backup_path': backup_path,
                'reload_queued': reload_queued,
                'settings': region_warning.settings_to_config_values(settings),
            })

        # ── Region scopes ([Channels] flood_scopes) ──────────────────────────

        def _region_scope_target_path():
            """Where a [Channels] write lands: the local overlay wins if it has
            the section, because that is the copy the merged config reads last."""
            return (
                self.local_config_path
                if 'Channels' in self._local_sections
                else self.config_path
            )

        def _region_scope_view():
            """Effective region-scope settings, read the way the bot reads them."""
            # [Channels] is canonical; [Bot] is still honoured with a warning by
            # CommandManager, so read it the same way or the page would show
            # "replies to every scope" while the bot enforces an allowlist.
            raw = ''
            legacy_section = None
            for section in ('Channels', 'Bot'):
                if self.config.has_section(section) and self.config.has_option(
                    section, 'flood_scopes'
                ):
                    candidate = (self.config.get(section, 'flood_scopes') or '').strip()
                    if not candidate:
                        continue
                    raw = candidate
                    if section != 'Channels':
                        legacy_section = section
                    break

            scopes, allow_global = flood_scope.split_allowlist(raw)
            override_raw = ''
            if self.config.has_section('Channels') and self.config.has_option(
                'Channels', 'outgoing_flood_scope_override'
            ):
                override_raw = (
                    self.config.get('Channels', 'outgoing_flood_scope_override') or ''
                ).strip()
            override = (
                '' if flood_scope.is_global_marker(override_raw)
                else flood_scope.normalize_scope_name(override_raw)
            )

            # Read-only, but it is the answer to "why does that channel ignore
            # the default?", so the page shows it rather than making the
            # operator open config.ini to find out.
            channel_overrides = []
            if self.config.has_section('Channels'):
                for key, value in self.config.items('Channels'):
                    if not key.startswith('flood_scope.') or len(key) <= len('flood_scope.'):
                        continue
                    configured = (value or '').strip()
                    channel_overrides.append({
                        'channel': key[len('flood_scope.'):],
                        'scope': (
                            '' if flood_scope.is_global_marker(configured)
                            else flood_scope.normalize_scope_name(configured)
                        ),
                    })
            channel_overrides.sort(key=lambda entry: entry['channel'].lower())

            target = _region_scope_target_path()
            if target == self.local_config_path:
                target_label = os.path.join(
                    os.path.basename(os.path.dirname(target)), os.path.basename(target)
                )
            else:
                target_label = os.path.basename(target)

            return {
                'allowlist_active': bool(scopes or allow_global),
                'scopes': scopes,
                'allow_global': allow_global,
                'outgoing_override': override,
                'channel_overrides': channel_overrides,
                'legacy_section': legacy_section,
                'target': target_label,
                'max_name_length': flood_scope.MAX_SCOPE_NAME_LENGTH,
            }

        @self.app.route('/api/region-scopes')
        def api_region_scopes_get():
            """Regional flood scopes from [Channels], as the bot resolves them."""
            try:
                # Re-read from disk so the page reflects edits made elsewhere,
                # and so the local-overlay target is resolved against what is
                # on disk now rather than at viewer startup.
                self.config = self._load_merged_config()
                return jsonify(_region_scope_view())
            except Exception:
                self.logger.exception("Error reading region scopes")
                return jsonify({'error': 'Internal error — see server logs'}), 500

        @self.app.route('/api/region-scopes', methods=['POST'])
        def api_region_scopes_save():
            """Persist [Channels] flood_scopes / outgoing_flood_scope_override."""
            data = request.get_json(silent=True) or {}

            def _as_bool(key, default=False):
                raw = data.get(key, default)
                if isinstance(raw, bool):
                    return raw
                return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

            try:
                submitted = data.get('scopes')
                if isinstance(submitted, (list, tuple)):
                    entries = [str(part).strip() for part in submitted if str(part).strip()]
                else:
                    entries = flood_scope.parse_scope_list(submitted)

                allow_global = _as_bool('allow_global', False)
                scopes: list[str] = []
                for entry in entries:
                    canonical = flood_scope.validate_scope_name(entry)
                    # '*' typed into the list box means the same thing as the
                    # checkbox; fold it in rather than writing it twice.
                    if flood_scope.is_global_marker(canonical):
                        allow_global = True
                    elif canonical not in scopes:
                        scopes.append(canonical)

                allowlist_enabled = _as_bool('allowlist_enabled', bool(scopes or allow_global))
                if allowlist_enabled and not scopes and not allow_global:
                    raise ValueError(
                        'Add at least one region scope, or turn the allowlist off '
                        'so the bot replies whatever the scope'
                    )

                # '*' last, matching the order config.ini.example documents.
                flood_scopes_value = (
                    flood_scope.format_scope_list(scopes + (['*'] if allow_global else []))
                    if allowlist_enabled else ''
                )

                override_raw = str(data.get('outgoing_override') or '').strip()
                # Every global marker means the same send path, but only the
                # empty value keeps send_channel_message from logging "override
                # was not applied" on each global send. Store the quiet one.
                override_value = (
                    '' if flood_scope.is_global_marker(override_raw)
                    else flood_scope.validate_scope_name(override_raw)
                )
            except ValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400

            try:
                # Resolve the write target against the config on disk now: the
                # local overlay may have grown a [Channels] section since the
                # viewer started, and it would silently win over a base write.
                self.config = self._load_merged_config()
                store = get_settings_store(
                    self.config, _region_scope_target_path(), self.db_manager
                )
                result = store.write_values('Channels', {
                    'flood_scopes': flood_scopes_value,
                    'outgoing_flood_scope_override': override_value,
                })
            except IniValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400
            except OSError:
                self.logger.exception("Error writing region scopes")
                return jsonify({
                    'success': False,
                    'error': 'Could not write config.ini — check file permissions',
                }), 500
            except Exception:
                self.logger.exception("Error saving region scopes")
                return jsonify({'success': False, 'error': 'Internal error — see server logs'}), 500

            backup_path = result.get('backup_path', '') if isinstance(result, dict) else ''
            reload_op_id = _queue_config_reload_id()

            self.logger.info(
                "Region scopes saved: flood_scopes=%r outgoing_flood_scope_override=%r",
                flood_scopes_value, override_value,
            )
            return jsonify({
                'success': True,
                'backup_path': backup_path,
                'reload_queued': reload_op_id is not None,
                'reload_operation_id': reload_op_id,
                'settings': _region_scope_view(),
            })

        # Feed management API endpoints
        def _schedule_tz():
            from modules.utils import get_config_timezone
            tz, _name = get_config_timezone(self.config, self.logger)
            return tz

        def _queue_config_reload_id():
            """Queue a config reload and return its operation id, or None.

            The id lets a caller poll /api/channel-operations/<id> and report
            what the bot actually did with the edit, instead of claiming
            success because a row was inserted.
            """
            try:
                return self._queue_operation('config_reload')
            except Exception:
                self.logger.exception("Failed to queue config reload")
                return None

        def _queue_config_reload():
            """Ask the bot to re-read config.ini; it re-registers scheduled jobs."""
            return _queue_config_reload_id() is not None

        # The duplicate check and the write have to be one critical section, or two
        # concurrent creates for the same schedule both pass the check and the second
        # silently replaces the first instead of getting the promised 409.
        schedule_write_lock = threading.Lock()

        def _existing_schedules():
            return {e['schedule'] for e in read_entries(self.config_path, _schedule_tz())}

        @self.app.route('/api/scheduled-messages')
        @self._api_errors('Error reading scheduled messages')
        def api_scheduled_messages():
            """List scheduled messages with their next run times."""
            return jsonify({'entries': read_entries(self.config_path, _schedule_tz())})

        @self.app.route('/api/scheduled-messages/preview', methods=['POST'])
        @self._api_errors('Error previewing schedule')
        def api_scheduled_messages_preview():
            """Validate a schedule and return its next run times (powers the builder)."""
            data = request.get_json(silent=True) or {}
            try:
                count = int(data.get('count', 5))
            except (TypeError, ValueError):
                return jsonify({'error': 'count must be an integer'}), 400
            if not 1 <= count <= 20:
                return jsonify({'error': 'count must be between 1 and 20'}), 400
            return jsonify(describe_schedule(
                data.get('schedule', ''),
                _schedule_tz(),
                message=data.get('message', ''),
                count=count,
            ))

        def _save_scheduled_message(data, *, replacing=None):
            """Shared create/update: validate, write config.ini, queue a reload."""
            with schedule_write_lock:
                return _save_scheduled_message_locked(data, replacing=replacing)

        def _save_scheduled_message_locked(data, *, replacing=None):
            schedule = (data.get('schedule') or '').strip()
            channel = (data.get('channel') or '').strip()
            message = (data.get('message') or '').strip()
            scope = (data.get('scope') or '').strip() or None

            field_error = validate_entry(channel, message, scope)
            if field_error:
                return jsonify({'success': False, 'error': field_error}), 400

            described = describe_schedule(schedule, _schedule_tz(), message=message)
            if not described.get('valid'):
                return jsonify({'success': False, 'error': described.get('error')}), 400

            existing = _existing_schedules()

            # Checked here rather than in the route so it shares this snapshot and the
            # surrounding lock.
            if replacing is not None and replacing not in existing:
                return jsonify({
                    'success': False,
                    'error': f"No scheduled message for '{replacing}'",
                }), 404

            # Schedules are INI keys, so two entries cannot share one. Renaming onto
            # another entry's key would silently overwrite it.
            if schedule in existing and schedule != replacing:
                return jsonify({
                    'success': False,
                    'error': (
                        f"A scheduled message already exists for '{schedule}'. "
                        "Edit that one, or use a different schedule."
                    ),
                }), 409

            updates = {SCHEDULED_MESSAGES_SECTION: {schedule: compose_value(channel, message, scope)}}
            deletes = None
            if replacing and replacing != schedule:
                deletes = {SCHEDULED_MESSAGES_SECTION: [replacing]}

            try:
                summary = update_ini_values(self.config_path, updates, deletes)
            except IniValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400
            except OSError as exc:
                self.logger.error("Failed to write scheduled message: %s", exc)
                return jsonify({
                    'success': False,
                    'error': 'Could not write config.ini — check file permissions',
                }), 500

            reloaded = _queue_config_reload()
            if not reloaded:
                # Written to disk but the running bot still has the old jobs. Saying
                # "saved" alone would imply it is live.
                self.logger.warning(
                    "Scheduled message written but the config reload could not be queued"
                )
            self.logger.info(
                "Scheduled message saved: %r -> %s:%s (backup=%s)",
                schedule, channel, message[:40],
                os.path.basename(summary.get('backup_path') or '') or 'none',
            )
            return jsonify({
                'success': True,
                'reload_queued': reloaded,
                'message': (
                    'Saved. The bot reloads its schedule within a few seconds.'
                    if reloaded else
                    'Saved to config.ini, but the bot could not be told to reload. '
                    'It will pick the change up on next restart.'
                ),
                'entry': {'schedule': schedule, 'channel': channel,
                          'scope': scope, 'message': message, **described},
            })

        @self.app.route('/api/scheduled-messages', methods=['POST'])
        def api_create_scheduled_message():
            """Create a scheduled message."""
            try:
                return _save_scheduled_message(request.get_json(silent=True) or {})
            except Exception as e:
                self.logger.error(f"Error creating scheduled message: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/scheduled-messages', methods=['PUT'])
        def api_update_scheduled_message():
            """Update a scheduled message, including changing its schedule."""
            try:
                data = request.get_json(silent=True) or {}
                original = (data.get('original_schedule') or '').strip()
                if not original:
                    return jsonify({'success': False, 'error': 'original_schedule is required'}), 400
                # Existence is verified inside the lock, against the same snapshot the
                # duplicate check uses: a concurrent delete between an outside check and
                # the write would otherwise resurrect the entry as a new one.
                return _save_scheduled_message(data, replacing=original)
            except Exception as e:
                self.logger.error(f"Error updating scheduled message: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/scheduled-messages', methods=['DELETE'])
        def api_delete_scheduled_message():
            """Delete a scheduled message."""
            try:
                data = request.get_json(silent=True) or {}
                schedule = (data.get('schedule') or '').strip()
                if not schedule:
                    return jsonify({'success': False, 'error': 'schedule is required'}), 400
                with schedule_write_lock:
                    if schedule not in _existing_schedules():
                        return jsonify({
                            'success': False,
                            'error': f"No scheduled message for '{schedule}'",
                        }), 404
                    try:
                        update_ini_values(
                            self.config_path, {}, {SCHEDULED_MESSAGES_SECTION: [schedule]}
                        )
                    except OSError as exc:
                        self.logger.error("Failed to delete scheduled message: %s", exc)
                        return jsonify({
                            'success': False,
                            'error': 'Could not write config.ini — check file permissions',
                        }), 500
                reloaded = _queue_config_reload()
                self.logger.info("Scheduled message deleted: %r", schedule)
                return jsonify({
                    'success': True,
                    'reload_queued': reloaded,
                    'message': (
                        'Deleted.' if reloaded else
                        'Deleted from config.ini, but the bot could not be told to '
                        'reload. It will stop sending after the next restart.'
                    ),
                })
            except Exception as e:
                self.logger.error(f"Error deleting scheduled message: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/feeds')
        @self._api_errors('Error getting feeds')
        def api_feeds():
            """Get all feed subscriptions with statistics"""
            feeds = self._get_feed_subscriptions()
            return jsonify(feeds)

        @self.app.route('/api/feeds/<int:feed_id>')
        @self._api_errors('Error getting feed detail')
        def api_feed_detail(feed_id):
            """Get detailed information about a specific feed"""
            feed = self._get_feed_subscription(feed_id)
            if not feed:
                return jsonify({'error': 'Feed not found'}), 404

            # Get activity and errors
            activity = self._get_feed_activity(feed_id)
            errors = self._get_feed_errors(feed_id)

            feed['activity'] = activity
            feed['errors'] = errors

            return jsonify(feed)

        @self.app.route('/api/feeds', methods=['POST'])
        @self._api_errors('Error creating feed')
        def api_create_feed():
            """Create a new feed subscription"""
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No data provided'}), 400

            feed_id = self._create_feed_subscription(data)
            return jsonify({'success': True, 'id': feed_id})

        @self.app.route('/api/feeds/<int:feed_id>', methods=['PUT'])
        @self._api_errors('Error updating feed')
        def api_update_feed(feed_id):
            """Update an existing feed subscription"""
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No data provided'}), 400

            success = self._update_feed_subscription(feed_id, data)
            if not success:
                return jsonify({'error': 'Feed not found'}), 404

            return jsonify({'success': True})

        @self.app.route('/api/feeds/<int:feed_id>', methods=['DELETE'])
        @self._api_errors('Error deleting feed')
        def api_delete_feed(feed_id):
            """Delete a feed subscription"""
            success = self._delete_feed_subscription(feed_id)
            if not success:
                return jsonify({'error': 'Feed not found'}), 404

            return jsonify({'success': True})

        @self.app.route('/api/feeds/default-format', methods=['GET'])
        def api_get_default_format():
            """Get the default output format from config"""
            try:
                default_format = self.config.get('Feed_Manager', 'default_output_format',
                                                fallback='{emoji} {body|truncate:100} - {date}\n{link|truncate:50}')
                return jsonify({'default_format': default_format})
            except Exception as e:
                self.logger.error(f"Error getting default format: {e}")
                return jsonify({'default_format': '{emoji} {body|truncate:100} - {date}\n{link|truncate:50}'})

        @self.app.route('/api/feeds/preview', methods=['POST'])
        @self._api_errors('Error previewing feed')
        def api_preview_feed():
            """Preview feed items with custom output format"""
            data = request.get_json()
            if not data or 'feed_url' not in data:
                return jsonify({'error': 'feed_url is required'}), 400

            feed_url = data['feed_url']
            feed_type = data.get('feed_type', 'rss')
            output_format = data.get('output_format', '')
            api_config = data.get('api_config', {})
            filter_config = data.get('filter_config')
            sort_config = data.get('sort_config')

            # Get default format from config if not provided
            if not output_format:
                output_format = self.config.get('Feed_Manager', 'default_output_format',
                                               fallback='{emoji} {body|truncate:100} - {date}\n{link|truncate:50}')

            # Fetch and format feed items
            try:
                preview_items = self._preview_feed_items(feed_url, feed_type, output_format, api_config, filter_config, sort_config)
            except ValueError as e:
                # SSRF validation error - return 400
                return jsonify({'error': str(e)}), 400

            return jsonify({
                'success': True,
                'items': preview_items
            })

        @self.app.route('/api/feeds/test', methods=['POST'])
        @self._api_errors('Error testing feed')
        def api_test_feed():
            """Test a feed URL and return preview of recent items"""
            data = request.get_json()
            if not data or 'url' not in data:
                return jsonify({'error': 'URL is required'}), 400

            url = data['url']

            # Validate URL for SSRF protection
            if self.config.has_section('Feed_Command'):
                try:
                    feed_command_allow_private = self.config.getboolean(
                        'Feed_Command', 'allow_private_urls', fallback=False
                    )
                except ValueError:
                    feed_command_allow_private = False
            else:
                feed_command_allow_private = False
            allow_private_feeds = (
                self.config.getboolean(
                    'Feed_Manager',
                    'allow_private_urls',
                    fallback=feed_command_allow_private,
                )
                if self.config.has_section('Feed_Manager')
                else feed_command_allow_private
            )
            if not validate_external_url(url, allow_private=allow_private_feeds):
                return jsonify({'error': 'Invalid or unsafe URL'}), 400

            return jsonify({'success': True, 'message': 'URL validated'})

        @self.app.route('/api/feeds/stats')
        @self._api_errors('Error getting feed stats')
        def api_feed_stats():
            """Get aggregate feed statistics"""
            stats = self._get_feed_statistics()
            return jsonify(stats)

        @self.app.route('/api/feeds/<int:feed_id>/activity')
        @self._api_errors('Error getting feed activity')
        def api_feed_activity(feed_id):
            """Get activity log for a specific feed"""
            activity = self._get_feed_activity(feed_id, limit=50)
            return jsonify({'activity': activity})

        @self.app.route('/api/feeds/<int:feed_id>/errors')
        @self._api_errors('Error getting feed errors')
        def api_feed_errors(feed_id):
            """Get error history for a specific feed"""
            errors = self._get_feed_errors(feed_id, limit=20)
            return jsonify({'errors': errors})

        @self.app.route('/api/feeds/errors/reset', methods=['POST'])
        @self._api_errors('Error resetting feed errors')
        def api_reset_all_feed_errors():
            """Clear recorded errors for every feed"""
            deleted = self._reset_feed_errors()
            return jsonify({'success': True, 'deleted': deleted})

        @self.app.route('/api/feeds/<int:feed_id>/errors/reset', methods=['POST'])
        @self._api_errors('Error resetting feed errors')
        def api_reset_feed_errors(feed_id):
            """Clear recorded errors for a single feed"""
            deleted = self._reset_feed_errors(feed_id)
            return jsonify({'success': True, 'deleted': deleted})

        @self.app.route('/api/feeds/<int:feed_id>/refresh', methods=['POST'])
        @self._api_errors('Error refreshing feed')
        def api_refresh_feed(feed_id):
            """Manually trigger a feed check"""
            # This would trigger feed_manager to poll this feed immediately
            # For now, just acknowledge the request
            return jsonify({'success': True, 'message': 'Feed refresh queued'})

        # Channel management API endpoints
        @self.app.route('/api/channels')
        @self._api_errors('Error getting channels')
        def api_channels():
            """Get all configured channels"""
            channels = self._get_channels()
            return jsonify({'channels': channels})

        @self.app.route('/api/channels', methods=['POST'])
        @self._api_errors('Error creating channel')
        def api_create_channel():
            """Create a new channel (hashtag or custom)"""
            data = request.get_json()
            if not data or 'name' not in data:
                return jsonify({'error': 'Channel name is required'}), 400

            channel_name = data.get('name', '').strip()
            channel_idx = data.get('channel_idx')
            channel_key = data.get('channel_key', '').strip()

            if not channel_name:
                return jsonify({'error': 'Channel name cannot be empty'}), 400

            # If channel_idx not provided, find the lowest available index
            if channel_idx is None:
                channel_idx = self._get_lowest_available_channel_index()
                if channel_idx is None:
                    max_channels = self.config.getint('Bot', 'max_channels', fallback=40)
                    return jsonify({'error': f'No available channel slots. All {max_channels} channels are in use.'}), 400

            # Determine if it's a hashtag channel
            is_hashtag = channel_name.startswith('#')

            # Validate custom channel has key
            if not is_hashtag and not channel_key:
                return jsonify({'error': 'Channel key is required for custom channels (channels without # prefix)'}), 400

            # Validate key format if provided
            if channel_key:
                if len(channel_key) != 32:
                    return jsonify({'error': 'Channel key must be exactly 32 hexadecimal characters'}), 400
                if not all(c in '0123456789abcdefABCDEF' for c in channel_key):
                    return jsonify({'error': 'Channel key must contain only hexadecimal characters (0-9, a-f, A-F)'}), 400

            # Try to create channel via bot's channel manager
            result = self._add_channel_for_web(channel_idx, channel_name, channel_key if not is_hashtag else None)

            if result.get('success'):
                if result.get('pending'):
                    # Operation is queued, return operation_id for polling
                    return jsonify({
                        'success': True,
                        'pending': True,
                        'operation_id': result.get('operation_id'),
                        'message': result.get('message', 'Channel operation queued')
                    })
                else:
                    return jsonify({'success': True, 'message': 'Channel created successfully'})
            else:
                return jsonify({'error': result.get('error', 'Failed to create channel')}), 500

        @self.app.route('/api/channels/<int:channel_idx>', methods=['DELETE'])
        @self._api_errors('Error deleting channel')
        def api_delete_channel(channel_idx):
            """Remove a channel"""
            result = self._remove_channel_for_web(channel_idx)
            if result.get('success'):
                if result.get('pending'):
                    # Operation is queued, return operation_id for polling
                    return jsonify({
                        'success': True,
                        'pending': True,
                        'operation_id': result.get('operation_id'),
                        'message': result.get('message', 'Channel operation queued')
                    })
                else:
                    return jsonify({'success': True, 'message': 'Channel deleted successfully'})
            else:
                return jsonify({'error': result.get('error', 'Failed to delete channel')}), 500

        @self.app.route('/api/channel-operations/<int:operation_id>', methods=['GET'])
        def api_get_operation_status(operation_id):
            """Get status of a channel operation"""
            try:
                with self._db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                    SELECT status, error_message, result_data, processed_at, claimed_at
                    FROM channel_operations
                    WHERE id = ?
                ''', (operation_id,))

                    result = cursor.fetchone()

                    if not result:
                        return jsonify({'error': 'Operation not found'}), 404

                    status, error_msg, result_data, processed_at, claimed_at = result

                    return jsonify({
                        'operation_id': operation_id,
                        'status': status,
                        'error_message': error_msg,
                        'claimed_at': claimed_at,
                        'processed_at': processed_at,
                        'result_data': json.loads(result_data) if result_data else None
                    })
            except Exception as e:
                self.logger.error(f"Error getting operation status: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/channels/validate', methods=['POST'])
        @self._api_errors('Error validating channel')
        def api_validate_channel():
            """Validate if a channel exists or can be created"""
            data = request.get_json()
            if not data or 'name' not in data:
                return jsonify({'error': 'Channel name is required'}), 400

            channel_name = data['name']
            # Check if channel exists
            channel_num = self._get_channel_number(channel_name)

            return jsonify({
                'exists': channel_num is not None,
                'channel_num': channel_num
            })

        @self.app.route('/api/channels/<int:channel_idx>', methods=['PUT'])
        @self._api_errors('Error updating channel')
        def api_update_channel(channel_idx):
            """Update channel name or configuration"""
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No data provided'}), 400

            # This would use channel_manager
            return jsonify({'success': True, 'message': 'Channel update requires bot connection'})

        @self.app.route('/api/channels/stats')
        @self._api_errors('Error getting channel stats')
        def api_channel_stats():
            """Get channel statistics and usage data"""
            stats = self._get_channel_statistics()
            return jsonify(stats)

        @self.app.route('/api/channels/<int:channel_idx>/feeds')
        @self._api_errors('Error getting channel feeds')
        def api_channel_feeds(channel_idx):
            """Get all feed subscriptions for a specific channel"""
            feeds = self._get_feeds_by_channel(channel_idx)
            return jsonify({'feeds': feeds})

        @self.app.route('/api/radio/status')
        @self._api_errors('Error getting radio status')
        def api_radio_status():
            """Current radio connection state from bot_metadata."""
            value = self.db_manager.get_metadata('radio_connected')
            connected = value == '1' if value is not None else None
            return jsonify({'connected': connected, 'status_known': value is not None})

        @self.app.route('/api/radio/reboot', methods=['POST'])
        @self._api_errors('Error queuing radio reboot')
        def api_radio_reboot():
            """Queue a radio reboot (disconnect + reconnect)."""
            op_id = self._queue_operation('radio_reboot')
            return jsonify({'success': True, 'operation_id': op_id, 'message': 'Radio reboot queued'})

        @self.app.route('/api/radio/connect', methods=['POST'])
        @self._api_errors('Error queuing radio connect/disconnect')
        def api_radio_connect():
            """Queue radio connect or disconnect. Body: {'action': 'connect'|'disconnect'}"""
            data = request.get_json(silent=True) or {}
            action = data.get('action', '')
            if action not in ('connect', 'disconnect'):
                return jsonify({'error': "action must be 'connect' or 'disconnect'"}), 400
            op_type = 'radio_connect' if action == 'connect' else 'radio_disconnect'
            op_id = self._queue_operation(op_type)
            return jsonify({'success': True, 'pending': True, 'operation_id': op_id})

        @self.app.route('/api/radio/firmware/config/read', methods=['POST'])
        @self._api_errors('Error queuing firmware read')
        def api_firmware_config_read():
            """Queue a firmware config read (path hash mode). Poll /api/channel-operations/<id>."""
            op_id = self._queue_operation('firmware_read')
            return jsonify({'success': True, 'operation_id': op_id})

        @self.app.route('/api/radio/firmware/config/write', methods=['POST'])
        @self._api_errors('Error queuing firmware write')
        def api_firmware_config_write():
            """Queue a firmware config write. Body may carry ``path_hash_mode``
            and/or ``default_flood_scope`` (a region name, or empty/null to
            clear the radio's default). Poll /api/channel-operations/<id>."""
            data = request.get_json(silent=True) or {}
            allowed = {'path_hash_mode', 'default_flood_scope'}
            payload = {k: v for k, v in data.items() if k in allowed}
            if not payload:
                return jsonify({
                    'error': 'No valid fields provided '
                             '(path_hash_mode, default_flood_scope)'
                }), 400
            if 'path_hash_mode' in payload:
                mode = int(payload['path_hash_mode'])
                if not (0 <= mode <= 2):
                    return jsonify({'error': 'path_hash_mode must be 0-2 (bytes per hop = mode + 1)'}), 400
                payload['path_hash_mode'] = mode
            if 'default_flood_scope' in payload:
                raw = str(payload['default_flood_scope'] or '').strip()
                try:
                    # A global marker means "no default scope", which the
                    # radio spells as a cleared field, so both arrive here
                    # as the empty string.
                    canonical = (
                        '' if flood_scope.is_global_marker(raw)
                        else flood_scope.validate_device_scope_name(raw)
                    )
                except ValueError as exc:
                    return jsonify({'error': str(exc)}), 400
                payload['default_flood_scope'] = canonical
            op_id = self._queue_operation('firmware_write', payload)
            return jsonify({'success': True, 'operation_id': op_id})

        @self.app.route('/api/radio/params', methods=['GET'])
        @self._api_errors('Error queuing radio params read')
        def api_radio_params_read():
            """Queue a radio parameter read (freq, bw, sf, cr, tx_power). Poll /api/channel-operations/<id>."""
            op_id = self._queue_operation('radio_params_read')
            return jsonify({'success': True, 'operation_id': op_id})

        @self.app.route('/api/radio/params', methods=['POST'])
        @self._api_errors('Error queuing radio params write')
        def api_radio_params_write():
            """Queue a radio/node parameter write. Body may mix: freq/bw/sf/cr
            (together), tx_power, name, lat/lon (together), adv_loc_policy,
            multi_acks, telemetry_mode_base/loc/env, and rx_delay/airtime_factor
            (together). manual_add_contacts is deliberately not writable here —
            it is owned by [Bot] auto_manage_contacts in config.ini.
            Poll /api/channel-operations/<id> for result."""
            data = request.get_json(silent=True) or {}
            allowed = {
                'freq', 'bw', 'sf', 'cr', 'tx_power',
                'name', 'lat', 'lon', 'adv_loc_policy',
                'multi_acks',
                'telemetry_mode_base', 'telemetry_mode_loc', 'telemetry_mode_env',
                'rx_delay', 'airtime_factor',
            }
            payload = {k: v for k, v in data.items() if k in allowed}
            if not payload:
                return jsonify({'error': f"No valid fields (expected one of: {', '.join(sorted(allowed))})"}), 400

            if 'freq' in payload:
                freq = float(payload['freq'])
                if not (100.0 <= freq <= 1700.0):
                    return jsonify({'error': 'freq must be 100–1700 MHz'}), 400
                payload['freq'] = freq
            if 'bw' in payload:
                bw = float(payload['bw'])
                if bw not in (62.5, 125.0, 250.0, 500.0):
                    return jsonify({'error': 'bw must be 62.5, 125, 250, or 500 kHz'}), 400
                payload['bw'] = bw
            if 'sf' in payload:
                sf = int(payload['sf'])
                if not (5 <= sf <= 12):
                    return jsonify({'error': 'sf must be 5–12'}), 400
                payload['sf'] = sf
            if 'cr' in payload:
                cr = int(payload['cr'])
                if not (5 <= cr <= 8):
                    return jsonify({'error': 'cr must be 5–8'}), 400
                payload['cr'] = cr
            if 'tx_power' in payload:
                tx = int(payload['tx_power'])
                if not (1 <= tx <= 30):
                    return jsonify({'error': 'tx_power must be 1–30 dBm'}), 400
                payload['tx_power'] = tx
            if 'name' in payload:
                name = str(payload['name']).strip()
                if not name or len(name.encode('utf-8')) > 32:
                    return jsonify({'error': 'name must be 1–32 bytes'}), 400
                payload['name'] = name
            if ('lat' in payload) != ('lon' in payload):
                return jsonify({'error': 'lat and lon must be provided together'}), 400
            if 'lat' in payload:
                lat = float(payload['lat'])
                lon = float(payload['lon'])
                if not (-90.0 <= lat <= 90.0):
                    return jsonify({'error': 'lat must be -90 to 90'}), 400
                if not (-180.0 <= lon <= 180.0):
                    return jsonify({'error': 'lon must be -180 to 180'}), 400
                payload['lat'] = lat
                payload['lon'] = lon
            if 'adv_loc_policy' in payload:
                policy = int(payload['adv_loc_policy'])
                if policy not in (0, 1):
                    return jsonify({'error': 'adv_loc_policy must be 0 (private) or 1 (share)'}), 400
                payload['adv_loc_policy'] = policy
            if 'multi_acks' in payload:
                acks = int(payload['multi_acks'])
                if not (0 <= acks <= 3):
                    return jsonify({'error': 'multi_acks must be 0–3'}), 400
                payload['multi_acks'] = acks
            for telem_key in ('telemetry_mode_base', 'telemetry_mode_loc', 'telemetry_mode_env'):
                if telem_key in payload:
                    mode = int(payload[telem_key])
                    if not (0 <= mode <= 2):
                        return jsonify({'error': f'{telem_key} must be 0 (deny), 1 (per-contact), or 2 (allow all)'}), 400
                    payload[telem_key] = mode
            if ('rx_delay' in payload) != ('airtime_factor' in payload):
                return jsonify({'error': 'rx_delay and airtime_factor must be provided together'}), 400
            if 'rx_delay' in payload:
                rx_delay = float(payload['rx_delay'])
                airtime_factor = float(payload['airtime_factor'])
                if not (0.0 <= rx_delay <= 20.0):
                    return jsonify({'error': 'rx_delay must be 0–20 seconds'}), 400
                if not (0.0 <= airtime_factor <= 9.0):
                    return jsonify({'error': 'airtime_factor must be 0–9'}), 400
                payload['rx_delay'] = rx_delay
                payload['airtime_factor'] = airtime_factor

            radio_fields = {'freq', 'bw', 'sf', 'cr'}
            if radio_fields & set(payload) and not radio_fields <= set(payload):
                return jsonify({'error': 'freq, bw, sf, and cr must all be provided together'}), 400

            op_id = self._queue_operation('radio_params_write', payload)
            return jsonify({'success': True, 'operation_id': op_id})

        @self.app.route('/api/radio/advert', methods=['POST'])
        @self._api_errors('Error queuing radio advert')
        def api_radio_advert():
            """Queue a self-advertisement. Body: {flood: bool} (default false =
            zero-hop). Poll /api/channel-operations/<id> for result."""
            data = request.get_json(silent=True) or {}
            flood = data.get('flood', False)
            if not isinstance(flood, bool):
                return jsonify({'error': 'flood must be true or false'}), 400
            op_id = self._queue_operation('radio_advert', {'flood': flood})
            return jsonify({'success': True, 'operation_id': op_id})
        @self.app.route('/api/admin/clock-sync-admin/run-now', methods=['POST'])
        def api_admin_clock_sync_admin_run_now():
            """Queue an immediate Clock_Sync_Admin DM run outside the normal schedule.

            The web viewer runs in a separate process from the bot (no live access to
            its scheduler), so this goes through the same channel_operations queue the
            radio/firmware/channel actions use. Poll /api/channel-operations/<id>.
            """
            try:
                if not self.config.getboolean('Clock_Sync_Admin', 'enabled', fallback=False):
                    return jsonify({'success': False, 'error': 'Clock_Sync_Admin is disabled in config.ini'}), 400
                with self.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO channel_operations (operation_type, status) VALUES ('clock_sync_admin_run_now', 'pending')"
                    )
                    conn.commit()
                    op_id = cursor.lastrowid
                return jsonify({'success': True, 'operation_id': op_id})
            except Exception as e:
                self.logger.error(f"Error queuing clock_sync_admin run-now: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500
        @self.app.route('/api/battery/<public_key>/history')
        def api_battery_history(public_key):
            """Battery voltage points for one device over a rolling window
            (default 7 days), for the contacts-page battery badge's history
            chart. JSON shape: {"public_key": ..., "days": N,
            "points": [{"observed_at": str, "voltage": float}, ...]}."""
            try:
                days = int(request.args.get('days', 7))
            except (TypeError, ValueError):
                days = 7
            days = max(1, min(days, 30))
            try:
                with self._with_db_connection() as conn:
                    rows = conn.execute(
                        """
                        SELECT observed_at, voltage FROM battery_observations
                        WHERE public_key = ?
                            AND observed_at >= strftime('%Y-%m-%d %H:%M:%S', 'now', ?)
                        ORDER BY observed_at
                        """,
                        (public_key, f'-{days} days'),
                    ).fetchall()
                points = [{'observed_at': row['observed_at'], 'voltage': row['voltage']} for row in rows]
                return jsonify({'public_key': public_key, 'days': days, 'points': points})
            except Exception as e:
                self.logger.error(f"Error reading battery history for {public_key[:12]}...: {e}")
                return jsonify({'error': str(e)}), 500
        @self.app.route('/api/clock-sync-targets')
        def api_clock_sync_targets():
            """Get Clock_Sync_Admin targets with their synchronization status"""
            try:
                targets_data = self._get_clock_sync_targets_status()
                return jsonify(targets_data)
            except Exception as e:
                self.logger.error(f"Error getting clock sync targets: {e}")
                return jsonify({'error': 'Failed to retrieve clock sync targets'}), 500
        @self.app.route('/api/clock-sync-targets-admin', methods=['GET'])
        def api_clock_sync_targets_admin_list():
            """List clock sync targets from DB with enriched details"""
            try:
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, target, enabled, created_at, auto_clkreboot_enabled, last_clkreboot_at "
                        "FROM clock_sync_targets ORDER BY id"
                    )
                    rows = cursor.fetchall()
                    targets = []
                    for r in rows:
                        if not r:
                            continue
                        t = {
                            'id': r[0],
                            'target': r[1],
                            'enabled': bool(r[2]),
                            'created_at': r[3],
                            'auto_clkreboot_enabled': bool(r[4]),
                            'last_clkreboot_at': r[5],
                        }
                        # Enrich with contact name and last sync info
                        try:
                            cursor.execute(
                                "SELECT name, public_key FROM complete_contact_tracking WHERE public_key = ? OR name = ? LIMIT 1",
                                (r[1], r[1]),
                            )
                            contact = cursor.fetchone()
                            if contact:
                                t['contact_name'] = contact[0]
                                t['public_key'] = contact[1]
                        except Exception:
                            pass
                        try:
                            cursor.execute(
                                "SELECT success, error_message, sent_at FROM clock_sync_admin_log WHERE public_key = ? OR target_name = ? ORDER BY sent_at DESC LIMIT 1",
                                (r[1], r[1]),
                            )
                            sync = cursor.fetchone()
                            if sync:
                                t['last_sync_success'] = bool(sync[0])
                                t['last_sync_error'] = sync[1]
                                t['last_sync_at'] = sync[2]
                        except Exception:
                            pass
                        targets.append(t)
                    # Enrich with clock drift (same as /contacts)
                    try:
                        drift_samples = self._get_latest_clock_drift_samples(cursor)
                        for t in targets:
                            name = t.get('contact_name') or t['target']
                            sample = drift_samples.get(name)
                            if sample:
                                t['clock_drift'] = sample.get('drift')
                    except Exception:
                        pass
                    # Enrich with battery level (same Battery_Monitor data as
                    # /contacts' badge — these are the same devices, since
                    # Battery_Monitor polls exactly this target list).
                    try:
                        battery_samples = self._get_latest_battery_samples(cursor)
                        for t in targets:
                            pubkey = t.get('public_key')
                            sample = battery_samples.get(pubkey) if pubkey else None
                            if sample:
                                t['battery_voltage'] = sample['voltage']
                                t['battery_observed_at'] = sample['observed_at']
                                t['battery_status'] = self._battery_status(sample['voltage'])
                                t['battery_percent'] = self._battery_percent(sample['voltage'])
                    except Exception:
                        pass
                # Include config info + bot public key + bot name
                config_info = {
                    'enabled': self.config.getboolean('Clock_Sync_Admin', 'enabled', fallback=False),
                    'schedule': self.config.get('Clock_Sync_Admin', 'schedule', fallback='0 3 * * *'),
                    'command_payload': self.config.get('Clock_Sync_Admin', 'command_payload', fallback='clock sync admin'),
                    'min_sync_drift': self.config.getint('Clock_Sync_Admin', 'min_sync_drift', fallback=60),
                    'max_clock_drift_seconds': self.config.getint('Clock_Sync_Admin', 'dashboard_max_clock_drift_seconds', fallback=300),
                    'bot_name': (self.config.get('Bot', 'bot_name', fallback='MeshCore Bot') or '').strip() or 'MeshCore Bot',
                }
                # Bot public key: config first, then DB fallback
                bot_key = self.config.get('Clock_Sync_Admin', 'bot_public_key', fallback='')
                if not bot_key:
                    try:
                        cursor2 = conn.cursor()
                        cursor2.execute(
                            "SELECT public_key FROM complete_contact_tracking WHERE name LIKE '%Bot%' LIMIT 1"
                        )
                        bot_row = cursor2.fetchone()
                        if bot_row:
                            bot_key = bot_row[0]
                    except Exception:
                        pass
                if bot_key:
                    config_info['bot_public_key'] = bot_key
                return jsonify({'targets': targets, 'config': config_info})
            except Exception as e:
                self.logger.error(f"Error listing clock sync targets: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/clock-sync-targets-admin/<int:target_id>', methods=['PUT'])
        def api_clock_sync_targets_admin_update(target_id):
            """Update a clock sync target: enable/disable, and/or opt in or out of
            auto-clkreboot (force-reset this target's clock, then resync it, when
            it reports a clock-ahead firmware error — off by default per target)."""
            data = request.get_json(silent=True) or {}
            enabled = data.get('enabled')
            auto_clkreboot_enabled = data.get('auto_clkreboot_enabled')
            if enabled is None and auto_clkreboot_enabled is None:
                return jsonify({'error': 'enabled or auto_clkreboot_enabled is required'}), 400

            set_clauses = []
            params = []
            if enabled is not None:
                set_clauses.append('enabled = ?')
                params.append(1 if enabled else 0)
            if auto_clkreboot_enabled is not None:
                set_clauses.append('auto_clkreboot_enabled = ?')
                params.append(1 if auto_clkreboot_enabled else 0)
            params.append(target_id)

            try:
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        f"UPDATE clock_sync_targets SET {', '.join(set_clauses)} WHERE id = ?",
                        params,
                    )
                    conn.commit()
                    if cursor.rowcount == 0:
                        return jsonify({'error': 'Target not found'}), 404
                response = {'id': target_id}
                if enabled is not None:
                    response['enabled'] = bool(enabled)
                if auto_clkreboot_enabled is not None:
                    response['auto_clkreboot_enabled'] = bool(auto_clkreboot_enabled)
                return jsonify(response)
            except Exception as e:
                self.logger.error(f"Error updating clock sync target: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/clock-sync-targets-admin', methods=['POST'])
        def api_clock_sync_targets_admin_add():
            """Add a clock sync target.

            Accepts {target, public_key?}. When a valid public key (64 hex chars)
            is supplied it is stored as the canonical target identifier — this is
            the stable key the scheduler resolves against, so the cron job works
            even if the node's display name later changes or is absent from the
            live contact table. Falls back to storing the name when no key given.
            """
            data = request.get_json(silent=True) or {}
            target = (data.get('target') or '').strip().strip('"\'')
            public_key = (data.get('public_key') or '').strip().strip('"\'')

            # Validate the target (name or pubkey) against malicious input.
            ok, target, error = _validate_clock_sync_target(target)
            if not ok:
                return jsonify({'error': error}), 400

            # Validate the optional public key (must be a full 64-char hex key).
            public_key_valid = bool(public_key) and validate_pubkey_format(public_key, expected_length=64)
            if public_key and not public_key_valid:
                return jsonify({'error': 'public_key must be a 64-character hex string'}), 400

            # Prefer the public key as the stored identifier when it is a full key.
            stored = public_key.lower() if public_key_valid else target

            try:
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO clock_sync_targets (target) VALUES (?)",
                        (stored,),
                    )
                    conn.commit()
                    new_id = cursor.lastrowid
                return jsonify({
                    'id': new_id,
                    'target': stored,
                    'name': target,
                    'public_key': public_key.lower() if public_key_valid else None,
                    'enabled': True,
                }), 201
            except sqlite3.IntegrityError:
                return jsonify({'error': f'Target "{target}" already exists'}), 409
            except Exception as e:
                self.logger.error(f"Error adding clock sync target: {e}")
                return jsonify({'error': str(e)}), 500
        @self.app.route('/api/clock-sync-targets-admin/<int:target_id>', methods=['DELETE'])
        def api_clock_sync_targets_admin_delete(target_id):
            """Delete a clock sync target"""
            try:
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "DELETE FROM clock_sync_targets WHERE id = ?",
                        (target_id,),
                    )
                    conn.commit()
                    if cursor.rowcount == 0:
                        return jsonify({'error': 'Target not found'}), 404
                return jsonify({'deleted': target_id})
            except Exception as e:
                self.logger.error(f"Error deleting clock sync target: {e}")
                return jsonify({'error': str(e)}), 500
        @self.app.route('/api/clock-sync-targets-admin/<int:target_id>/history', methods=['GET'])
        def api_clock_sync_targets_admin_history(target_id):
            """Get last 5 clock sync interactions for a target"""
            try:
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT target FROM clock_sync_targets WHERE id = ?",
                        (target_id,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return jsonify({'error': 'Target not found'}), 404
                    target_key = row[0]
                    cursor.execute(
                        "SELECT success, error_message, sent_at FROM clock_sync_admin_log WHERE public_key = ? OR target_name = ? ORDER BY sent_at DESC LIMIT 5",
                        (target_key, target_key),
                    )
                    logs = cursor.fetchall()
                    history = [
                        {'success': bool(l[0]), 'error': l[1], 'at': l[2]}
                        for l in logs if l
                    ]
                return jsonify({'history': history})
            except Exception as e:
                self.logger.error(f"Error getting clock sync history: {e}")
                return jsonify({'error': str(e)}), 500
        @self.app.route('/api/clock-sync-targets-admin/resolve', methods=['GET'])
        def api_clock_sync_targets_admin_resolve():
            """Resolve a typed name/pubkey to known nodes (name + public_key) for autocomplete.

            Matches exact name, exact pubkey, name prefix, or pubkey prefix
            (case-insensitive), ordered by relevance. Backed by the persistent
            complete_contact_tracking table so it works even when the node is not
            in the live radio contact table.
            """
            needle = sanitize_name(request.args.get('target') or '', max_length=64).strip().strip('"\'')
            if not needle:
                return jsonify({'matches': [], 'exact': False})
            # Escape LIKE wildcards so a typed % or _ is matched literally, not as a wildcard.
            needle_like = needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            try:
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT name, public_key,
                            CASE
                                WHEN lower(name) = lower(?) THEN 0
                                WHEN lower(public_key) = lower(?) THEN 1
                                WHEN lower(name) LIKE lower(?) || '%' ESCAPE '\\' THEN 2
                                WHEN lower(public_key) LIKE lower(?) || '%' ESCAPE '\\' THEN 3
                                ELSE 4
                            END AS rank
                        FROM complete_contact_tracking
                        WHERE lower(name) = lower(?)
                           OR lower(public_key) = lower(?)
                           OR lower(name) LIKE lower(?) || '%' ESCAPE '\\'
                           OR lower(public_key) LIKE lower(?) || '%' ESCAPE '\\'
                        ORDER BY rank, name
                        LIMIT 10
                        """,
                        (needle, needle, needle_like, needle_like, needle, needle, needle_like, needle_like),
                    )
                    rows = cursor.fetchall()
                    matches = [
                        {'name': r[0], 'public_key': r[1]}
                        for r in rows
                        if r and r[0] and r[1]
                    ]
                    exact = bool(matches) and (
                        matches[0]['name'].lower() == needle.lower()
                        or matches[0]['public_key'].lower() == needle.lower()
                    )
                return jsonify({'matches': matches, 'exact': exact})
            except Exception as e:
                self.logger.error(f"Error resolving clock sync target: {e}")
                return jsonify({'error': str(e)}), 500
        @self.app.route('/api/contacts/local-hop-collisions')
        def api_contacts_local_hop_collisions():
            """Return local (0-hop) repeaters/roomservers sharing a 1-byte public-key prefix.

            Query params:
              days (int, default 30): activity window; 0 = all time.
            """
            days = request.args.get('days', 30, type=int)
            if days < 0:
                return jsonify({'error': 'days must be >= 0'}), 400
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()

                time_clause = ""
                params: list = []
                if days > 0:
                    time_clause = "AND last_heard >= datetime('now', ?)"
                    params = [f'-{days} days']

                # params appears twice: once for the outer WHERE and once for the inner subquery
                cursor.execute(f'''
                    SELECT
                        SUBSTR(public_key, 1, 2) AS prefix_1b,
                        public_key,
                        name,
                        role,
                        hop_count,
                        last_heard
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                      AND out_path_len = 0
                      {time_clause}
                      AND SUBSTR(public_key, 1, 2) IN (
                          SELECT SUBSTR(public_key, 1, 2)
                          FROM complete_contact_tracking
                          WHERE role IN ('repeater', 'roomserver')
                            AND out_path_len = 0
                            {time_clause}
                          GROUP BY SUBSTR(public_key, 1, 2)
                          HAVING COUNT(*) > 1
                      )
                    ORDER BY SUBSTR(public_key, 1, 2), name COLLATE NOCASE
                ''', params * 2)

                rows = cursor.fetchall()
                groups: dict = {}
                for row in rows:
                    prefix = row['prefix_1b']
                    if prefix not in groups:
                        groups[prefix] = []
                    groups[prefix].append({
                        'name': row['name'],
                        'public_key': row['public_key'],
                        'role': row['role'],
                        'hop_count': row['hop_count'],
                        'last_heard': row['last_heard'],
                    })

                collisions = [
                    {'prefix': prefix, 'repeaters': repeaters}
                    for prefix, repeaters in sorted(groups.items())
                ]
                total_affected = sum(len(g['repeaters']) for g in collisions)

                return jsonify({
                    'collisions': collisions,
                    'total_collision_groups': len(collisions),
                    'total_affected_repeaters': total_affected,
                    'days': days,
                })
            except Exception as e:
                self.logger.error(f"Error fetching local hop collisions: {e}", exc_info=True)
                return jsonify({'error': 'Internal server error'}), 500
            finally:
                if conn:
                    conn.close()
        @self.app.route('/api/health/storage')
        def api_health_storage():
            """Read-only filesystem state for an external monitoring agent.

            Aggregate only: no mount paths, no configuration values. The probe write is
            not recorded as a storage fault, so a prober polling this route cannot push
            the bot over its reboot threshold.
            """
            try:
                monitor = IOHealthMonitor(self.config, self.logger, db_manager=self.db_manager)
                fields = monitor.status()
                fields['uptime_seconds'] = self._get_bot_uptime()
                fields['timestamp'] = int(time.time())
                if request.args.get('format') == 'text':
                    return Response(_storage_text(fields), mimetype='text/plain; charset=utf-8')
                return jsonify(fields)
            except Exception:
                self.logger.exception("Error building storage health view")
                return jsonify({'error': 'Failed to build storage health view'}), 500
        @self.app.route('/api/health/storage/powercycle', methods=['POST'])
        def api_health_storage_powercycle():
            """Run the configured power-cycle hook, or raise the reboot semaphore.

            Admin-only. The reason (JSON body or ``?reason=``) is collapsed to one line
            before it reaches the semaphore info file. A hook that fails answers 502 so
            a monitoring script can tell the difference between "not delivered" and
            "delivered".
            """
            try:
                monitor = IOHealthMonitor(self.config, self.logger, db_manager=self.db_manager)
                fields = monitor.status()

                reason = ''
                payload = request.get_json(silent=True)
                if isinstance(payload, dict):
                    reason = str(payload.get('reason', '') or '')
                reason = (reason or request.args.get('reason', '') or 'manual:web').strip()

                ok, action, detail = monitor.powercycle(reason)
                result = {
                    'powercycle': 'ok' if ok else 'failed',
                    'action': action,
                    'detail': detail,
                    'state': fields['state'],
                    'semaphore_pending': monitor.semaphore_pending(),
                }
                if request.args.get('format') == 'text':
                    return Response(_storage_text(result), mimetype='text/plain; charset=utf-8'), (200 if ok else 502)
                return jsonify(result), (200 if ok else 502)
            except Exception:
                self.logger.exception("Error running the power cycle request")
                return jsonify({'error': 'Failed to run the power cycle request'}), 500
        @self.app.route('/api/radio/announcement', methods=['POST'])
        def api_radio_announcement():
            """Send an announcement message to a channel.
            Body: {channel: str, message: str}
            """
            try:
                data = request.get_json(silent=True) or {}
                channel = data.get('channel', '').strip()
                message = data.get('message', '').strip()

                if not channel:
                    return jsonify({'error': 'Channel is required'}), 400
                if not message:
                    return jsonify({'error': 'Message is required'}), 400
                if len(message) > 200:
                    return jsonify({'error': 'Message must be 200 characters or less'}), 400

                # Queue the announcement as a channel operation
                payload = {
                    'channel': channel,
                    'message': message
                }
                with self.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO channel_operations (operation_type, payload_data, status) VALUES ('send_announcement', ?, 'pending')",
                        (json.dumps(payload),)
                    )
                    conn.commit()
                    op_id = cursor.lastrowid

                self.logger.info(f"Queued announcement to channel {channel}: {message}")
                return jsonify({'success': True, 'operation_id': op_id, 'message': 'Announcement queued'})
            except Exception as e:
                self.logger.error(f"Error queuing announcement: {e}")
                return jsonify({'error': 'Failed to queue announcement'}), 500
        @self.app.route('/api/radio/test-dm', methods=['POST'])
        def api_radio_test_dm():
            """Run a command locally as a test DM and return captured replies.

            Body: {message: str, sender: str}
            This does not queue anything for the radio and does not spend airtime.
            """
            try:
                data = request.get_json(silent=True) or {}
                message = data.get('message', '').strip()
                sender = data.get('sender', 'web-test').strip() or 'web-test'

                if not message:
                    return jsonify({'error': 'Message is required'}), 400
                if len(message) > 200:
                    return jsonify({'error': 'Message must be 200 characters or less'}), 400

                replies = self._run_test_dm(message, sender)

                self.logger.info(f"Web Viewer test DM: {message!r} -> {replies}")
                return jsonify({'success': True, 'replies': replies})
            except Exception as e:
                self.logger.error(f"Error running Web Viewer test DM: {e}")
                return jsonify({'error': 'Failed to run test command'}), 500

        # ── Plugins settings panel ───────────────────────────────────────────
        @self.app.route('/api/regions')
        def api_regions():
            """Public aggregates only: no settings, no per-sender data."""
            try:
                self.config = self._load_merged_config()
                try:
                    days = max(1, min(int(request.args.get('days', 14)), 90))
                except (TypeError, ValueError):
                    days = 14
                return jsonify({
                    'traffic': region_warning.traffic_summary(
                        self.db_manager, self.config, days, self.logger
                    ),
                    'series': region_warning.daily_series(
                        self.db_manager, self.config, days, self.logger
                    ),
                })
            except Exception:
                self.logger.exception("Error building public regions view")
                return jsonify({'error': 'Failed to build regions view'}), 500
        @self.app.route('/infos')
        def infos():
            """Bot info page — command summary and useful links"""
            all_commands = self._get_command_info()
            # Group by category preserving sort order
            from collections import OrderedDict  # noqa: PLC0415
            groups: OrderedDict = OrderedDict()
            for cmd in all_commands:
                cat = cmd['category']
                if cat not in groups:
                    groups[cat] = {'category_label': cmd['category_label'], 'commands': []}
                groups[cat]['commands'].append(cmd)
            command_groups = list(groups.values())

            command_prefix = (self.config.get('Bot', 'command_prefix', fallback='') or '').strip()
            monitor_channels_raw = self.config.get('Channels', 'monitor_channels', fallback='')
            monitor_channels = [c.strip() for c in monitor_channels_raw.split(',') if c.strip()]
            respond_to_dms = self.config.getboolean('Channels', 'respond_to_dms', fallback=True)
            cmd_reference_url_raw = (self.config.get('Cmd_Command', 'cmd_reference_url', fallback='') or '').strip()
            cmd_reference_url = ''
            if cmd_reference_url_raw:
                try:
                    if validate_external_url(cmd_reference_url_raw):
                        cmd_reference_url = cmd_reference_url_raw
                except Exception:
                    pass

            from modules.commands.llm_command import LlmCommand
            class _MinimalBot:
                def __init__(self, config: Any, logger: Any, db_manager: Any = None) -> None:
                    self.config = config
                    self.logger = logger
                    self.db_manager = db_manager

            llm_context = ""
            llm_context_breakdown = []
            llm_context_total_tokens = 0
            try:
                minimal_bot = _MinimalBot(self.config, self.logger, getattr(self, 'db_manager', None))
                llm_cmd = LlmCommand(minimal_bot)
                llm_context = llm_cmd._inject_current_time_into_prompt(llm_cmd.system_prompt)
                llm_context_breakdown = llm_cmd._cached_context_breakdown
                if llm_context_breakdown:
                    llm_context_total_tokens = sum(s['est_tokens'] for s in llm_context_breakdown)
                    sys_prompt_tokens = len(llm_cmd.system_prompt) // 4
                    llm_context_total_tokens += sys_prompt_tokens
            except Exception as e:
                self.logger.warning("Failed to load LLM context for /infos page: %s", e)

            return render_template(
                'infos.html',
                command_groups=command_groups,
                total_commands=len(all_commands),
                command_prefix=command_prefix,
                monitor_channels=monitor_channels,
                respond_to_dms=respond_to_dms,
                cmd_reference_url=cmd_reference_url,
                llm_context=llm_context,
                llm_context_breakdown=llm_context_breakdown,
                llm_context_total_tokens=llm_context_total_tokens,
            )
        @self.app.route('/regions')
        def regions_page():
            """Public explainer: why the bot sends region warnings, with public stats."""
            return render_template('regions.html')
        @self.app.route('/time')
        def time_page():
            """Time synchronization page"""
            return render_template('time.html')
        @self.app.route('/time/admin')
        def time_admin_page():
            """Clock sync targets admin page"""
            return render_template('time_admin.html')

    def _station_state(self) -> dict[str, Any]:
        """Whether the bot is running and its radio connected, from bot_metadata.

        The stored start time survives the bot stopping, so uptime can't answer
        "is it running?". The bot rewrites system_health every 30 seconds
        (core.py's main loop); a heartbeat older than BOT_HEARTBEAT_STALE_S means
        it isn't. radio_connected is the bot's last word on the link, so it's
        only reported while the bot is there to keep it current.
        """
        last_seen = None
        health = self.db_manager.get_system_health()
        if isinstance(health, dict):
            try:
                last_seen = float(health.get('timestamp')) if health.get('timestamp') else None
            except (TypeError, ValueError):
                last_seen = None
        bot_running = last_seen is not None and time.time() - last_seen < BOT_HEARTBEAT_STALE_S
        radio_value = self.db_manager.get_metadata('radio_connected')
        radio_connected = (radio_value == '1') if (bot_running and radio_value is not None) else None
        started = self.db_manager.get_bot_start_time() if bot_running else None
        return {
            'bot_running': bot_running,
            'bot_last_seen': last_seen,
            'bot_started': started,
            'radio_connected': radio_connected,
        }

    def _get_bot_uptime(self):
        """Get bot uptime in seconds from database"""
        try:
            # Get start time from database metadata
            start_time = self.db_manager.get_bot_start_time()
            if start_time:
                return int(time.time() - start_time)
            else:
                # Fallback: try to get earliest message timestamp
                with self._with_db_connection() as conn:
                    cursor = conn.cursor()

                    # Try to get earliest message timestamp as fallback
                    cursor.execute("""
                        SELECT MIN(timestamp) FROM message_stats
                        WHERE timestamp IS NOT NULL
                    """)
                    result = cursor.fetchone()
                    if result and result[0]:
                        return int(time.time() - result[0])

                return 0
        except Exception as e:
            self.logger.debug(f"Could not get bot start time from database: {e}")
            return 0


    def _decode_path_hex(self, path_hex: str, bytes_per_hop: int | None = None) -> list[dict[str, Any]]:
        """Decode a hex path string to repeater nodes.

        Thin wrapper over the shared engine (modules.path_inference.decode_path_nodes); the
        bot `path` command and /api/mesh/resolve-path use the same implementation.
        """
        from modules.path_inference import decode_path_nodes
        return decode_path_nodes(
            path_hex,
            bytes_per_hop,
            config=self.config,
            db_manager=self.db_manager,
            logger=self.logger,
            mesh_graph=self._get_mesh_graph(),
        )

    @staticmethod
    def _battery_percent(voltage: float | None) -> int | None:
        """Rough voltage-to-percentage estimate for display alongside the
        raw voltage — same linear 3.0V=0% / 4.2V=100% mapping the companion
        firmware itself uses for its own screen battery icon
        (``examples/companion_radio/ui-new/UITask.cpp``'s
        ``renderBatteryIndicator``, ``BATT_MIN_MILLIVOLTS``/
        ``BATT_MAX_MILLIVOLTS``), so this matches what the device's own
        display already shows rather than inventing a different curve.
        Real Li-ion discharge isn't linear, but neither is the firmware's
        own estimate — consistency with the device's own screen matters
        more here than curve accuracy.
        """
        if voltage is None:
            return None
        percent = (voltage - 3.0) * 100 / (4.2 - 3.0)
        return max(0, min(100, round(percent)))

    def _battery_status(self, voltage: float | None) -> str | None:
        """Classify a voltage reading for the contacts-page badge colour.
        Thresholds are configurable (``[Battery_Monitor]
        low_voltage``/``critical_voltage``) since battery chemistry and
        wiring vary per device — there is no universal "20%" for a raw
        voltage reading. Returns ``'critical'``/``'low'``/``'ok'``/``None``
        (no reading yet)."""
        if voltage is None:
            return None
        critical = self.config.getfloat('Battery_Monitor', 'critical_voltage', fallback=3.4)
        low = self.config.getfloat('Battery_Monitor', 'low_voltage', fallback=3.7)
        if voltage <= critical:
            return 'critical'
        if voltage <= low:
            return 'low'
        return 'ok'

    @staticmethod

    def _contact_path_bytes_per_hop(
        self,
        row: Any,
        all_paths: list[dict[str, Any]],
    ) -> int | None:
        """Determine the path encoding (bytes-per-hop) for a contact.

        Priority: valid ``out_bytes_per_hop``, then the first valid encoding among
        observed paths. Invalid/NULL observed encodings are ignored (they must not
        silently become 1-byte and override a valid out encoding).
        """
        obph_raw = None
        try:
            obph_raw = row["out_bytes_per_hop"]
        except (KeyError, IndexError):
            obph_raw = None
        obph: int | None
        try:
            obph = int(obph_raw) if obph_raw is not None else None
        except (TypeError, ValueError):
            obph = None
        if obph is not None and obph in (1, 2, 3):
            return obph

        for p in all_paths:
            enc = p.get("bytes_per_hop")
            try:
                val = int(enc) if enc is not None else None
            except (TypeError, ValueError):
                val = None
            if val is not None and val in (1, 2, 3):
                return val
        return None

    def _count_multibyte_packets_from_stream_json(self, cursor, cutoff_ts: float) -> int:
        """Count packet_stream rows (type=packet) since cutoff with bytes_per_hop in (2, 3). JSON parse fallback."""
        import json

        n = 0
        try:
            cursor.execute(
                """
                SELECT data FROM packet_stream
                WHERE type = ? AND timestamp > ?
                """,
                ("packet", cutoff_ts),
            )
            for row in cursor.fetchall():
                raw = row["data"]
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                bph = d.get("bytes_per_hop")
                try:
                    bph_i = int(bph) if bph is not None else None
                except (TypeError, ValueError):
                    bph_i = None
                if bph_i in (2, 3):
                    n += 1
        except Exception as e:
            self.logger.debug(f"packet_stream JSON scan for multibyte: {e}")
        return n

    @staticmethod
    def _extract_advert_sender_timestamp(raw_advert_data: Any) -> Optional[int]:
        """Extract the sender clock (``advert_time``) from raw advert data."""
        if raw_advert_data is None:
            return None
        if isinstance(raw_advert_data, str):
            try:
                raw_advert_data = json.loads(raw_advert_data)
            except (TypeError, ValueError):
                return None
        if not isinstance(raw_advert_data, dict):
            return None
        value = raw_advert_data.get('advert_time')
        if value is None:
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _get_clock_sync_targets_status(self):
        """Get Clock_Sync_Admin targets with their synchronization status"""
        try:
            # Get configuration
            enabled = self.config.getboolean('Clock_Sync_Admin', 'enabled', fallback=False)
            schedule = self.config.get('Clock_Sync_Admin', 'schedule', fallback='0 3 * * *')
            command_payload = self.config.get('Clock_Sync_Admin', 'command_payload', fallback='clock sync admin')
            targets_raw = self.config.get('Clock_Sync_Admin', 'targets', fallback='')

            # Parse targets
            targets = []
            seen = set()
            for token in (targets_raw or "").split(","):
                candidate = token.strip().strip("\"'")
                if not candidate:
                    continue
                dedup_key = candidate.lower()
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                targets.append(candidate)

            # Get clock drift settings
            drift_threshold_seconds = self.config.getint(
                'Clock_Sync_Admin',
                'dashboard_max_clock_drift_seconds',
                fallback=300,
            )
            check_window_hours = self.config.getint(
                'Clock_Sync_Admin',
                'dashboard_check_window_hours',
                fallback=24,
            )

            # Resolve targets and get their status
            targets_status = []
            bot = getattr(self, 'bot', None)

            # Build live contacts lookup when meshcore is available (in-process bot)
            pubkey_to_contact: dict[str, Any] = {}
            meshcore = None
            if bot and hasattr(bot, 'meshcore') and bot.meshcore:
                meshcore = bot.meshcore
                contacts = getattr(meshcore, 'contacts', {}) or {}
                for contact_data in contacts.values():
                    public_key = (contact_data.get("public_key", "") or "").strip()
                    if public_key:
                        pubkey_to_contact[public_key] = contact_data

            # Always query the database for drift data, contacts, and sync log
            drift_data: dict[str, Any] = {}
            last_heard_data: dict[str, Any] = {}
            db_contacts: dict[str, Any] = {}
            clock_sync_log: dict[str, Any] = {}
            conn = None
            try:
                conn = self._get_db_connection()
                cursor = conn.cursor()

                # Get message stats with clock drift information
                cutoff_time = time.time() - (check_window_hours * 3600)
                cursor.execute(
                    """
                    WITH latest_message_per_sender AS (
                        SELECT sender_id, MAX(id) AS latest_id
                        FROM message_stats
                        GROUP BY sender_id
                    )
                    SELECT
                        c.name,
                        c.public_key,
                        c.role,
                        c.hop_count,
                        CAST(strftime('%s', m.created_at) AS INTEGER) AS received_at,
                        m.timestamp AS sender_timestamp,
                        CASE
                            WHEN m.timestamp IS NULL THEN NULL
                            WHEN CAST(m.timestamp AS INTEGER) <= 0 THEN NULL
                            ELSE ABS(
                                CAST(strftime('%s', m.created_at) AS INTEGER)
                                - CAST(m.timestamp AS INTEGER)
                            )
                        END AS drift_seconds
                    FROM complete_contact_tracking c
                    JOIN latest_message_per_sender lm ON c.name = lm.sender_id
                    JOIN message_stats m ON m.id = lm.latest_id
                    WHERE CAST(strftime('%s', m.created_at) AS INTEGER) >= ?
                    AND m.timestamp IS NOT NULL
                    AND CAST(m.timestamp AS INTEGER) > 0
                    """,
                    (cutoff_time,)
                )

                for row in cursor.fetchall():
                    drift_data[row[1]] = {  # public_key as key
                        'name': row[0],
                        'role': row[2],
                        'hop_count': row[3],
                        'received_at': row[4],
                        'sender_timestamp': row[5],
                        'drift_seconds': int(row[6]) if row[6] is not None else None,
                    }

                # Get last_heard timestamps for all contacts
                cursor.execute(
                    """
                    SELECT public_key, CAST(strftime('%s', last_heard) AS INTEGER) AS last_heard_ts
                    FROM complete_contact_tracking
                    WHERE last_heard IS NOT NULL
                    """
                )
                for row in cursor.fetchall():
                    last_heard_data[row[0]] = row[1]  # public_key -> last_heard timestamp

                # Build a DB-backed contact lookup (name/role/hop_count) as a fallback
                # when a target is not present in the radio's live contact table.
                cursor.execute(
                    """
                    SELECT public_key, name, role, hop_count
                    FROM complete_contact_tracking
                    WHERE public_key IS NOT NULL AND public_key != ''
                    """
                )
                for row in cursor.fetchall():
                    public_key = (row[0] or "").strip()
                    if not public_key:
                        continue
                    db_contacts[public_key] = {
                        'name': (row[1] or "").strip(),
                        'role': row[2],
                        'hop_count': row[3],
                    }

                # Get latest Clock_Sync_Admin log entries for each public key
                cursor.execute(
                    """
                    SELECT public_key, success, CAST(strftime('%s', sent_at) AS INTEGER), error_message
                    FROM clock_sync_admin_log
                    WHERE id IN (
                        SELECT MAX(id)
                        FROM clock_sync_admin_log
                        GROUP BY public_key
                    )
                    """
                )
                for row in cursor.fetchall():
                    clock_sync_log[row[0]] = {  # public_key as key
                        'success': bool(row[1]),
                        'sent_at': row[2],
                        'error_message': row[3],
                    }
            finally:
                if conn:
                    conn.close()

            # Process each target
            for target_identifier in targets:
                target_info = {
                    'identifier': target_identifier,
                    'name': target_identifier,
                    'public_key': None,
                    'role': None,
                    'hop_count': None,
                    'drift_seconds': None,
                    'message_timestamp': None,
                    'last_seen': None,
                    'status': 'Not Found',
                    'last_sync_success': None,
                    'last_sync_at': None,
                    'last_sync_error': None
                }

                # Try to resolve contact by name via live meshcore
                contact = None
                if meshcore:
                    try:
                        contact = meshcore.get_contact_by_name(target_identifier)
                    except Exception:
                        pass

                    # If not found by name, try by exact public key or prefix match
                    if not contact:
                        if target_identifier in pubkey_to_contact:
                            contact = pubkey_to_contact[target_identifier]
                        else:
                            for public_key, contact_data in pubkey_to_contact.items():
                                if public_key.startswith(target_identifier):
                                    contact = contact_data
                                    break

                # Fallback: resolve from the DB contact tracking table
                db_contact = None
                if not contact:
                    db_contact = db_contacts.get(target_identifier)
                    if not db_contact:
                        for db_key, db_data in db_contacts.items():
                            if db_key.startswith(target_identifier):
                                db_contact = db_data
                                break
                    if db_contact:
                        db_contact = dict(db_contact)
                        db_contact['public_key'] = (
                            target_identifier if target_identifier in db_contacts
                            else next(
                                (k for k in db_contacts if k.startswith(target_identifier)),
                                None,
                            )
                        )

                resolved = contact or db_contact

                if resolved:
                    public_key = (resolved.get('public_key', '') or '').strip()
                    contact_name = (
                        (resolved.get('name', '') or '').strip()
                        or (resolved.get('adv_name', '') or '').strip()
                        or target_identifier
                    )

                    target_info['name'] = contact_name
                    target_info['public_key'] = public_key
                    target_info['role'] = resolved.get('role', 'unknown')
                    target_info['hop_count'] = resolved.get('hop_count')

                    # Get Clock_Sync_Admin log data if available
                    if public_key in clock_sync_log:
                        log = clock_sync_log[public_key]
                        target_info['last_sync_success'] = log['success']
                        target_info['last_sync_at'] = log['sent_at']
                        target_info['last_sync_error'] = log['error_message']
                    else:
                        target_info['last_sync_success'] = None
                        target_info['last_sync_at'] = None
                        target_info['last_sync_error'] = None

                    # Get drift data if available
                    if public_key in drift_data:
                        drift = drift_data[public_key]
                        target_info['drift_seconds'] = drift['drift_seconds']
                        target_info['last_seen'] = drift['received_at']
                        target_info['message_timestamp'] = drift['sender_timestamp']

                        if drift['drift_seconds'] is not None:
                            if drift['drift_seconds'] <= drift_threshold_seconds:
                                target_info['status'] = 'In Sync'
                            else:
                                target_info['status'] = 'Out of Sync'
                        else:
                            target_info['status'] = 'No Data'
                    else:
                        # No recent drift data, but still show last_heard if available
                        if public_key in last_heard_data:
                            target_info['last_seen'] = last_heard_data[public_key]
                        target_info['status'] = 'Unknown'

                targets_status.append(target_info)

            return {
                'enabled': enabled,
                'schedule': schedule,
                'command_payload': command_payload,
                'targets': targets_status,
                'drift_threshold_seconds': drift_threshold_seconds,
                'check_window_hours': check_window_hours,
            }

        except Exception as e:
            self.logger.error(f"Error getting clock sync targets status: {e}", exc_info=True)
            return {
                'enabled': False,
                'schedule': '',
                'command_payload': '',
                'targets': []
            }

    def _get_command_info(self) -> list[dict]:
        """Load and cache public command info using PluginLoader.

        Returns a list of dicts (name, keywords, description, short_description,
        usage, examples, category) sorted basic-first then alphabetically.
        Returns an empty list on any failure so the page still renders.
        """
        if hasattr(self, '_command_info_cache'):
            return self._command_info_cache

        try:
            from modules.plugin_loader import PluginLoader  # noqa: PLC0415

            class _DummyTranslator:
                def translate(self, key: str, **kwargs: Any) -> str:
                    return key

                def get_value(self, key: str) -> Any:
                    return None

            class _MinimalBot:
                def __init__(self, config: Any, logger: Any, db_manager: Any = None) -> None:
                    self.config = config
                    self.logger = logger
                    self.db_manager = db_manager
                    self.translator = _DummyTranslator()
                    self.command_manager = None

            minimal_bot = _MinimalBot(self.config, self.logger, getattr(self, 'db_manager', None))
            plugin_loader = PluginLoader(minimal_bot)
            commands = plugin_loader.load_all_plugins()

            admin_commands_str = self.config.get('Admin_ACL', 'admin_commands', fallback='')
            admin_commands = {c.strip() for c in admin_commands_str.split(',') if c.strip()}

            filtered: list[dict] = []
            for cmd_name, cmd_instance in commands.items():
                category = getattr(cmd_instance, 'category', 'general')
                if category in self._EXCLUDED_CATEGORIES:
                    continue
                primary_name = getattr(cmd_instance, 'name', cmd_name)
                if cmd_name in admin_commands or primary_name in admin_commands:
                    continue
                if hasattr(cmd_instance, 'requires_admin_access') and cmd_instance.requires_admin_access():
                    continue
                if not self._is_command_enabled(cmd_instance):
                    continue
                keywords = getattr(cmd_instance, 'keywords', [])
                if not keywords:
                    continue
                filtered.append({
                    'name': primary_name,
                    'keywords': keywords,
                    'description': getattr(cmd_instance, 'description', ''),
                    'short_description': getattr(cmd_instance, 'short_description', ''),
                    'usage': getattr(cmd_instance, 'usage', ''),
                    'examples': getattr(cmd_instance, 'examples', []),
                    'category': category,
                    'category_label': self._CATEGORY_NAMES.get(category, category.replace('_', ' ').title()),
                })

            # Sort: basic first, then by category label, then by command name
            _cat_order = {cat: i for i, cat in enumerate(self._CATEGORY_NAMES)}
            filtered.sort(key=lambda c: (_cat_order.get(c['category'], 99), c['name']))

            self._command_info_cache = filtered
        except Exception as e:
            self.logger.warning("Could not load command info for /infos page: %s", e)
            self._command_info_cache = []

        return self._command_info_cache

    def _get_contact_page_scope_keys(self, since='30d', search=''):
        """Return the public keys eligible for a contacts page (search + since).

        This is a lightweight pre-filter over complete_contact_tracking used to
        bound the expensive observed_paths join to only the contacts that could
        appear on the requested page. It is intentionally a *superset* of the
        visible page (only the cheap SQL-routable filters are applied); the
        Python-side filter in api_contacts remains authoritative.
        """
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            if since == 'all':
                since_clause = ''
            elif since == '24h':
                since_clause = " WHERE c.last_heard >= strftime('%s', 'now', '-24 hours')"
            elif since == '7d':
                since_clause = " WHERE c.last_heard >= strftime('%s', 'now', '-7 days')"
            elif since == '30d':
                since_clause = " WHERE c.last_heard >= strftime('%s', 'now', '-30 days')"
            else:
                since_clause = " WHERE c.last_heard >= strftime('%s', 'now', '-90 days')"

            params = []
            search_clause = ''
            if search:
                search_sql = (
                    " (LOWER(c.name) LIKE ? OR LOWER(c.public_key) LIKE ?"
                    " OR LOWER(c.city) LIKE ? OR LOWER(c.state) LIKE ?"
                    " OR LOWER(c.country) LIKE ? OR LOWER(c.device_type) LIKE ?)"
                )
                search_clause = (
                    ("WHERE" if not since_clause else "AND") + search_sql
                )
                like = f"%{search.lower()}%"
                params = [like] * 6

            sql = (
                "SELECT c.public_key FROM complete_contact_tracking c "
                + since_clause + search_clause
                + " ORDER BY c.last_heard DESC"
            )
            cursor.execute(sql, params)
            return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            self.logger.debug(f"Could not scope contacts page keys: {e}")
            return None
        finally:
            if conn is not None:
                conn.close()

    def _get_latest_battery_samples(self, cursor: Any) -> dict[str, dict[str, Any]]:
        """Latest battery voltage reading per device, from Battery_Monitor's
        polling (``modules/scheduler.py``, ``battery_observations`` table).

        Returns ``{public_key: {'voltage': float, 'observed_at': str}}`` —
        keyed by public key, unlike ``_get_latest_clock_drift_samples``'s
        name-keying, since ``battery_observations`` is written with the full
        public key already (no need to go through the name join it uses to
        work around ``message_stats`` only storing names).
        """
        samples: dict[str, dict[str, Any]] = {}
        try:
            cursor.execute(
                """
                SELECT public_key, voltage, observed_at
                FROM battery_observations bo
                WHERE observed_at = (
                    SELECT MAX(observed_at) FROM battery_observations
                    WHERE public_key = bo.public_key
                )
                """
            )
            for row in cursor.fetchall():
                samples[row['public_key']] = {
                    'voltage': row['voltage'],
                    'observed_at': row['observed_at'],
                }
        except Exception as e:
            self.logger.error(f"Error computing battery samples: {e}")
            return {}
        return samples

    def _get_latest_clock_drift_samples(self, cursor: Any) -> dict[str, dict[str, Any]]:
        """Latest per-contact clock drift sample, from messages and adverts.

        ``message_stats.sender_id`` stores the contact *name* (not the public
        key), so the join is against ``complete_contact_tracking.name`` — the
        same join the clock sync dashboard uses. For every contact the freshest
        clocked packet wins: the latest message or the latest advertisement
        (``raw_advert_data.advert_time`` is the sender clock, ``last_heard``
        the reception time). Continuous adverts keep the drift current even
        when a device stops sending messages (e.g. a companion that is clock
        synced at bot startup but only advertises afterwards).

        Returns ``{name: {'drift': int, 'received_at': float,
        'sender_timestamp': int, 'source': 'message' | 'advert'}}``; contacts
        with no parsable sender timestamp are excluded (drift unknown, not
        "in sync").
        """
        samples: dict[str, dict[str, Any]] = {}
        try:
            cursor.execute(
                """
                WITH latest_message_per_sender AS (
                    SELECT sender_id, MAX(id) AS latest_id
                    FROM message_stats
                    GROUP BY sender_id
                )
                SELECT
                    c.name,
                    m.timestamp AS sender_timestamp,
                    m.created_at AS received_at
                FROM complete_contact_tracking c
                INNER JOIN latest_message_per_sender lm
                    ON lm.sender_id = c.name
                INNER JOIN message_stats m
                    ON m.id = lm.latest_id
                WHERE m.timestamp IS NOT NULL
                    AND CAST(m.timestamp AS INTEGER) > 0
                """
            )
            for row in cursor.fetchall():
                sender_timestamp = row['sender_timestamp']
                received_at = self._parse_db_timestamp(row['received_at'])
                if received_at is None:
                    continue
                try:
                    sender_epoch = int(sender_timestamp)
                except (TypeError, ValueError):
                    continue
                if sender_epoch <= 0:
                    continue
                samples[row['name']] = {
                    'drift': abs(int(received_at) - sender_epoch),
                    'received_at': received_at,
                    'sender_timestamp': sender_epoch,
                    'source': 'message',
                }

            cursor.execute(
                """
                SELECT name, last_heard, raw_advert_data
                FROM complete_contact_tracking
                WHERE name IS NOT NULL
                    AND last_heard IS NOT NULL
                    AND raw_advert_data IS NOT NULL
                """
            )
            for row in cursor.fetchall():
                received_at = self._parse_db_timestamp(row['last_heard'])
                if received_at is None:
                    continue
                sender_epoch = self._extract_advert_sender_timestamp(row['raw_advert_data'])
                if sender_epoch is None:
                    continue
                existing = samples.get(row['name'])
                if existing is not None and existing['received_at'] >= received_at:
                    continue
                samples[row['name']] = {
                    'drift': abs(int(received_at) - sender_epoch),
                    'received_at': received_at,
                    'sender_timestamp': sender_epoch,
                    'source': 'advert',
                }
        except Exception as e:
            self.logger.error(f"Error computing clock drift samples: {e}")
            return {}
        return samples

    @staticmethod
    def _parse_db_timestamp(value: Any) -> Optional[float]:
        """Parse a naive UTC timestamp from the DB into epoch seconds."""
        if value is None:
            return None
        text = str(value).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return None

    @staticmethod

    def _preview_fetch_body(self, feed_url, method='GET', headers=None, params=None, body=None):
        """Stream-fetch a feed body with a hard size cap (2 MiB).

        Uses the safe requests session/request helpers so every outbound fetch
        is policy-checked and redirect-bounded; responses over the cap raise
        ValueError and are closed immediately.
        """
        feed_preview_byte_limit = 2 * 1024 * 1024
        url_policy = SafeUrlPolicy(
            allow_private=self.config.getboolean(
                'Feed_Manager', 'allow_private_urls', fallback=False
            )
        )
        session = create_safe_requests_session(url_policy)
        with session:
            response = safe_requests_request(
                session,
                method,
                feed_url,
                headers=headers,
                params=params,
                timeout=30,
                stream=True,
                **( {'json': body} if method == 'POST' else {} ),
            )
            try:
                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        content_length_num = int(content_length)
                    except (TypeError, ValueError):
                        content_length_num = None
                    if content_length_num is not None and content_length_num > feed_preview_byte_limit:
                        raise ValueError(
                            f"Feed response exceeds {feed_preview_byte_limit} byte limit"
                        )

                buf = []
                total = 0
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > feed_preview_byte_limit:
                        raise ValueError(
                            f"Feed response exceeds {feed_preview_byte_limit} byte limit"
                        )
                    buf.append(chunk)
                return b''.join(buf)
            finally:
                response.close()

    def _preview_shorten_link(self, link: str) -> str:
        """Shorten a feed link when [Feed_Manager] shorten_urls is enabled."""
        if not (link or '').strip():
            return (link or '')
        shorten_enabled = False
        try:
            shorten_enabled = self.config.getboolean(
                'Feed_Manager', 'shorten_urls', fallback=False
            )
        except Exception:
            shorten_enabled = False
        if not shorten_enabled:
            return link
        try:
            from modules.feed_format import shorten_url_sync
            out = shorten_url_sync(link, config=self.config, logger=self.logger)
            return out if out else link
        except Exception:
            return link

    def _run_test_dm(self, message: str, sender: str = 'web-test') -> list[str]:
        """Run a Web Viewer test DM without queueing anything for the radio."""
        manager = self._test_command_manager()
        return asyncio.run(
            manager.execute_capture(
                message,
                sender_id=sender,
                is_dm=True,
                timeout=30.0,
                skip_cooldown=True,
            )
        )

    def _should_include_item(self, item: dict[str, Any], filter_config: dict) -> bool:
        """Check if an item should be included based on filter configuration (preview; same rules as FeedManager)."""
        from modules.feed_filter_eval import item_passes_filter_config  # noqa: F811

        return item_passes_filter_config(item, filter_config)

    def _sort_items_preview(self, items: list[dict[str, Any]], sort_config: dict) -> list[dict[str, Any]]:
        """Sort items based on sort configuration (standalone version for preview)"""
        if not sort_config or not items:
            return items

        field_path = sort_config.get('field')
        order = sort_config.get('order', 'desc').lower()

        if not field_path:
            return items

        # Helper to get nested values
        def get_nested_value(data, path, default=''):  # noqa: F811
            if not path or not data:
                return default
            parts = path.split('.')
            value = data
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list):
                    try:
                        idx = int(part)
                        if 0 <= idx < len(value):
                            value = value[idx]
                        else:
                            return default
                    except (ValueError, TypeError):
                        return default
                else:
                    return default
                if value is None:
                    return default
            return value if value is not None else default

        def get_sort_value(item):
            """Get the sort value for an item"""
            # Try raw data first
            raw_data = item.get('raw', {})
            value = get_nested_value(raw_data, field_path, '')

            if not value and field_path.startswith('raw.'):
                value = get_nested_value(raw_data, field_path[4:], '')

            if not value:
                value = get_nested_value(item, field_path, '')

            # Handle Microsoft date format
            if isinstance(value, str) and value.startswith('/Date('):
                dt = self._parse_microsoft_date(value)
                if dt:
                    return dt.timestamp()

            # Handle datetime objects
            if isinstance(value, datetime):
                return value.timestamp()

            # Handle numeric values
            if isinstance(value, (int, float)):
                return float(value)

            # Handle string timestamps
            if isinstance(value, str):
                # Try to parse as ISO format
                try:
                    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    return dt.timestamp()
                except ValueError:
                    pass

                # Try common date formats
                for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                    try:
                        dt = datetime.strptime(value, fmt)
                        return dt.timestamp()
                    except ValueError:
                        continue

            # For strings, use lexicographic comparison
            return str(value)

        # Sort items
        try:
            sorted_items = sorted(items, key=get_sort_value, reverse=(order == 'desc'))
            return sorted_items
        except Exception as e:
            self.logger.warning(f"Error sorting items in preview: {e}")
            return items

    def _test_command_manager(self):
        """Return a local CommandManager used for Web Viewer test DMs."""
        cached = getattr(self, '_test_cmd_manager', None)
        if cached is not None:
            return cached

        from modules.command_manager import CommandManager  # noqa: PLC0415
        from modules.plugin_loader import PluginLoader  # noqa: PLC0415

        class _DummyTranslator:
            def translate(self, key: str, **kwargs: Any) -> str:
                return key

            def get_value(self, key: str) -> Any:
                return None

        class _TestBot:
            def __init__(self, app: Any) -> None:
                self.config = app.config
                self.logger = app.logger
                self.db_manager = getattr(app, 'db_manager', None)
                self.bot_root = app.bot_root
                self._local_root = Path(app.bot_root) / 'local'
                self.translator = _DummyTranslator()
                self.command_manager = None
                self.meshcore = None
                self.is_radio_zombie = False
                self.is_radio_offline = False

        bot = _TestBot(self)
        manager = CommandManager(bot)
        bot.command_manager = manager
        plugin_loader = PluginLoader(bot)
        manager.commands = plugin_loader.load_all_plugins()
        self._test_cmd_manager = manager
        return manager

    def run(self, host='127.0.0.1', port=8080, debug=False):
        """Run the modern web viewer"""
        self.logger.info(f"Starting modern web viewer on {host}:{port}")
        self._suppress_werkzeug_headers_error()
        try:
            self.socketio.run(
                self.app,
                host=host,
                port=port,
                debug=debug,
                allow_unsafe_werkzeug=True
            )
        except Exception as e:
            self.logger.error(f"Error running web viewer: {e}")
            raise

    @staticmethod
    def _suppress_werkzeug_headers_error() -> None:
        """Install a log filter that silences the 'Headers already set' AssertionError.

        Werkzeug's dev server catches this internally and continues serving, but it
        logs a full traceback at ERROR level.  The underlying cause (concurrent
        SocketIO polling requests racing through the WSGI layer) is reduced by the
        single-socket-per-page fix, but may still occur occasionally.  The filter
        downgrades these specific records to DEBUG so they don't alarm operators.
        """
        import logging

        class _HeadersAlreadySetFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                msg = record.getMessage()
                return "Headers already set" not in msg

        for name in ("werkzeug", "werkzeug.serving"):
            logging.getLogger(name).addFilter(_HeadersAlreadySetFilter())

def main():
    """Entry point for the meshcore-viewer command"""
    import argparse

    parser = argparse.ArgumentParser(description='MeshCore Bot Data Viewer')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )

    args = parser.parse_args()

    viewer = BotDataViewer(config_path=args.config)
    viewer.run(host=args.host, port=args.port, debug=args.debug)

if __name__ == '__main__':
    main()

def _validate_clock_sync_target(raw: Any) -> tuple[bool, str, str]:
    """Validate a Clock_Sync_Admin target (node name or public key).

    Guards against malicious input (HTML/JS injection, binary/control bytes,
    over-long strings) before it is stored and later rendered in the UI or sent
    as a DM recipient. Returns ``(ok, cleaned_value, error_message)``.

    Accepts either a full 64-char hex public key, or a node name made of unicode
    word characters plus space / hyphen / dot / slash / apostrophe. Everything
    else (``< > " ` & = ( ) { } [ ]`` and control chars) is rejected.
    """
    value = (str(raw) if raw is not None else "").strip()
    if not value:
        return False, "", "target is required"
    # Strip control characters (binary, newlines, tabs, nulls, ANSI escapes) and cap length.
    value = sanitize_name(value, max_length=64).strip()
    if not value:
        return False, "", "target is required"
    if len(value) > 64:
        return False, value, "target too long (max 64 characters)"
    # A full public key is always acceptable (stored lowercased).
    if validate_pubkey_format(value, expected_length=64):
        return True, value.lower(), ""
    # Otherwise a node name: strict allowlist. re.UNICODE is default for str patterns,
    # so \\w covers letters/digits/underscore in any script.
    if re.fullmatch(r"[\w \-\.\/']+", value):
        return True, value, ""
    return (
        False,
        value,
        "target contains invalid characters (allowed: letters, digits, space, - _ . / ')",
    )



def _storage_text(fields: dict[str, Any]) -> str:
    """Render storage status as ``key=value`` lines for a monitoring agent.

    Booleans become 0/1, collections become comma lists, empty values stay empty,
    and a value containing whitespace is single-quoted so one field per line is
    still readable by a shell.
    """
    lines: list[str] = []
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = "1" if value else "0"
        elif isinstance(value, (list, tuple, set)):
            rendered = ",".join(str(item) for item in value)
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        if any(char in rendered for char in (" ", "\t", "\n")):
            rendered = "'" + rendered.replace("'", "") + "'"
        lines.append(f"{key}={rendered}")
    return "\n".join(lines)


