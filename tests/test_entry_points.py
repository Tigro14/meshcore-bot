import json
import sqlite3

from modules.web_viewer.entry_points import entry_points


def test_entry_points_direction_dedup_collision_and_time():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key,country in [('aaaa11','United Kingdom'),('bbbb22','France'),('cccc33','Guernsey'),('dddd44','France'),('eeee55',None),('ffff11','France'),('ffff22','France')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)',(key,key,country,48,-3,'repeater'))
    def packet(path,hash='1234',time=99999,route='FLOOD',width=2):
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)',(time,'packet',json.dumps(dict(path_hex=path,packet_hash=hash,route_type_name=route,bytes_per_hop=width))))
    packet('aaaabbbb');packet('aaaabbbb')  # duplicates
    packet('ccccbbbb')  # same packet via a different foreign relay: counted once per entry
    packet('aaaabbbbdddd','2345')  # internal French edge must not count
    packet('bbbbaaaa','3456')  # outbound
    packet('aaaaffff','4567')  # collision
    packet('eeeeaaaa','5678')  # unknown country
    packet('aaaabbbb','6789',time=1)  # outside 24 h
    packet('aaaabbbb','7890',route='DIRECT')  # planned path
    packet('aabb','8901',width=1)  # unambiguous single byte is accepted
    packet('aaaaeeeebbbb','9012')  # never bridge across unknown relay
    d=entry_points(c,1,now=100000)
    assert len(d['ranking'])==1
    assert d['ranking'][0]['public_key']=='bbbb22'
    assert d['ranking'][0]['count']==3
    assert len(d['edges'])==2
    assert d['countries']==['Guernsey','United Kingdom']
    assert d['excluded_segments']>0
    assert d['excluded_packets']==0
    assert entry_points(c,1,'Guernsey',now=100000)['ranking'][0]['count']==1
    assert entry_points(c,7,now=100000)['ranking'][0]['count']==4


def test_arrival_hops_shortest_path_and_unscoped_filter():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key, country in [('aaaa11', 'United Kingdom'), ('bbbb22', 'France'), ('cccc33', 'Guernsey')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)',
                  (key, key, country, None, None, 'repeater'))
    def add(path, packet, route='FLOOD', width=2):
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)', (99999, 'packet', json.dumps(dict(
            path_hex=path, packet_hash=packet, route_type_name=route, bytes_per_hop=width))))
    add('9999ccccaaaabbbb', '1234')  # unknown preceding relay still occupies a hop
    add('aaaabbbb', '1234')  # shortest arrival wins
    add('ccccaaaabbbb', '2345')
    add('ccccaaaabbbb', '2345')  # duplicate
    add('ccccaaaabbbb', '3456', 'TRANSPORT_FLOOD')
    add('aaaa11bbbb22', '4567', width=3)
    result = entry_points(c, now=100000)
    assert result['ranking'][0]['arrival_hops'] == [{'hops': 1, 'count': 2}, {'hops': 2, 'count': 2}]
    filtered = entry_points(c, now=100000, unscoped_only=True)
    node = filtered['ranking'][0]
    assert filtered['unscoped_only'] is True
    assert node['count'] == 3
    assert node['arrival_hops'] == [{'hops': 1, 'count': 2}, {'hops': 2, 'count': 1}]
    assert sum(b['count'] for b in node['arrival_hops']) == node['count']


def test_global_histogram_deduplicates_across_gateways_and_respects_filters():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key, country in [('aaaa11', 'United Kingdom'), ('bbbb22', 'France'), ('cccc33', 'Guernsey'), ('dddd44', 'France')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)',
                  (key, key, country, 48, -3, 'repeater'))
    for path, packet, route in [('ccccaaaabbbb', '1234', 'FLOOD'), ('ccccdddd', '1234', 'FLOOD'), ('aaaabbbb', '2345', 'TRANSPORT_FLOOD')]:
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)', (99999, 'packet', json.dumps(dict(
            path_hex=path, packet_hash=packet, route_type_name=route, bytes_per_hop=2))))
    result = entry_points(c, now=100000)
    assert sum(n['count'] for n in result['ranking']) == 3
    assert result['global_summary'] == {'count': 2, 'arrival_hops': [{'hops': 1, 'count': 2}]}
    filtered = entry_points(c, now=100000, country='United Kingdom', unscoped_only=True)
    assert filtered['global_summary'] == {'count': 1, 'arrival_hops': [{'hops': 2, 'count': 1}]}
    assert entry_points(c, now=100000, country='Spain')['global_summary'] == {'count': 0, 'arrival_hops': []}


