"""Closed assistant capabilities and strict model-output validation."""
import json
import re

from .network_plan import ACTIVE, infer_operation
from .router import Decision, Route, normalize

CATALOG = {
    'test': {'receive': 'Réception du message actuel : test, ping, tu me reçois, SNR, RSSI. Pas un test actif distant.'},
    'path': {'message': 'Chemin et noms des relais traversés par le message actuel.',
             'hops': 'Nombre de sauts du message actuel jusqu’à Baliz : combien de sauts jusqu’à toi, entre nous, combien de relais pour te joindre. Mesure reçue ; ne demande pas de précision.'},
    'mesh': {
        'summary': 'Résumé du réseau : nœuds connus, actifs, relais et messages sur 24h.',
        'hfcond': 'Bulletin de propagation HF. Aussi conditions radio générales et tropo ; aucune prévision tropo locale.',
        'relay_connectivity': 'Classement des relais par nombre de liens observés. hours=24,limit=3.',
        'count_nodes': 'Nombre de nœuds tous rôles. Actifs : hours=24 ; connus : hours=0.',
        'count_repeaters': 'Nombre de répéteurs. hours=0 par défaut. Aucun classement.',
        'list_repeaters': 'Noms des répéteurs connus, sort=recent par défaut. Jamais les relais traversés par un message : path.message. activity seulement sur demande de classement par annonces.',
        'onebyte': 'Relais utilisant un routage de 1 octet, selon les observations.',
        'near': 'Relais proches géographiquement ; utilise GPS ou premier relais comme repère. Gère la position absente.',
        'stats': 'Statistiques 24h du trafic, jamais mesures des capteurs.',
        'neighbors': 'Recherche radio active des voisins directs, en privé uniquement.',
        'trace': 'Test actif de trajet radio vers une cible ou un chemin explicite.',
        'advert': 'Demande explicite que le bot émette une annonce radio, en privé uniquement.',
        'unsupported': 'Mesure réseau précise indisponible. Jamais une demande documentée, sociale ou un simple test.'},
    'info': {'provenance': 'Expliquer d’où Baliz obtient ses données et comment ses mesures sont calculées. topic=position pour l’origine des coordonnées, clock pour le calcul de dérive, assistant pour le nom du modèle LLM configuré, overview sans catégorie. Autres catégories : network/paths/wiki/airtime/bbs. Fournisseur météo : weather.provider. Seulement sur demande explicite, jamais pour donner une mesure ou un résumé.'},
    'bbs': {
        'send': 'Déposer un message privé pour un compagnon connu. recipient=nom exact demandé ou vide, text=texte exact après deux-points ou entre guillemets, sinon vide : demander le contenu. Jamais le mot message de la consigne. Ne réécris jamais le message.',
        'count': 'Compter mes messages privés BBS en attente.',
        'read': 'Lire les messages de ma boîte aux lettres privée BBS.',
        'continue': 'Réponse à une précision BBS en cours seulement. selection=nom ou numéro copié de la réponse, text=contenu exact si demandé, sinon vide.',
        'cancel': 'Annuler un envoi BBS en cours.'},
    'weather': {'forecast': 'Prévisions météo : température, pluie et vent. location=lieu exact demandé ou vide ; period=today/tomorrow.',
                'provider': 'Fournisseur ou source des prévisions météo utilisés par Baliz : qui te fournit la météo, où prends-tu tes prévisions. Réglage connu, pas une mesure de télémétrie ni une prévision ; aucun argument.'},
    'wiki': {'lookup': 'Documentation MeshCore : configuration, régions/scopes, région par défaut sur compagnon, routage, canaux à rejoindre.'},
    'llm': {'clarify': 'Donnée essentielle manquante ; meilleur relais sans critère, télémétrie sans mesure précisée.',
            'chat': 'Conversation, salutation, remerciement, reproche ; pas les questions de documentation MeshCore.'},
}


