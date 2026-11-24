"""Graph degree, as used for map dot size; not packet volume or network load."""
from collections import Counter


def connectivity(conn, hours=24, limit=3, budget=220, min_observations=2):
    cutoff = f'-{hours} hours'
    nodes = conn.execute("""SELECT public_key,name FROM complete_contact_tracking
        WHERE lower(role) IN ('repeater','roomserver')
        AND latitude IS NOT NULL AND longitude IS NOT NULL
        AND latitude != 0 AND longitude != 0
        AND datetime(last_heard)>=datetime('now','localtime',?) ORDER BY name""", (cutoff,)).fetchall()
    names = {key.lower(): name or key[:8] for key,name in nodes if key}

    def resolve(key, prefix):
        if key:
            # Never reassign an absent known endpoint to another contact.
            return key.lower() if key.lower() in names else None
        prefix = (prefix or '').lower()
        matches = [k for k in names if prefix and k.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    degrees = Counter()
    edges = conn.execute("""SELECT from_public_key,to_public_key,from_prefix,to_prefix
        FROM mesh_connections WHERE datetime(last_seen)>=datetime('now','localtime',?)
        AND observation_count>=?""", (cutoff,min_observations))
    for from_key,to_key,from_prefix,to_prefix in edges:
        source,target = resolve(from_key,from_prefix),resolve(to_key,to_prefix)
        if source is None or target is None:
            continue
        # One increment at each end of each directed edge, just like computeNodeStats.
        degrees[source] += 1
        degrees[target] += 1
    if not degrees:
        return f'Aucune liaison identifiable sur {hours} h (minimum {min_observations} observations).'
    lines = [f'Top connectivité / {hours} h (≥{min_observations} obs) :']
    for key in sorted(degrees,key=lambda k:(-degrees[k],str(names[k]),k))[:limit]:
        name = ' '.join(str(names[key]).split()).encode()[:30].decode(errors='ignore')
        line = f'{name} : {degrees[key]} liaisons'
        if len('\n'.join(lines+[line]).encode()) > budget:
            break
        lines.append(line)
    return '\n'.join(lines) if len(lines)>1 else 'Budget insuffisant pour afficher le classement.'
