"""SQLite retention helpers that keep writer lock hold times bounded."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

DEFAULT_RETENTION_DELETE_BATCH_SIZE = 1000
DEFAULT_RETENTION_DELETE_PAUSE_SECONDS = 0.1
_MAX_RETENTION_DELETE_BATCH_SIZE = 10_000
_MAX_RETENTION_DELETE_PAUSE_SECONDS = 5.0
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def retention_delete_settings(config: Any) -> tuple[int, float]:
    """Return bounded chunk size and inter-batch pause from configuration."""
    batch_size = DEFAULT_RETENTION_DELETE_BATCH_SIZE
    pause_seconds = DEFAULT_RETENTION_DELETE_PAUSE_SECONDS
    try:
        batch_size = int(
            config.getint(
                "Data_Retention",
                "retention_delete_batch_size",
                fallback=batch_size,
            )
        )
    except (AttributeError, TypeError, ValueError):
        batch_size = DEFAULT_RETENTION_DELETE_BATCH_SIZE
    try:
        pause_seconds = float(
            config.getfloat(
                "Data_Retention",
                "retention_delete_pause_seconds",
                fallback=pause_seconds,
            )
        )
    except (AttributeError, TypeError, ValueError):
        pause_seconds = DEFAULT_RETENTION_DELETE_PAUSE_SECONDS

    return (
        max(1, min(batch_size, _MAX_RETENTION_DELETE_BATCH_SIZE)),
        max(0.0, min(pause_seconds, _MAX_RETENTION_DELETE_PAUSE_SECONDS)),
    )


def delete_timestamp_rows_in_chunks(
    connection_factory: Callable[[], AbstractContextManager[sqlite3.Connection]],
    table: str,
    timestamp_column: str,
    cutoff: Any,
    *,
    batch_size: int = DEFAULT_RETENTION_DELETE_BATCH_SIZE,
    pause_seconds: float = DEFAULT_RETENTION_DELETE_PAUSE_SECONDS,
    future_cutoff: Any | None = None,
    logger: Any | None = None,
    progress_label: str | None = None,
) -> int:
    """Delete timestamped rows in short committed transactions.

    A fresh connection is acquired for every batch. Each commit releases
    SQLite's writer lock, and the optional pause gives live packet/contact/graph
    writers an opportunity to proceed before retention acquires it again.
    """
    for value, kind in ((table, "table"), (timestamp_column, "column")):
        if not _VALID_IDENTIFIER.fullmatch(value):
            raise ValueError(f"Invalid retention {kind} identifier: {value!r}")
    if batch_size < 1:
        raise ValueError("Retention delete batch_size must be at least 1")
    if pause_seconds < 0:
        raise ValueError("Retention delete pause_seconds cannot be negative")

    table_sql = f'"{table}"'
    column_sql = f'"{timestamp_column}"'
    where_sql = f"{column_sql} < ?"
    comparison_params: tuple[Any, ...] = (cutoff,)
    if future_cutoff is not None:
        where_sql += f" OR {column_sql} > ?"
        comparison_params += (future_cutoff,)

    delete_sql = (
        f"DELETE FROM {table_sql} WHERE rowid IN ("  # noqa: S608 - identifiers validated above
        f"SELECT rowid FROM {table_sql} WHERE {where_sql} LIMIT ?)"
    )
    total_deleted = 0
    completed_batches = 0
    label = progress_label or table

    while True:
        with connection_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(delete_sql, (*comparison_params, batch_size))
            deleted = max(0, cursor.rowcount)
            conn.commit()

        total_deleted += deleted
        completed_batches += 1
        if deleted < batch_size:
            break

        if logger is not None and completed_batches % 10 == 0:
            logger.info(
                "Retention cleanup progress for %s: %d rows deleted",
                label,
                total_deleted,
            )
        if pause_seconds:
            time.sleep(pause_seconds)

    return total_deleted
