# Assistant Baliz : ask, mesh et llm

Cette architecture appartient pour le moment à la branche `baliz` du fork.
Aucune PR vers Tigro n'est nécessaire pour l'utiliser.

## Commandes et responsabilités

| Entrée | Fonction | Réglages |
| --- | --- | --- |
| `ask`, alias configurable `baliz` | Routeur de l'assistant | `Ask_Command` |
| `mesh`, `query`, `sql` | Questions libres sur les données réseau locales | `Mesh_Command` |
| `llm`, `ia`, `ai`, `chat` | Accès direct au moteur conversationnel existant | `Llm_Command` |
| `test`, `path` | Outils RF existants | Sections existantes |

`ask` ne génère plus de SQL. Les anciennes fonctions de `AskCommand` ont été
extraites dans `MeshService`. `LlmCommand` délègue à `LlmService` et ne contient
aucune règle de routage. Les deux services partagent le transport HTTP LLM.
La conversation ne génère ni n'exécute plus de suivi `[[SQL: ...]]` : une requête
sur la base doit passer par `mesh` (directement ou derrière `ask`).

## Routage

Le routeur est déterministe et indépendant des entrées/sorties. Il reconnaît des
formulations françaises et anglaises. Il n'utilise pas de classification LLM.

1. Route explicitement demandée (`ask mesh ...`, `ask wiki ...`, `ask llm ...`,
   `ask test`, `ask path`) ou aide.
2. Réception du message courant vers `test`.
3. Chemin du message courant vers `path`.
4. Salutations, présentation et demandes créatives (blague, histoire, poème,
   chanson) vers `llm`, sans recherche Wiki.
5. Intention documentaire explicite vers le Wiki.
6. Question sur les observations réseau vers `mesh`.
7. Recherche Wiki pour les autres formulations ; en l'absence de correspondance,
   conversation générale.

Une salutation devant une question technique ne change pas sa destination :
`bonjour, combien de répéteurs actifs ?` reste une demande réseau. Une route
explicite conserve toujours la priorité. Si `llm` est désactivé, une demande
conversationnelle est refusée au lieu de contourner ce réglage par le Wiki.

Une demande documentaire explicite sans source donne « Aucune source pertinente ».
Elle ne se transforme pas en requête réseau ou en réponse inventée par le modèle.
Une demande réseau n'interroge pas le Wiki. Les réponses documentaires excluent
contexte live et historique, et ne sont pas ajoutées à l'historique général.
`demain` seul ne déclenche aucun traitement météo. La météo n'est pas une route
spécialisée de cette première version ; le contexte météo existant du LLM reste
configurable. En cas de formulation ambiguë, utiliser une route explicite.

## Configuration et migration

Le [profil de configuration Baliz issu du LXC 103](baliz-config.md) conserve
l'identité et les réglages de production, sans le contournement de scope local.
Il est fourni dans `config.ini.baliz`, avec les adaptations au routeur documentées.

```ini
[Ask_Command]
enabled = true
aliases = baliz
enabled_routes = test,path,mesh,wiki,llm
route_timeout_seconds = 120
max_pages = 4

[Mesh_Command]
enabled = true
public_enabled = true

[Llm_Command]
enabled = true
public_enabled = true
# Conserver ici endpoint, model, timeout_seconds et les options de contexte.
```

- Ajouter `aliases = baliz` dans `Ask_Command`. Retirer cet alias de
  `Llm_Command` s'il y figure, pour éviter deux commandes avec le même déclencheur.
- Reporter les restrictions de canaux de l'ancien `Ask_Command` dans
  `Mesh_Command` si elles concernaient l'interrogation réseau. Définir séparément
  les canaux autorisés pour l'assistant.
- `ask <question réseau>` fonctionne toujours via routage ; `ask tables` et
  `ask help` changent de sens. L'aide réseau est `mesh help`, le schéma `mesh tables`.
- Les alias `query` et `sql` appartiennent désormais à `mesh`.
- L'ancien réglage `Llm_Command.db_query_enabled` n'est plus utilisé. Activer
  `Mesh_Command.enabled` et la route `mesh` à sa place.
