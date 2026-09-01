"""Tests for bounded SQLite retention deletes."""

from configparser import ConfigParser
from contextlib import closing, contextmanager
from unittest.mock import Mock

import pytest

from modules.db_retention import (
    delete_timestamp_rows_in_chunks,
    retention_delete_settings,
)


def test_chunked_delete_commits_and_yields_between_batches(
    tmp_path, monkeypatch
):
    import sqlite3

    db_path = tmp_path / "retention.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE events (id INTEGER PRIMARY KEY, seen INTEGER NOT NULL);
            CREATE TABLE live_writes (id INTEGER PRIMARY KEY);
            """
        )
        conn.executemany(
            "INSERT INTO events(seen) VALUES (?)",
            [(1,), (2,), (3,), (4,), (5,), (100,)],
        )
        conn.commit()

    opened_connections = 0

    @contextmanager
    def connection():
        nonlocal opened_connections
        opened_connections += 1
        with closing(sqlite3.connect(db_path, timeout=0.1)) as conn:
            yield conn

    pauses = []

    def live_writer_during_pause(seconds):
        pauses.append(seconds)
        with closing(sqlite3.connect(db_path, timeout=0.1)) as conn:
            conn.execute(
                "INSERT INTO live_writes DEFAULT VALUES"
            )
            conn.commit()

    monkeypatch.setattr(
        "modules.db_retention.time.sleep",
        live_writer_during_pause,
    )

    deleted = delete_timestamp_rows_in_chunks(
        connection,
        "events",
        "seen",
        10,
        batch_size=2,
        pause_seconds=0.01,
    )

    assert deleted == 5
    assert opened_connections == 3
    assert pauses == [0.01, 0.01]
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT seen FROM events").fetchall() == [(100,)]
        assert conn.execute("SELECT COUNT(*) FROM live_writes").fetchone()[0] == 2


def test_chunked_delete_reports_progress_every_ten_full_batches(tmp_path):
    import sqlite3

    db_path = tmp_path / "retention.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, seen INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO events(seen) VALUES (?)",
            [(1,) for _ in range(21)],
        )
        conn.commit()

    @contextmanager
    def connection():
        with closing(sqlite3.connect(db_path)) as conn:
            yield conn

    logger = Mock()
    deleted = delete_timestamp_rows_in_chunks(
        connection,
        "events",
        "seen",
        10,
        batch_size=2,
        pause_seconds=0,
        logger=logger,
        progress_label="test events",
    )

    assert deleted == 21
    logger.info.assert_called_once_with(
        "Retention cleanup progress for %s: %d rows deleted",
        "test events",
        20,
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("events; DROP TABLE events", "seen"),
        ("events", "seen OR 1=1"),
    ],
)
def test_chunked_delete_rejects_invalid_identifiers(table, column):
    with pytest.raises(ValueError, match="Invalid retention"):
        delete_timestamp_rows_in_chunks(
            Mock(),
            table,
            column,
            10,
        )


def test_retention_settings_are_configurable_and_bounded():
    config = ConfigParser()
    config.add_section("Data_Retention")
    config.set("Data_Retention", "retention_delete_batch_size", "25000")
    config.set("Data_Retention", "retention_delete_pause_seconds", "9")

    assert retention_delete_settings(config) == (10_000, 5.0)


# ---------------------------------------------------------------------------
# Neighbor observation retention (modules/maintenance.py)
# ---------------------------------------------------------------------------


def _utc_now():
    """Clock callable MaintenanceRunner takes for injection."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc)


def _maintenance_with_db(tmp_path, config, db_path=None):
    """A MaintenanceRunner bound to a real migrated database."""
    import logging
    import sqlite3
    from contextlib import contextmanager

    from modules.db_migrations import MigrationRunner
    from modules.maintenance import MaintenanceRunner

    logger = logging.getLogger("test-neighbor-retention")
    path = db_path or (tmp_path / "retention_neighbors.db")
    with closing(sqlite3.connect(path)) as conn:
        MigrationRunner(conn, logger).run()

    class DBManager:
        @contextmanager
        def connection(self):
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                yield conn

        def delete_timestamp_rows_in_chunks(self, table, column, cutoff, **kwargs):
            return delete_timestamp_rows_in_chunks(
                self.connection, table, column, cutoff, **kwargs
            )

    bot = Mock()
    bot.config = config
    bot.logger = logger
    bot.db_manager = DBManager()
    return MaintenanceRunner(bot, _utc_now), bot.db_manager, path


def _seed_observations(path, stamps):
    import sqlite3

    with closing(sqlite3.connect(path)) as conn:
        conn.executemany(
            """
            INSERT INTO neighbor_observations
                (observed_at, self_public_key, neighbor_public_key, snr,
                 heard_secs_ago, scopes, status)
            VALUES (?, 'ff', 'aa', 1.0, 0, '', 'responded')
            """,
            [(s,) for s in stamps],
        )
        conn.commit()


def _observation_count(path):
    import sqlite3

    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM neighbor_observations").fetchone()[0]


def test_neighbor_observation_retention_deletes_only_old_rows(tmp_path):
    import datetime

    maintenance, _, path = _maintenance_with_db(tmp_path, ConfigParser())
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = (now - datetime.timedelta(days=5)).isoformat()
    ancient = (now - datetime.timedelta(days=500)).isoformat()
    _seed_observations(path, [recent, ancient, ancient])

    maintenance._cleanup_neighbor_observations(365)
    assert _observation_count(path) == 1


def test_neighbor_observation_retention_disabled_when_not_positive(tmp_path):
    import datetime

    maintenance, _, path = _maintenance_with_db(tmp_path, ConfigParser())
    ancient = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5000)
    ).isoformat()
    _seed_observations(path, [ancient])

    maintenance._cleanup_neighbor_observations(0)
    assert _observation_count(path) == 1


def test_neighbor_observation_retention_survives_a_pre_migration_database(tmp_path):
    """A database without migration 22 must not abort the whole retention run."""
    import logging
    import sqlite3
    from contextlib import contextmanager

    from modules.maintenance import MaintenanceRunner

    path = tmp_path / "old.db"
    sqlite3.connect(path).close()

    class DBManager:
        @contextmanager
        def connection(self):
            with closing(sqlite3.connect(path)) as conn:
                yield conn

        def delete_timestamp_rows_in_chunks(self, table, column, cutoff, **kwargs):
            return delete_timestamp_rows_in_chunks(
                self.connection, table, column, cutoff, **kwargs
            )

    bot = Mock()
    bot.config = ConfigParser()
    bot.logger = logging.getLogger("test-neighbor-retention")
    bot.db_manager = DBManager()

    # Must not raise.
    MaintenanceRunner(bot, _utc_now)._cleanup_neighbor_observations(365)


def test_neighbor_observation_retention_tolerates_a_missing_db_manager(tmp_path):
    import logging

    from modules.maintenance import MaintenanceRunner

    bot = Mock()
    bot.config = ConfigParser()
    bot.logger = logging.getLogger("test-neighbor-retention")
    bot.db_manager = None

    MaintenanceRunner(bot, _utc_now)._cleanup_neighbor_observations(365)
