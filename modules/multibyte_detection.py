#!/usr/bin/env python3
"""Shared multibyte / 1-byte path-encoding detection. Single source of truth.

Used by the web viewer (separate process) for the ``/contacts`` path-encoding
badge and by the ``multibyte`` command. Every function is read-only against a
SQLite cursor or connection; none mutate state, so it is safe to call from
either process.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def chunks_from_multibyte_path_hex(path_hex: str, bytes_per_hop: int) -> list[str]:
    """Split path hex into per-hop segments for 2- or 3-byte hop encoding."""
    if not path_hex or bytes_per_hop not in (2, 3):
        return []
    step = bytes_per_hop * 2
    out: list[str] = []
    for i in range(0, len(path_hex), step):
        seg = path_hex[i : i + step]
        if len(seg) == step:
            out.append(seg.lower())
    return out


def bucket_hop_chunks(multibyte_hop_chunks: set[str]) -> dict[int, set[str]]:
    """Bucket hop-prefix chunks by length (4 or 6) for O(1) prefix matching."""
    return {
        4: {c for c in multibyte_hop_chunks if len(c) == 4},
        6: {c for c in multibyte_hop_chunks if len(c) == 6},
    }


def collect_multibyte_hop_chunks(
    cursor: sqlite3.Cursor,
    recent_days: int | None = None,
    logger: Any = None,
) -> set[str]:
    """Hop prefixes from multibyte paths in ``observed_paths``.

    If ``recent_days`` is set (e.g. 7), only paths whose ``last_seen`` falls
    within that window are used. Default (None) keeps full history — used by
    the contacts API badge. Dashboard 7d stats pass ``recent_days=7`` so
    percentages match the chart title.
    """
    chunks: set[str] = set()
    try:
        extra = ""
        if recent_days is not None:
            d = max(1, min(int(recent_days), 366))
            extra = f" AND date(last_seen) >= date('now', '-{d} days')"
        cursor.execute(
            f"""
            SELECT path_hex, bytes_per_hop FROM observed_paths
            WHERE bytes_per_hop IN (2, 3) AND path_hex IS NOT NULL AND length(path_hex) > 0
            {extra}
            """
        )
        for row in cursor.fetchall():
            ph = row[0]
            bph = row[1]
            try:
                bph_i = int(bph) if bph is not None else 0
            except (TypeError, ValueError):
                bph_i = 0
            for c in chunks_from_multibyte_path_hex(ph, bph_i):
                if len(c) in (4, 6):
                    chunks.add(c)
    except Exception as e:
        if logger is not None:
            logger.debug(f"Could not load multibyte hop chunks: {e}")
    return chunks


def compute_path_encoding_badge(
    row: Any,
    all_paths: list[dict[str, Any]],
    multibyte_hop_chunks: set[str],
) -> str | None:
    """Return ``'multibyte'``, ``'one_byte'``, or ``None`` for a contact row.

    ``row`` must expose ``public_key``, ``role``, ``out_bytes_per_hop``,
    ``out_path_len`` and ``advert_count`` (mapping or attribute access).
    ``all_paths`` is a list of dicts, each with a ``bytes_per_hop`` value.
    """
    pk = row["public_key"] or ""
    role = (row["role"] or "").lower()
    obph_raw = row["out_bytes_per_hop"]
    obph: int | None
    try:
        obph = int(obph_raw) if obph_raw is not None else None
    except (TypeError, ValueError):
        obph = None
    if obph is not None and obph not in (1, 2, 3):
        obph = None

    out_path_len = row["out_path_len"]
    if out_path_len is None:
        out_path_len = -1
    try:
        out_path_len = int(out_path_len)
    except (TypeError, ValueError):
        out_path_len = -1

    advert_count = row["advert_count"] or 0

    def norm_bph(b: Any) -> int:
        if b is None:
            return 1
        try:
            i = int(b)
            return i if i in (1, 2, 3) else 1
        except (TypeError, ValueError):
            return 1

    # Multibyte evidence
    if obph in (2, 3):
        return "multibyte"
    for p in all_paths:
        if norm_bph(p.get("bytes_per_hop")) in (2, 3):
            return "multibyte"
    if role in ("repeater", "roomserver") and pk:
        pk_low = pk.lower()
        for chunk in multibyte_hop_chunks:
            if pk_low.startswith(chunk):
                return "multibyte"

    # One-byte: positive signal and no multibyte observation
    has_signal = bool(advert_count > 0 or len(all_paths) > 0 or out_path_len >= 0)
    if not has_signal:
        return None

    if obph is not None and obph != 1:
        return None
    for p in all_paths:
        if norm_bph(p.get("bytes_per_hop")) != 1:
            return None

    return "one_byte"


def find_onebyte_repeaters(
    conn: sqlite3.Connection,
    top_n: int = 5,
    logger: Any = None,
) -> list[dict[str, Any]]:
    """Currently-tracked repeaters/roomservers still on 1-byte, most recent first.

    Uses the same evidence as the ``/contacts`` path-encoding badge. Returns
    up to ``top_n`` dicts with ``name``, ``role``, ``hop_count``,
    ``advert_count`` and ``last_heard``.
    """
    cursor = conn.cursor()
    chunks = collect_multibyte_hop_chunks(cursor, recent_days=None, logger=logger)

    rows = cursor.execute(
        """
        SELECT c.public_key, c.name, c.role, c.hop_count,
               c.advert_count, c.out_path_len, c.out_bytes_per_hop, c.last_heard,
               GROUP_CONCAT(DISTINCT op.bytes_per_hop) AS path_encodings
        FROM complete_contact_tracking c
        LEFT JOIN (
            SELECT public_key, bytes_per_hop FROM observed_paths
            WHERE packet_type = 'advert' AND public_key IS NOT NULL
        ) op ON c.public_key = op.public_key
        WHERE c.role IN ('repeater', 'roomserver') AND c.is_currently_tracked = 1
        GROUP BY c.public_key
        """
    ).fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        row = {
            "public_key": r[0],
            "name": r[1],
            "role": r[2],
            "hop_count": r[3],
            "advert_count": r[4],
            "out_path_len": r[5],
            "out_bytes_per_hop": r[6],
            "last_heard": r[7],
            "path_encodings": r[8],
        }

        all_paths: list[dict[str, Any]] = []
        encodings = row["path_encodings"]
        if encodings:
            for tok in str(encodings).split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    all_paths.append({"bytes_per_hop": int(tok)})
                except ValueError:
                    all_paths.append({"bytes_per_hop": None})

        badge = compute_path_encoding_badge(row, all_paths, chunks)
        if badge != "one_byte":
            continue

        results.append(
            {
                "public_key": row["public_key"],
                "name": row["name"] or (row["public_key"][:8] if row["public_key"] else "Unknown"),
                "role": row["role"],
                "hop_count": row["hop_count"],
                "advert_count": row["advert_count"] or 0,
                "last_heard": row["last_heard"],
            }
        )

    results.sort(key=lambda x: x["last_heard"] or "", reverse=True)
    return results[:top_n]


def count_tracked_repeaters(conn: sqlite3.Connection) -> int:
    """Total currently-tracked repeaters/roomservers (for the summary line)."""
    cursor = conn.cursor()
    cur = cursor.execute(
        """
        SELECT COUNT(*) FROM complete_contact_tracking
        WHERE role IN ('repeater', 'roomserver') AND is_currently_tracked = 1
        """
    )
    return cur.fetchone()[0] or 0


__all__ = [
    "chunks_from_multibyte_path_hex",
    "bucket_hop_chunks",
    "collect_multibyte_hop_chunks",
    "compute_path_encoding_badge",
    "find_onebyte_repeaters",
    "count_tracked_repeaters",
]
