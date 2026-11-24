import sqlite3
from modules.assistant.relay_connectivity import connectivity


def test_degree_not_volume_and_filters():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,role,latitude,longitude,last_heard)')
    for key,name in [('aabb','Alpha'),('bbcc','Bravo'),('ccdd','Charlie'),('dddd','Old')]:
        c.execute("INSERT INTO complete_contact_tracking VALUES (?,?,'repeater',48,-4,datetime('now',?))",(key,name,'-2 days' if name=='Old' else '-1 hour'))
    c.execute('CREATE TABLE mesh_connections(from_public_key,to_public_key,from_prefix,to_prefix,observation_count,last_seen)')
    for a,b,n,age in [('aabb','bbcc',2,'-1 hour'),('bbcc','aabb',2,'-1 hour'),('bbcc','ccdd',999,'-1 hour'),('aabb','ccdd',1,'-1 hour'),('ccdd','aabb',99,'-2 days'),('ccdd','dddd',99,'-1 hour')]:
        c.execute("INSERT INTO mesh_connections VALUES (?,?,?,?,?,datetime('now',?))",(a,b,a[:2],b[:2],n,age))
    result=connectivity(c,budget=220)
    assert result.index('Bravo : 3')<result.index('Alpha : 2')<result.index('Charlie : 1')
    assert 'paquet' not in result and 'Old' not in result
    assert len(connectivity(c,budget=158).encode())<=158
    # Ambiguous prefix must never be assigned to an arbitrary contact.
    c.execute("INSERT INTO complete_contact_tracking VALUES ('aaee','Collision','repeater',48,-4,datetime('now'))")
    c.execute("INSERT INTO mesh_connections VALUES (NULL,'ccdd','aa','cc',9,datetime('now'))")
    assert 'Charlie : 1' in connectivity(c,budget=220)
