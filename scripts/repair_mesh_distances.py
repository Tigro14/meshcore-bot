#!/usr/bin/env python3
"""Recalculate mesh_connections.geographic_distance from full public keys.

Historically the distance was pre-computed via prefix resolution at trace time,
which can resolve to a different node than the one stored in
from_public_key/to_public_key (prefix collisions, non-repeater roles),
producing wildly wrong distances (10-300x). The haversine computed from the
stored full public keys is the source of truth.

This script recomputes geographic_distance in place for every edge that has
both a from and a to full public key with a known location. Edges without a
full public key are left untouched.

Idempotent. Run from the meshcore-bot venv against the production DB.

Usage:
    python scripts/repair_mesh_distances.py [--db PATH] [--dry-run]
"""
import argparse
import math
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "/var/lib/meshcore-bot/meshcore_bot.db"
# Only treat a stored value as wrong beyond this tolerance (km).
TOLERANCE_KM = 0.3


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    la1, lo1, la2, lo2 = (math.radians(v) for v in (a[0], a[1], b[0], b[1]))
    dlat = la2 - la1
    dlon = lo2 - lo1
    x = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return r * 2 * math.asin(math.sqrt(x))


def build_location_map(cur: sqlite3.Cursor) -> dict[str, tuple[float, float]]:
    cur.execute(
        """
        SELECT public_key, latitude, longitude
        FROM complete_contact_tracking
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND latitude != 0 AND longitude != 0
        """
    )
    return {row[0]: (float(row[1]), float(row[2])) for row in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to the meshcore bot SQLite DB")
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    loc = build_location_map(cur)
    print(f"Known locations: {len(loc)}")

    cur.execute(
        "SELECT id, from_public_key, to_public_key, geographic_distance FROM mesh_connections"
    )
    rows = cur.fetchall()

    updates: list[tuple[float, int]] = []
    same = 0
    cannot = 0
    max_change = 0.0
    for r in rows:
        fk, tk = r["from_public_key"], r["to_public_key"]
        if fk in loc and tk in loc:
            d = haversine_km(loc[fk], loc[tk])
            old = r["geographic_distance"]
            if old is None or abs(old - d) > TOLERANCE_KM:
                updates.append((d, r["id"]))
                if old is not None:
                    max_change = max(max_change, abs(old - d))
            else:
                same += 1
        else:
            cannot += 1

    print(f"Edges: {len(rows)}  to fix: {len(updates)}  already correct: {same}  "
          f"unresolvable (no full key): {cannot}")
    if updates:
        print(f"Largest correction: {max_change:.2f} km")

    if args.dry_run:
        print("Dry run: no changes written.")
        con.close()
        return 0

    if updates:
        cur.executemany(
            "UPDATE mesh_connections SET geographic_distance = ? WHERE id = ?", updates
        )
        con.commit()
        print(f"Wrote {len(updates)} corrected distances.")
    else:
        print("Nothing to fix.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
