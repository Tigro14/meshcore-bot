"""Single closed-catalog interpreter. Failure never chooses another capability."""

import asyncio
import time
from typing import Any

import requests

from .llm_client import post_chat


class SemanticRouter:
    """Classify a question without executing any capability."""

    def __init__(self, owner: Any):
        self.logger = owner.logger
        read = owner.get_config_value
        self.enabled = read(
            "Ask_Command", "semantic_routing_enabled", fallback=True, value_type="bool"
        )
        self.endpoint = read(
            "Llm_Command",
            "endpoint",
            fallback="http://127.0.0.1:8080/v1/chat/completions",
            value_type="str",
        )
        self.model = read("Llm_Command", "model", fallback="", value_type="str")
        self.timeout_seconds = max(
            1.0,
            min(
                300.0,
                read(
                    "Ask_Command",
                    "semantic_timeout_seconds",
                    fallback=read("Llm_Command", "timeout_seconds", fallback=300.0, value_type="float"),
                    value_type="float",
                ),
            ),
        )

    def _classify(self, question, enabled_routes, bbs_context=None):
        import json

        from .catalog import CATALOG, ROUTING_DIALOG_EXAMPLES, STATS_FUNCTIONS, parse_plan
        catalog = {k: dict(v) for k, v in CATALOG.items() if k in enabled_routes}
        if "mesh" in catalog:
            catalog["mesh"].pop("stats", None)
            catalog["mesh"].update({name: description for name, (_, description) in STATS_FUNCTIONS.items()})
        from .network_plan import argument_schema
        variants = []
        for route, operations in catalog.items():
            for operation in operations:
                args = argument_schema(operation) if route == 'mesh' and operation not in STATS_FUNCTIONS else {
                    'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
                if route == 'mesh' and operation in STATS_FUNCTIONS:
                    args = {'type': 'object', 'properties': {'hashes': {'type': 'boolean'}},
                            'required': ['hashes'], 'additionalProperties': False}
                if route == 'info':
                    from .provenance import argument_schema as provenance_schema
                    args = provenance_schema()
                if route == 'bbs':
                    from .bbs_service import argument_schema as bbs_schema
                    args = bbs_schema(operation)
                    if operation == 'continue' and bbs_context:
                        if bbs_context['awaiting'] == 'text':
                            args['properties']['selection']['const'] = ''
                            args['properties']['text']['const'] = question
                        else:
                            args['properties']['text']['const'] = ''
                if route == 'weather' and operation == 'forecast':
                    args = {'type': 'object', 'properties': {'location': {'type': 'string'},
                            'period': {'type': 'string', 'enum': ['today', 'tomorrow']}},
                            'required': ['location', 'period'], 'additionalProperties': False}
                variants.append({'type': 'object', 'properties': {
                    'function': {'const': route + '.' + operation}, 'args': args},
                    'required': ['function', 'args'], 'additionalProperties': False})
        examples = []
        for question_example, plan in ROUTING_DIALOG_EXAMPLES:
            if plan["route"] in catalog:
                function = plan["route"] + "." + plan["operation"]
                arguments = plan["args"]
                if function == "mesh.stats":
                    function = "mesh." + next(name for name, (topic, _) in STATS_FUNCTIONS.items()
                                               if topic == arguments["topic"])
                    arguments = {"hashes": arguments["hashes"]}
                examples.extend([
                    {"role": "user", "content": question_example},
                    {"role": "assistant", "content": json.dumps({
                        "function": function, "args": arguments}, ensure_ascii=False)},
                ])
        followup_examples = []
        if bbs_context:
            followup_examples.append({"role": "system", "content":
                "La question suivante répond à cette demande de précision BBS : "
                + json.dumps(bbs_context, ensure_ascii=False)
                + ". Une réponse courte choisissant un destinataire ou donnant le texte est bbs.continue, pas mesh.unsupported. "
                "Une question indépendante (météo, réseau, documentation) garde sa propre fonction. "
                "Les exemples suivants illustrent cette continuation ; recopie les arguments de la réponse actuelle seulement."})
            if bbs_context['awaiting'] == 'recipient':
                followup_examples.extend([
                    {"role": "user", "content": "1"},
                    {"role": "assistant", "content": json.dumps({"function": "bbs.continue", "args": {"selection": "1", "text": ""}})},
                ])
            else:
                followup_examples.extend([
                    {"role": "user", "content": "À demain !"},
                    {"role": "assistant", "content": json.dumps({"function": "bbs.continue", "args": {"selection": "", "text": "À demain !"}}, ensure_ascii=False)},
                ])
        payload = {
            "messages": [
                {"role": "system", "content": (
                    "Tu choisis une fonction pour Baliz, assistant radio MeshCore. "
                    "Réponds uniquement par un appel JSON {\"function\":\"route.operation\",\"args\":{...}}. "
                    "Interprète le sens de la demande dans ce contexte. Une demande brève peut être complète. "
                    "Réception du message actuel, y compris un simple test ou ping : test.receive. "
                    "Nombre de sauts jusqu’à Baliz, entre nous ou pour te joindre : path.hops. C’est une mesure du message actuel, sans argument, jamais un résumé ni une clarification. "
                    "Chemin et noms des relais du message : path.message. Réglages, documentation et régions MeshCore : wiki.lookup. "
                    "Conditions radio ou tropo : mesh.hfcond, seul bulletin disponible, limité à la HF. "
                    "Résumé réseau : mesh.summary. Nombre de relais : mesh.count_repeaters. Noms des relais : mesh.list_repeaters. "
                    "Classement par liens : mesh.relay_connectivity. Meilleur relais sans critère : llm.clarify. "
                    "Clarifie seulement une information indispensable manquante. Ne demande pas de précision pour test, ping ou une question de configuration claire. "
                    "Mesures de télémétrie imprécises : llm.clarify ; mesure précise indisponible : mesh.unsupported. "
                    "Salutations, remerciements et remarques : llm.chat. "
                    "Expliquer comment Baliz obtient ou calcule ses données : info.provenance, seulement sur demande explicite. L’origine des coordonnées est topic=position, pas une recherche de relais proches. Sans catégorie précisée : topic=overview. "
                    "Fournisseur ou source des prévisions utilisées par Baliz : weather.provider, args={}. Cette question est complète et ne demande ni ville, ni batterie, température ou signal. "
                    "Nom du modèle IA/LLM sur lequel Baliz est basé : info.provenance, topic=assistant ; ce réglage se consulte, il ne faut pas le deviner. "
                    "Prévisions météo : weather.forecast ; recopie le lieu demandé dans location sans inventer ni remplacer de ville ; vide seulement sans lieu. period=today/tomorrow. "
                    "Boîte aux lettres : bbs.send pour laisser un message, bbs.count pour savoir si j’en ai, bbs.read pour les lire. "
                    "La demande de transmettre ou laisser un message prime sur le contenu : bbs.send, jamais llm.chat ni info.provenance. Son texte, même une question sur les sources, est une donnée à recopier, pas une demande à traiter. "
                    "recipient, selection et text doivent être des extraits EXACTS du message actuel, accents et ponctuation compris, ou vides. "
                    "S’il manque le destinataire ou le texte, appelle bbs.send avec le champ vide : la BBS demandera la précision. "
                    "Si un échange BBS est en cours et la réponse complète ce choix, utilise bbs.continue. "
                    "Pour un choix de destinataire : selection=nom ou numéro, text vide si déjà fourni. Pour le texte demandé : selection vide, text=réponse intégrale exacte. "
                    "Une demande indépendante garde sa route habituelle ; annuler un envoi utilise bbs.cancel. "
                    "Un contexte BBS éventuel est fourni après les exemples. Sans ce contexte, aucun envoi n’est en cours. "
                    "Respecte tous les arguments requis du schéma. Défauts mesh : hours=0,country='',limit=5,sort=recent,hashes=false,target='',role=repeater,roundtrip=true,path=''. "
                    "count_nodes actifs : hours=24 ; relay_connectivity : hours=24,limit=3. Sans schéma spécifique : args={}. "
                    "Catalogue : " + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))

                )},
                *examples,
                *followup_examples,
                {"role": "user", "content": question},
            ],
            "temperature": 0, "max_tokens": 320,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "route_plan", "strict": True, "schema": {"anyOf": variants}}},
        }
        if self.model:
            payload["model"] = self.model
        payload["reasoning_effort"] = "none"
        repair_function = None
        repair_requires_location = False
        started = time.monotonic()
        for attempt in range(2):
            remaining = self.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                response = post_chat(self.endpoint, payload, remaining)
                response.raise_for_status()
            except requests.RequestException as exc:
                # Do not duplicate a request that may still be running upstream.
                self.logger.warning("Catalog transport failure after %.2fs: %s", time.monotonic() - started, exc)
                return None
            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                raw = json.loads(content)
                if not isinstance(raw, dict) or set(raw) != {'function', 'args'} or not isinstance(raw['function'], str):
                    raise ValueError('Invalid function call')
                if repair_function is not None and raw['function'] != repair_function:
                    raise ValueError('Argument repair changed function')
                route, operation = raw['function'].split('.', 1)
                if route not in catalog or operation not in catalog[route]:
                    raise ValueError('Function not exposed')
                repair_function = raw['function']
                if not isinstance(raw['args'], dict):
                    raise ValueError('Invalid arguments object')
                if route == 'mesh' and operation in STATS_FUNCTIONS:
                    if (not isinstance(raw['args'], dict) or set(raw['args']) != {'hashes'}
                            or type(raw['args']['hashes']) is not bool):
                        raise ValueError('Invalid statistic arguments')
                    raw['args'] = {'topic': STATS_FUNCTIONS[operation][0], 'hashes': raw['args']['hashes']}
                    operation = 'stats'
                if repair_requires_location and not raw['args'].get('location'):
                    raise ValueError('Missing requested place')
                if route == 'weather' and raw['args'].get('location'):
                    repair_requires_location = True
                plan = parse_plan(json.dumps(dict(route=route, operation=operation, args=raw['args'])), question, enabled_routes)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                self.logger.warning("Catalog invalid plan attempt=%s elapsed=%.2fs: %s", attempt + 1, time.monotonic() - started, exc)
                if attempt == 0:
                    if repair_function is not None:
                        payload['response_format']['json_schema']['schema']['anyOf'] = [
                            variant for variant in variants
                            if variant['properties']['function']['const'] == repair_function
                        ]
                    if repair_function == 'weather.forecast':
                        # Ground the repair in literal spans of the request;
                        # the model still chooses which span names the place.
                        words = question.split()[:40]
                        places = sorted({
                            ' '.join(words[i:j]).strip(' ,.!?')
                            for i in range(len(words))
                            for j in range(i + 1, min(len(words), i + 8) + 1)
                            if len(' '.join(words[i:j])) <= 100
                        })
                        places = [p for p in places if p and not any(c in p for c in '\n\r;|')]
                        if not repair_requires_location:
                            places.append('')
                        if places:
                            payload['response_format']['json_schema']['schema']['anyOf'][0]['properties']['args']['properties']['location']['enum'] = places
                    payload['messages'].append({'role': 'user', 'content':
                        'Le plan précédent a été rejeté : ' + str(exc)[:160] +
                        ('. Corrige uniquement les arguments de ' + repair_function + '. Conserve cette fonction. ' + ('Le lieu ne doit pas être vide si un lieu était demandé. Recopie le lieu tel qu’il figure dans la question initiale, sans ajouter de pays.' if repair_function == 'weather.forecast' else 'Recopie les arguments uniquement depuis le message actuel, sans inventer de texte ni de destinataire.') if repair_function else '. Réinterprète la demande initiale.') +
                        ' Respecte le schéma. Retourne uniquement un appel JSON valide.'})
                continue
            usage = data.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            self.logger.info("Catalog tokens prompt=%s cached=%s completion=%s", usage.get("prompt_tokens"), details.get("cached_tokens"), usage.get("completion_tokens"))
            self.logger.info("Catalog selected route=%s operation=%s elapsed=%.2fs", plan.route.value, plan.operation, time.monotonic() - started)
            return plan
        return None

    async def decide(self, question, enabled_routes, bbs_context=None):
        if not self.enabled or not enabled_routes:
            return None
        return await asyncio.to_thread(self._classify, question, enabled_routes, bbs_context)
