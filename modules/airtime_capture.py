"""Local capture continuity: metadata only, no polling or radio transmissions."""
from __future__ import annotations

import math
import sqlite3


def create_tables(cursor: sqlite3.Cursor) -> None:
    cursor.execute('CREATE TABLE IF NOT EXISTS airtime_capture_intervals ('
                   'start_at REAL PRIMARY KEY, end_at REAL NOT NULL CHECK(end_at>start_at))')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_airtime_capture_end ON airtime_capture_intervals(end_at)')


class ListeningRecorder:
    """Only join two healthy ticks in this process, never across a restart/gap."""
    def __init__(self) -> None:
        self.previous: tuple[float, float, bool] | None = None
        self.last_prune: float | None = None

    def record(self, conn: sqlite3.Connection, wall: float, mono: float, ready: bool,
               loss_at: float = 0) -> None:
        previous = self.previous
        self.previous = None  # Failed writes must break continuity too.
        if previous:
            before, before_mono, before_ready = previous
            elapsed = mono-before_mono
            if (ready and before_ready and 0 < elapsed <= 75 and
                    abs((wall-before)-elapsed) <= 2 and loss_at < before):
                conn.execute('INSERT OR IGNORE INTO airtime_capture_intervals VALUES (?,?)', (before, wall))
        if self.last_prune is None or mono-self.last_prune >= 3600:
            conn.execute('DELETE FROM airtime_capture_intervals WHERE end_at<?', (wall-30*86400,))
            self.last_prune = mono
        conn.commit()
        self.previous = (wall, mono, ready)


def listening_intervals(conn: sqlite3.Connection, start: float, end: float) -> list[tuple[float, float]]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_capture_intervals'").fetchone():
        return []
    return [(float(left), float(right)) for left, right in conn.execute(
        'SELECT start_at,end_at FROM airtime_capture_intervals WHERE end_at>? AND start_at<? ORDER BY start_at',
        (start, end))]


def covered_buckets(intervals: list[tuple[float, float]], start: float, end: float,
                    step: int) -> list[float]:
    """Union intervals; duplicates/overlap never manufacture full coverage."""
    merged: list[tuple[float, float]] = []
    for left, right in sorted(intervals):
        if not (math.isfinite(left) and math.isfinite(right)):
            continue
        left, right = max(start, left), min(end, right)
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    coverage, index = [], 0
    for bucket in range(max(1, math.ceil((end-start)/step))):
        left, right = start+bucket*step, min(end, start+(bucket+1)*step)
        while index < len(merged) and merged[index][1] <= left:
            index += 1
        total, current = 0.0, index
        while current < len(merged) and merged[current][0] < right:
            total += max(0, min(right, merged[current][1])-max(left, merged[current][0]))
            current += 1
        coverage.append(total)
    return coverage