- `enabled` active la capacité. `public_enabled = false` masque seulement
  l'accès direct de `llm` ou `mesh`, sans empêcher `ask` de l'utiliser.
- La route `wiki` exige `Llm_Command.enabled` et la configuration RAG existante.
  Les réglages documentaires se trouvent dans `[Rag_Service]`.
  Le RAG reste désactivé par défaut : configurer sa source avant utilisation.
- Les paramètres apparaissent dans l'éditeur des plugins. Les alias utilisent
  l'éditeur de mots-clés déjà existant.

Les autorisations de l'assistant **et** de la capacité cible s'appliquent.
Les restrictions de canaux et les cooldowns ne sont pas contournés. Un outil
indisponible produit une erreur explicite, sans basculer vers un autre outil.

## Organisation du code

| Fichier | Responsabilité |
| --- | --- |
| `modules/commands/ask_command.py` | Alias, réglages, appel du dispatcher et réponse |
| `modules/assistant/router.py` | Décisions pures : route, question, raison |
| `modules/assistant/dispatcher.py` | Autorisations, capacités, délai, erreurs |
| `modules/assistant/mesh_service.py` | SQL en lecture seule sur les six tables réseau autorisées |
| `modules/assistant/llm_service.py` | Moteur existant : contexte, Wiki, génération et historique |
| `modules/assistant/llm_client.py` | Transport HTTP commun |
| `modules/assistant/response.py` | Pagination en octets UTF-8 et envoi final |

L'adaptateur RF utilise exclusivement les commandes `test` et `path` distribuées.
Il copie le message réel, conserve son identité, sa portée régionale et ses
métadonnées RF, puis capture les réponses par le mécanisme existant `capture_sink`.
Il n'utilise pas le message synthétique du planificateur, ne remplace aucune
méthode d'envoi et refuse les plugins de remplacement non audités. L'assistant
émet ensuite la réponse capturée par le transport normal et ses limites RF.

L'interrogation SQL conserve la validation lecture seule, borne les résultats à
20 lignes, applique un contrôle SQLite des tables accessibles et une limite
CPU de deux secondes pour la requête. La protection thermique est appliquée aux
capacités LLM et mesh. Le timeout global coupe l'attente de l'assistant ; une
requête HTTP déjà lancée dans un thread peut finir en arrière-plan jusqu'à son
propre timeout, sans émettre de réponse radio.

## Validation sur le LXC 109

Ne pas démarrer de service radio pour ces vérifications. Utiliser le compte
`baliz-test`, le dépôt `/opt/baliz-test` et son environnement `.venv`.

```sh
.venv/bin/python -m pytest tests/test_assistant_router.py tests/test_assistant_integration.py tests/commands/test_llm_command.py tests/test_wiki_rag.py --no-cov
.venv/bin/python -m pytest tests -m 'not mqtt' --no-cov
.venv/bin/ruff check .
.venv/bin/mypy modules/
```

Le lanceur `scripts/baliz_assistant_smoke.py` teste les véritables commandes avec
une base réseau fictive, un index documentaire connu et, si demandé, Ollama.
Il capture toutes les réponses et n'initialise aucun companion radio.
Les tests RF utilisent des messages de test ; ils ne remplacent pas une recette
ultérieure avec de vrais paquets reçus.

## Résultats du 12 septembre 2026

Sur le LXC 109, code de l'architecture `a22b23f`, lanceur final `32b0587` :

- 96 tests ciblés réussis (1,46 s).
- Suite complète : 4 733 réussis, 21 échecs, 12 ignorés et 7 désélectionnés (163,02 s).
- Les 21 échecs ont été reproduits sur le commit de base `7a6286d`, avec exactement
  les mêmes identifiants : aucun nouvel échec. Ils concernent le Web Viewer
  (`TestApiMeshEdgesEvidence`, `TestMultibyteMeshAggregateCache`).
