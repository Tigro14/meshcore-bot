"""Small factual summary of the bot's own observations."""


def network_summary(conn):
    known, active, repeaters = conn.execute("""
        SELECT COUNT(DISTINCT public_key),
               COUNT(DISTINCT CASE WHEN datetime(last_heard) BETWEEN
                   datetime('now','localtime','-24 hours') AND datetime('now','localtime')
                   THEN public_key END),
               COUNT(DISTINCT CASE WHEN lower(role)='repeater' AND datetime(last_heard) BETWEEN
                   datetime('now','localtime','-24 hours') AND datetime('now','localtime')
                   THEN public_key END)
        FROM complete_contact_tracking WHERE public_key IS NOT NULL AND public_key != ''
    """).fetchone()
    has_messages = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_stats'").fetchone()
    messages = None
    if has_messages:
        messages = conn.execute("SELECT COUNT(*) FROM message_stats WHERE timestamp BETWEEN CAST(strftime('%s','now') AS INTEGER)-86400 AND CAST(strftime('%s','now') AS INTEGER)").fetchone()[0]
    traffic = f'{messages} messages enregistrés' if messages is not None else 'messages non disponibles'
    return f'Vu par Baliz : {known} nœuds connus. Sur 24 h : {active} entendus, dont {repeaters} relais ; {traffic}.'
