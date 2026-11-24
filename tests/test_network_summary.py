import sqlite3

from modules.assistant.network_summary import network_summary


def test_summary_window_roles_and_unknown_identities():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,role,last_heard)')
    c.execute("INSERT INTO complete_contact_tracking VALUES ('a','repeater',datetime('now','localtime'))")
    c.execute("INSERT INTO complete_contact_tracking VALUES ('b','companion',datetime('now','localtime'))")
    c.execute("INSERT INTO complete_contact_tracking VALUES ('c','repeater',datetime('now','localtime','-2 days'))")
    c.execute("INSERT INTO complete_contact_tracking VALUES ('d','repeater',datetime('now','localtime','+2 days'))")
    c.execute("INSERT INTO complete_contact_tracking VALUES ('','repeater',datetime('now','localtime'))")
    assert network_summary(c) == 'Vu par Baliz : 4 nœuds connus. Sur 24 h : 2 entendus, dont 1 relais ; messages non disponibles.'
    c.execute('CREATE TABLE message_stats(timestamp)')
    c.execute("INSERT INTO message_stats VALUES (CAST(strftime('%s','now') AS INTEGER))")
    c.execute("INSERT INTO message_stats VALUES (CAST(strftime('%s','now') AS INTEGER)-90000)")
    assert network_summary(c).endswith('1 messages enregistrés.')


def test_summary_is_in_closed_catalog():
    import json

    from modules.assistant.catalog import CATALOG, parse_plan
    from modules.assistant.network_plan import argument_schema

    plan = parse_plan(json.dumps({'route': 'mesh', 'operation': 'summary', 'args': {}}), 'reseau', set(CATALOG))
    assert plan.operation == 'summary'
    assert argument_schema('summary')['properties'] == {}
