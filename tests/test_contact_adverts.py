import sqlite3
from datetime import datetime, timedelta

from modules.web_viewer.contact_adverts import contact_advert_counts


def test_rolling_windows_deduplicate_and_exclude_future():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE unique_advert_packets(public_key, packet_hash, first_seen)')
    now = datetime(2026, 10, 9, 15, 0)
    for key, packet, age in [
        ('a', 'recent', 1), ('a', 'recent', 2), ('a', 'boundary', 24),
        ('a', 'older', 25), ('a', 'week', 7*24), ('a', 'month', 30*24),
        ('a', 'old', 91*24), ('a', 'future', -1), ('b', 'other', 2),
    ]:
        db.execute('INSERT INTO unique_advert_packets VALUES (?,?,?)',
                   (key, packet, (now-timedelta(hours=age)).isoformat(' ')))
    counts = contact_advert_counts(db.cursor(), now=now)
    assert counts['a'] == {'24h': 2, '7d': 4, '30d': 5, '90d': 5}
    assert counts['b']['24h'] == 1