def test_packet_type_filters():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key, country in [('aaaa11', 'United Kingdom'), ('bbbb22', 'France')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)', (key, key, country, 48, -3, 'repeater'))
    for i, payload in enumerate(['TXT_MSG', 'GRP_TXT', 'ADVERT', 'ACK', 'UNKNOWN'], 1):
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)', (99999, 'packet', json.dumps(dict(
            path_hex='aaaabbbb', packet_hash=str(i), route_type_name='FLOOD', bytes_per_hop=2, payload_type=i, payload_type_name=payload))))
    for kind, expected in [('all', 5), ('messages', 2), ('adverts', 1)]:
        result = entry_points(c, now=100000, packet_type=kind, unscoped_only=True)
        assert result['global_summary']['count'] == expected
        assert result['ranking'][0]['count'] == expected
        assert result['edges'][0]['count'] == expected


def test_foreign_ambiguity_without_inventing_identity_or_location():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key, country in [('aa11', 'United Kingdom'), ('aa22', 'United Kingdom'),
                         ('bb11', 'France'), ('cc11', 'Guernsey'), ('cc22', 'United Kingdom'),
                         ('dd11', 'France'), ('dd22', 'United Kingdom'),
                         ('ee11', None), ('ee22', 'United Kingdom'), ('bb22', 'France')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)', (key, key, country, 48, -3, 'repeater'))
    # Two-byte destination bb11 is unique, source aa is ambiguous at one byte:
    # use a different unique French prefix for the one-byte paths.
    c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)', ('ff11', 'French', 'France', 48, -3, 'repeater'))
    for i, path in enumerate(['aaff', 'ccff', 'ddff', 'eeff', 'aabb'], 1):
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)', (99999, 'packet', json.dumps(dict(
            path_hex=path, packet_hash=str(i), route_type_name='FLOOD', bytes_per_hop=1))))
    result = entry_points(c, now=100000)
    assert result['global_summary']['count'] == 2
    assert all(e['reliable_count']==0 for e in result['edges'])


def test_excluded_units_and_short_path_evidence():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key, country in [('aaaa', 'United Kingdom'), ('bbbb', 'France')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)', (key, key, country, 48, -3, 'repeater'))
    for path, width, packet in [('aabb',1,'12'), ('aaaabbbb',2,'12'), ('aabb',1,'13'), ('ccddeeff',1,'14'), ('x',1,'15')]:
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)', (99999,'packet',json.dumps(dict(path_hex=path,bytes_per_hop=width,packet_hash=packet,route_type_name='FLOOD'))))
    d=entry_points(c,now=100000)
    assert d['excluded_packets']==1
    assert d['excluded_segments']==3
    assert d['edges'][0]['count']==2
    assert d['edges'][0]['multi_byte_count']==1
    assert d['edges'][0]['short_only_count']==1


def test_foreign_following_relay_excludes_entry_but_unknown_is_dashed():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key,nation in [('aa11','United Kingdom'),('bb11','France'),('cc11','France'),('dd11','United Kingdom')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)',(key,key,nation,48,-3,'repeater'))
    def add(path,width,hash):
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)',(99999,'packet',json.dumps(dict(path_hex=path,bytes_per_hop=width,packet_hash=hash,route_type_name='FLOOD'))))
    add('aabbdd',1,'123')
    assert entry_points(c,now=100000)['ranking']==[]
    assert entry_points(c,now=100000)['foreign_suffix_segments']==1
    add('aabbcc',1,'456')
    d=entry_points(c,now=100000)
    assert d['edges'][0]['reliable_count']==0
    add('aa11bb11cc11',2,'789')
    assert entry_points(c,now=100000)['edges'][0]['reliable_count']==1
    add('aa11bb11eeee',2,'abc')
    assert entry_points(c,now=100000)['edges'][0]['inferred_count']==2


def test_integrated_filters_recompute_ranking_and_global_histogram():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking(public_key,name,country,latitude,longitude,role)')
    c.execute('CREATE TABLE packet_stream(timestamp,type,data)')
    for key, country in [('aaaa11', 'United Kingdom'), ('bbbb22', 'France'), ('cccc33', 'France')]:
        c.execute('INSERT INTO complete_contact_tracking VALUES(?,?,?,?,?,?)', (key, key, country, 48, -3, 'repeater'))
    for path, packet, width in [('aaaabbbbcccc','1111',2), ('aaaabbbbcccc','2222',2), ('aaaaccccbbbb','1111',2), ('aabbcc','3333',1)]:
        c.execute('INSERT INTO packet_stream VALUES(?,?,?)', (99999, 'packet', json.dumps(dict(path_hex=path,packet_hash=packet,route_type_name='FLOOD',bytes_per_hop=width))))
    result = entry_points(c, now=100000, target_keys={'bbbb22'}, evidence='multibyte', min_packets=2)
    assert len(result['edges']) == len(result['ranking']) == 1
    assert result['ranking'][0]['public_key'] == 'bbbb22'
    assert result['global_summary'] == {'count':2, 'arrival_hops':[{'hops':1, 'count':2}]}
    assert entry_points(c, now=100000, min_packets=4)['global_summary']['count'] == 0
    assert entry_points(c, now=100000, target_keys=set())['ranking'] == []
    assert entry_points(c, days=0, now=100000)['global_summary']['count'] == 3