# Model-facing functions name their actual result. Internally the existing stats
# adapter and its permission checks remain shared; no input text is inspected.
STATS_FUNCTIONS = {
    'bot_users': ('messages', 'Rank the people who called Baliz most often in 24h, with call counts.'),
    'channel_traffic': ('channels', 'Rank channels by observed message count and users over 24h. Not documented channels to join.'),
    'bot_usage': ('general', 'Count bot calls and replies, top command and top bot user over 24h.'),
    'longest_paths': ('paths', 'Show longest radio paths observed over 24h.'),
    'advert_counts': ('adverts', 'Count observed node announcements over 24h.'),
}


# Few-shot examples teach intent, never execute lexical overrides.
CLARIFICATION_EXAMPLES = [
    ('statistiques de la télémétrie ?', {'route': 'llm', 'operation': 'clarify', 'args': {}}),
    ('tu peux me donner les chiffres des mesures ?', {'route': 'llm', 'operation': 'clarify', 'args': {}}),
    ('tu peux vérifier ça ?', {'route': 'llm', 'operation': 'clarify', 'args': {}}),
]

# Short contrasting dialogues: ambiguity, social reply and a precise tool request.
ROUTING_DIALOG_EXAMPLES = CLARIFICATION_EXAMPLES[:1] + [
    ('quel est le meilleur répéteur ?', {'route': 'llm', 'operation': 'clarify', 'args': {}}),
    ('les répéteurs avec le plus de liens', {'route': 'mesh', 'operation': 'relay_connectivity', 'args': {'hours': 24, 'limit': 3}}),
    ('combien de répéteurs dans le réseau ?', {'route': 'mesh', 'operation': 'count_repeaters', 'args': {'hours': 0, 'country': '', 'limit': 5, 'sort': 'recent'}}),
    ('nombre de nœuds actifs', {'route': 'mesh', 'operation': 'count_nodes', 'args': {'hours': 24, 'country': ''}}),
    ('les relais autour de moi', {'route': 'mesh', 'operation': 'near', 'args': {'limit': 5, 'role': 'repeater', 'target': ''}}),
    ('Par quels relais passe mon message ?', {'route': 'path', 'operation': 'message', 'args': {}}),
    ('Combien de sauts jusqu’à toi ?', {'route': 'path', 'operation': 'hops', 'args': {}}),
    ('À combien de relais suis-je de toi ?', {'route': 'path', 'operation': 'hops', 'args': {}}),
    ('D’où viennent tes informations ?', {'route': 'info', 'operation': 'provenance', 'args': {'topic': 'overview'}}),
    ('Quelle est la source de tes statistiques réseau ?', {'route': 'info', 'operation': 'provenance', 'args': {'topic': 'network'}}),
    ('D’où proviennent les positions des nœuds ?', {'route': 'info', 'operation': 'provenance', 'args': {'topic': 'position'}}),
    ('D’où vient la mesure de dérive d’horloge ?', {'route': 'info', 'operation': 'provenance', 'args': {'topic': 'clock'}}),
    ('Comment calcules-tu l’occupation radio ?', {'route': 'info', 'operation': 'provenance', 'args': {'topic': 'airtime'}}),
    ('Laisse un message à Alpha : d’où viennent les coordonnées ?', {'route': 'bbs', 'operation': 'send', 'args': {'recipient': 'Alpha', 'text': 'd’où viennent les coordonnées ?'}}),
    ('Quel fournisseur te fournit la météo ?', {'route': 'weather', 'operation': 'provider', 'args': {}}),
    ('Sur quel LLM es-tu basé ?', {'route': 'info', 'operation': 'provenance', 'args': {'topic': 'assistant'}}),
    ('météo à Quimper', {'route': 'weather', 'operation': 'forecast', 'args': {'location': 'Quimper', 'period': 'today'}}),
    ('quel temps demain à Rennes ?', {'route': 'weather', 'operation': 'forecast', 'args': {'location': 'Rennes', 'period': 'tomorrow'}}),
    ('Laisse un message à Alpha : rendez-vous à midi', {'route': 'bbs', 'operation': 'send', 'args': {'recipient': 'Alpha', 'text': 'rendez-vous à midi'}}),
    ('Laisse un message à Camille', {'route': 'bbs', 'operation': 'send', 'args': {'recipient': 'Camille', 'text': ''}}),
    ('J’ai des messages ?', {'route': 'bbs', 'operation': 'count', 'args': {}}),
    ('Lis mes messages', {'route': 'bbs', 'operation': 'read', 'args': {}}),
    ('merci', {'route': 'llm', 'operation': 'chat', 'args': {}}),
    ('qui utilise le plus Baliz ?', {'route': 'mesh', 'operation': 'stats', 'args': {'topic': 'messages', 'hashes': False}}),
    ('statistique des canaux', {'route': 'mesh', 'operation': 'stats', 'args': {'topic': 'channels', 'hashes': False}}),
    ('quels sont les canaux utilisés en Bretagne ?', {'route': 'wiki', 'operation': 'lookup', 'args': {}}),
    ('condition radio', {'route': 'mesh', 'operation': 'hfcond', 'args': {}}),
    ('tropo', {'route': 'mesh', 'operation': 'hfcond', 'args': {}}),
    ('test', {'route': 'test', 'operation': 'receive', 'args': {}}),
    ('ping', {'route': 'test', 'operation': 'receive', 'args': {}}),
    ('Quelle région par défaut sur un compagnon ?', {'route': 'wiki', 'operation': 'lookup', 'args': {}}),
    ('région', {'route': 'wiki', 'operation': 'lookup', 'args': {}}),
    ('réseau', {'route': 'mesh', 'operation': 'summary', 'args': {}}),
    ('quelle est la tension de batterie du relais Alpha ?', {'route': 'mesh', 'operation': 'unsupported', 'args': {}}),
]