- Ruff et mypy réussis sur Debian ; MkDocs strict et contrôle des logs réussis localement.
- Six scénarios du lanceur réussis, dont trois utilisant réellement Ollama
  `gemma3:4b` et l'index Wiki du banc. Le scénario mesh retrouve les deux répéteurs
  de sa base fictive. Le scénario path vérifie le cas sans données de chemin.
- Première tentative SQL : timeout à 90 s. Nouvelle tentative réussie avec
  `--timeout 300`, adapté au modèle tournant sur CPU. Le délai est configurable,
  et ne change aucun réglage de production.

Commande du lanceur, en remplaçant l'adresse par celle du serveur du banc :

```sh
.venv/bin/python scripts/baliz_assistant_smoke.py --live --timeout 300 \
  --endpoint http://OLLAMA_HOST:11434/v1/chat/completions --model gemma3:4b \
  --wiki-index /var/lib/baliz-test/wiki/pages.jsonl
```

Les rapports sont conservés dans `/var/lib/baliz-test/reports/assistant-*`.
Le conteneur 103 n'a pas été modifié et aucun service radio n'a été démarré.
Une recette RF réelle et la validation humaine des formulations du modèle
restent nécessaires avant le passage en production.

## Boîte aux lettres en langage naturel

La route optionnelle `bbs` permet de déposer, compter et lire ses messages BBS sans syntaxe de commande. Un contact ambigu ou un texte manquant donne lieu à un échange limité à cinq minutes, lié à l’expéditeur et au canal ou DM. Les contenus lus restent gérés directement par la BBS, sans passage par le LLM. La lecture et le comptage restent réservés aux DM ; les commandes `bbs s`, `bbs list` et `bbs r` restent disponibles.

Voir la [validation locale du 8 novembre](validation/baliz-bbs-assistant-20261108.md).


Pendant la saisie BBS, une fois le destinataire choisi, la réponse entière devient le texte du message, sans passage dans le LLM. Écrire `annule` permet de quitter cette saisie. Voir la [correction de priorité du texte BBS](validation/baliz-bbs-body-fix-20261108.md).

Pour déposer en une seule demande, séparer explicitement le contenu : `Laisse un message à Alpha : rendez-vous à midi.` Sans texte séparé, Baliz demande quoi transmettre après le choix du destinataire. Répondre par un nom ou un numéro ne peut pas servir de contenu. Voir la [validation de cette séparation](validation/baliz-bbs-payload-fix-20261108.md).

Le catalogue expose `path.hops` pour le nombre de sauts du message actuel. Cette réponse passive utilise les métadonnées reçues, sans reformulation. Si le DM ne contient pas ce compteur, Baliz l’indique au lieu de déduire zéro de son ancien libellé « Direct ». Voir la [validation du compteur reçu](validation/baliz-message-hops-20261109.md).

`weather.provider` répond aux questions sur le fournisseur principal des prévisions météo de Baliz. Cette réponse vient du backend WX initialisé, sans appel météo ni génération de faits par le LLM. Les questions de prévision continuent à utiliser `weather.forecast`. Voir la [validation du fournisseur météo](validation/baliz-weather-provider-20261109.md).

## Provenance sur demande

La route optionnelle `info` expose `info.provenance` pour les demandes explicites sur l’origine ou la méthode des données : réseau, positions, chemins, horloge, documents, airtime, BBS et conversation. La source météo reste accessible par `weather.provider`, qui décrit le service WX effectivement initialisé.

Les descriptions proviennent d’un registre fermé et sont envoyées directement, sans génération ni reformulation. Elles ne sont ni ajoutées aux réponses ordinaires, ni injectées dans les appels LLM qui produisent ces réponses. Le routeur connaît seulement la fonction et ses catégories pour reconnaître une demande de provenance. Les restrictions du service concerné restent appliquées.

Cette première version décrit les sources par fonction ; elle ne prétend pas retrouver le document ou l’observation précise d’une réponse antérieure. Sans catégorie indiquée, elle demande laquelle préciser. Voir la [validation du 9 novembre](validation/baliz-provenance-20261109.md).
