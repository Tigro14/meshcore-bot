"""Typed network plans. User text supplies parameters; the model cannot invent them."""
import re
from .router import normalize

ACTIVE = {'neighbors', 'trace', 'advert'}

def infer_operation(question):
    q = normalize(question).strip(' ?!.')
    if re.search(r"\b(comment|explique|definition|what is|how does|qu.est.ce|c.est quoi|ne|pas|not|sans)\b", q):
        return None
    if re.search(r"^(annonce[- ]toi|advert|announce yourself|envoie (?:une|ton) annonce)\b", q): return 'advert'
    if re.search(r"^(trace|tracer|teste? le (chemin|trajet)|lance (?:un )?diagnostic de liaison)\b", q): return 'trace'
    if re.search(r"\b(voisins? directs?|neighbors|decouvr\w* les voisins)\b", q): return 'neighbors'
    if re.search(r"\b(proches?|nearest|closest|near)\b", q): return 'near'
    if re.search(r"\b(stats|statistiques|statistics|activite du reseau)\b", q): return 'stats'
    if re.search(r"\b1\s*(octet|byte)\b", q): return 'onebyte'
    if re.search(r"\b(repeteurs?|repeaters?)\b", q):
        if re.search(r'\b(combien|nombre|how many)\b', q): return 'count_repeaters'
        if re.search(r'\b(liste|list|quels|plus actifs|most active)\b', q): return 'list_repeaters'
    return None


def parameters(operation, question):
    q = normalize(question)
    args = {}
    if operation in {'count_repeaters', 'list_repeaters', 'onebyte'}:
        args = {'hours': 0, 'country': '', 'limit': 5, 'sort': 'recent'}
        time = re.search(r'\b(\d+)\s*(h(?:eures?)?|hours?|j(?:ours?)?|days?)\b', q)
        if time:
            args['hours'] = int(time[1]) * (24 if time[2].startswith(('j','d')) else 1)
            if not 1 <= args['hours'] <= 8760: raise ValueError('Période hors limites (1 h à 365 jours).')
        elif re.search(r"\b(actifs?|active|aujourd'hui|today)\b", q): args['hours'] = 24
        elif re.search(r'\b(depuis|heures?|jours?|hier|yesterday|last)\b', q): raise ValueError('Période non reconnue : indique un nombre d’heures ou de jours.')
        if re.search(r'\b(inactifs?|pas entendus|non entendus)\b', q): raise ValueError('Le filtre des nœuds inactifs n’est pas encore disponible.')
        country_question = re.sub(r'\b(?:en|in)\s+1\s*(?:octet|byte)s?\b', '', question, flags=re.I)
        match = re.search(r'\b(?:en|in|au|aux)\s+(.+?)(?=\s+(?:depuis|sur|entendus?|actifs?|ces|dans|au cours)\b|[?!]|$)', country_question, re.I)
        if match and not re.match(r'1\s*(octet|byte)', normalize(match[1])): args['country'] = match[1].strip(' .')
        if re.search(r'\b(plus actifs|most active|annonces|adverts)\b',q): args['sort']='activity'
    if operation in {'near','list_repeaters','onebyte'}:
        count = re.search(r'\b(?:les|top|liste|list|near)\s+(\d+)\b', q)
        args['limit'] = int(count[1]) if count else 5
        if not 1 <= args['limit'] <= 20: raise ValueError('Le nombre de résultats doit être compris entre 1 et 20.')
    if operation == 'near':
        args.update(role='repeater', target='')
        for word,role in [('capteur','sensor'),('sensor','sensor'),('compagnon','companion'),('companion','companion'),('roomserver','roomserver')]:
            if word in q: args['role']=role
        target=re.search(r'\b(?:de|of|near)\s+([0-9a-f]{4})\b',q)
        if target: args['target']=target[1]
        elif re.search(r'\b(?:de|of)\s+(?!moi\b|toi\b|me\b|you\b)([\w-]+)\s*[?!.]*$',q):
            raise ValueError('Pour cibler un autre nœud, indique son préfixe de clé sur 4 caractères.')
    if operation=='stats':
        args={'topic':'general','hashes': bool(re.search(r'\b(hash|hashes|verbose)\b',q))}
        for pattern,topic in [(r'messages?|expediteurs?', 'messages'),(r'canaux|channels?', 'channels'),(r'chemins?|paths?', 'paths'),(r'annonces?|adverts?', 'adverts')]:
            if re.search(r'\b(?:'+pattern+r')\b',q): args['topic']=topic
    if operation=='trace':
        args={'path':'','target':'','roundtrip':True}
        path=re.search(r'\b[0-9a-f]{2,6}(?:\s*,\s*[0-9a-f]{2,6})+\b',q)
        if path:
            nodes=[n.strip() for n in path[0].split(',')]
            if len({len(n) for n in nodes})!=1 or len(nodes[0]) not in (2,4) or len(nodes)>32:
                raise ValueError('Chemin trace invalide : utilise des préfixes homogènes de 1 ou 2 octets.')
            args['path']=','.join(nodes)
        else:
            compact=re.fullmatch(r'tracer?\s+([0-9a-f]{2,64})[?!.]*',q)
            if compact:
                raw=compact[1]
                if len(raw)%2: raise ValueError('Chemin hexadécimal incomplet.')
                args['path']=','.join(raw[i:i+2] for i in range(0,len(raw),2))
            target=re.search(r'\b(?:vers|to)\s+(.+?)[?!.]*$',question,re.I)
            if target and normalize(target[1]) not in {'moi','toi','me','you'}: args['target']=target[1].strip(' ?.')
        if re.search(r'\b(aller simple|one way)\b',q): args['roundtrip']=False
    return args


def argument_schema(operation):
    specs={
      'count_nodes': {'hours':{'type':'integer','minimum':0,'maximum':8760},'country':{'type':'string'}},
      'relay_connectivity': {'hours':{'type':'integer','minimum':1,'maximum':8760},'limit':{'type':'integer','minimum':1,'maximum':10}},
      'count_repeaters': {'hours':{'type':'integer','minimum':0,'maximum':8760},'country':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':20},'sort':{'type':'string','enum':['recent','activity']}},
      'near':{'limit':{'type':'integer','minimum':1,'maximum':20},'role':{'type':'string','enum':['repeater','sensor','companion','roomserver']},'target':{'type':'string'}},
      'stats':{'topic':{'type':'string','enum':['general','messages','channels','paths','adverts']},'hashes':{'type':'boolean'}},
      'trace':{'path':{'type':'string'},'target':{'type':'string'},'roundtrip':{'type':'boolean'}},
    }
    specs['list_repeaters']=specs['onebyte']=specs['count_repeaters']
    props=specs.get(operation,{})
    return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}