ROUTING_EXAMPLES = ROUTING_DIALOG_EXAMPLES + [
    ('reseau', {'route': 'mesh', 'operation': 'summary', 'args': {}}),
    ('état du réseau', {'route': 'mesh', 'operation': 'summary', 'args': {}}),
    ('résumé du mesh', {'route': 'mesh', 'operation': 'summary', 'args': {}}),
    ('condition radio', {'route': 'mesh', 'operation': 'hfcond', 'args': {}}),
    ('quels sont les canaux utilisés en Bretagne ?', {'route': 'wiki', 'operation': 'lookup', 'args': {}}),
    ('quels canaux rejoindre dans ma région ?', {'route': 'wiki', 'operation': 'lookup', 'args': {}}),

    ('conditions radio HF', {'route': 'mesh', 'operation': 'hfcond', 'args': {}}),
    ('propagation sur les bandes décamétriques', {'route': 'mesh', 'operation': 'hfcond', 'args': {}}),
    ('tropo', {'route': 'mesh', 'operation': 'hfcond', 'args': {}}),

    ("nombre de nœuds actifs", {"route": "mesh", "operation": "count_nodes", "args": {"hours": 24, "country": ""}}),
    ('météo demain à Lyon', {'route': 'weather', 'operation': 'forecast', 'args': {'location': 'Lyon', 'period': 'tomorrow'}}),
    ('quelle est la route ?', {'route': 'path', 'operation': 'message', 'args': {}}),
    ('tu me reçois ?', {'route': 'test', 'operation': 'receive', 'args': {}}),
    ('comment fonctionne le routage ?', {'route': 'wiki', 'operation': 'lookup', 'args': {}}),
    ('qui utilise le plus Baliz ?', {'route': 'mesh', 'operation': 'stats', 'args': {'topic': 'messages', 'hashes': False}}),
    ('statistique des canaux', {'route': 'mesh', 'operation': 'stats', 'args': {'topic': 'channels', 'hashes': False}}),
    ('quels sont les relais les plus sollicités ?', {'route': 'mesh', 'operation': 'relay_connectivity', 'args': {'hours': 24, 'limit': 3}}),
    ('tu en as mis du temps pour répondre', {'route': 'llm', 'operation': 'chat', 'args': {}}),
]


