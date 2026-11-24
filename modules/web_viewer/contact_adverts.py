"""Rolling advert counts from locally received, deduplicated advertisements."""
from datetime import datetime, timedelta


def contact_advert_counts(cursor, now=None):
    """Use receive time, not a node's advertised clock or last-heard timestamp.

    Duplicate hashes (including across daily buckets) represent one announcement.
    All-time cumulative totals remain owned by complete_contact_tracking because
    the detailed event history may have a shorter retention period.
    """
    now = now or datetime.now()
    windows = {'24h': 1, '7d': 7, '30d': 30, '90d': 90}
    columns = ', '.join(
        'SUM(CASE WHEN first_seen >= ? AND first_seen <= ? THEN 1 ELSE 0 END)'
        for _ in windows
    )
    params = []
    for days in windows.values():
        params.extend([(now - timedelta(days=days)).isoformat(' '), now.isoformat(' ')])
    rows = cursor.execute(f"""
        WITH announcements AS (
            SELECT public_key, packet_hash, MIN(first_seen) AS first_seen
            FROM unique_advert_packets
            WHERE packet_hash IS NOT NULL AND packet_hash != ''
            GROUP BY public_key, packet_hash
        )
        SELECT public_key, {columns} FROM announcements GROUP BY public_key
    """, params).fetchall()
    return {row[0]: dict(zip(windows, row[1:])) for row in rows}