def validate_arguments(args, schema):
    """Validate the same closed schema that is sent to the classifier."""
    properties = schema['properties']
    if set(args) != set(properties):
        raise ValueError('Invalid argument fields')
    types = {'string': str, 'integer': int, 'boolean': bool}
    for key, spec in properties.items():
        value = args[key]
        if type(value) is not types[spec['type']]:
            raise ValueError('Invalid argument type: ' + key)
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError('Invalid argument value: ' + key)
        if isinstance(value, str) and (len(value) > 200 or any(c in value for c in '\r\n\x00')):
            raise ValueError('Invalid argument text: ' + key)
        if 'minimum' in spec and value < spec['minimum'] or 'maximum' in spec and value > spec['maximum']:
            raise ValueError('Argument out of bounds: ' + key)


def grounded_place(location: str, question: str) -> bool:
    """Compare place tokens, allowing spelling separators and St/Ste forms."""
    def tokens(text):
        words = re.findall(r"[^\W_]+", normalize(text), flags=re.UNICODE)
        aliases = {'st': 'saint', 'ste': 'sainte'}
        return [aliases.get(word, word) for word in words]
    place, source = tokens(location), tokens(question)
    return bool(place) and any(source[i:i + len(place)] == place
                               for i in range(len(source) - len(place) + 1))


def parse_plan(text, question, enabled):
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {'route', 'operation', 'args'}:
        raise ValueError('Invalid plan fields')
    route, operation, args = value['route'], value['operation'], value['args']
    if not isinstance(route, str) or route not in enabled or route not in CATALOG:
        raise ValueError('Route not enabled')
    if not isinstance(operation, str) or operation not in CATALOG[route] or not isinstance(args, dict):
        raise ValueError('Unknown operation')
    if route == 'weather' and operation == 'forecast':
        if set(args) != {'location', 'period'} or args['period'] not in ('today', 'tomorrow'):
            raise ValueError('Invalid forecast parameters')
        location = args['location']
        if not isinstance(location, str) or len(location) > 100 or any(c in location for c in '\n\r;|'):
            raise ValueError('Invalid place')
        if location and not grounded_place(location, question):
            raise ValueError('Invented place')
        expected_period = 'tomorrow' if re.search(r'\b(demain|tomorrow)\b', normalize(question)) else 'today'
        if args['period'] != expected_period:
            raise ValueError('Wrong forecast date')
    elif route == 'info':
        from .provenance import argument_schema
        validate_arguments(args, argument_schema())
    elif route == 'bbs':
        from .bbs_service import validate_bbs_arguments
        validate_bbs_arguments(operation, args, question)
    elif route == 'mesh':
        if operation in ACTIVE and infer_operation(question) != operation:
            raise ValueError('Active radio action requires explicit user intent')
        from .network_plan import argument_schema
        validate_arguments(args, argument_schema(operation))
        # Literal targets must come from the request; intent and typed filters
        # belong to the model, not to a second keyword-based interpreter.
        if operation == 'trace' and args.get('path'):
            path = args['path']
            nodes = path.split(',')
            if not all(re.fullmatch('[0-9a-fA-F]{2}|[0-9a-fA-F]{4}', n) for n in nodes) or len({len(n) for n in nodes}) != 1 or len(nodes) > 32:
                raise ValueError('Invalid trace path')
            if re.sub(r'[\s,]', '', path).casefold() not in re.sub(r'[\s,]', '', question).casefold():
                raise ValueError('Invented trace path')
        for name in ('target', 'country'):
            value = args.get(name, '')
            if value and normalize(value) not in normalize(question):
                raise ValueError('Invented network target')
    elif args:
        raise ValueError('Unexpected arguments')
    return Decision(Route(route), question, 'catalog', operation, args)
