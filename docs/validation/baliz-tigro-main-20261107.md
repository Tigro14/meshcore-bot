# Intégration de Tigro main dans Baliz — 7 novembre 2026

## Références et portée

- Baliz avant intégration : `7a78067439ba54707b92c655e461e87b96d05d5b`.
- Tigro main intégré : `d3ba88887b19f123dd9a8905a31857da4472f4a9` (PR #84 et #85, correction de démarrage #83 déjà présente).
- Branche de travail : `sync/tigro-main-20261107`, destination `baliz`.
- Publication GitHub seulement. Aucun déploiement, redémarrage ou changement de configuration du LXC103. `main` et `config/baliz-identity` restent inchangées.

## Changements repris

- BBS : notifications privées agrégées à l’arrivée, rappel des messages non lus à 24 h et dernier rappel à 73 h ; vérification périodique toutes les 30 minutes, selon les valeurs par défaut de Tigro. Une étape est enregistrée après un envoi réussi ; un échec permet une nouvelle tentative lors du prochain passage.
- BBS : noms actuels des contacts dans les statistiques, sans exposer le contenu des messages.
- Pages Region, Airtime, BBS, Time et Infos accessibles en lecture seule sans connexion. Les opérations administratives restent protégées lorsque l’authentification est activée ; les commandes de modification sont masquées pour les visiteurs.
- Suppression des anciens substituts BBS des messages planifiés, comme chez Tigro.

## Adaptations Baliz

- La nouvelle migration BBS porte le numéro **36**, car Baliz utilise déjà 35 pour la création de `bbs_inbox`. Les fonctions et le registre des migrations 1 à 35 restent identiques à la référence Baliz.
- Test de passage des versions 33, 34 et 35 vers 36, exécuté deux fois pour vérifier l’idempotence. Conservation des statistiques de l’assistant, du consentement de publication des canaux, des messages BBS existants et des préférences Clock Sync. Un message déjà présent dans `bbs_inbox` avant migration conserve son contenu et reçoit des marques de rappel initialement vides.
- Le profil `config.ini.baliz`, les modules de l’assistant, le traitement des messages, les observations d’horloge et la carte ne changent pas.
- Time conserve le tutoriel français, les badges de dérive, les textes de réception et la présentation mobile, avec les nouveaux contrôles de lecture seule de Tigro.

## Vérifications

Sur Python 3.13, sélection couvrant BBS, planification, migrations, compatibilité Baliz, viewer, démarrage, visibilité des canaux, confidentialité des logs, accès à l’assistant et horloges : **1 166 réussis, 2 ignorés, 2 échecs préexistants**.

Les deux échecs ont été reproduits sur la version de référence, avec les mêmes assertions :

- `tests/test_web_viewer_app.py::TestAllowedTables::test_whitelist_contains_expected_tables` attend une égalité de listes incluant `assistant_function_usage`.
- `tests/test_web_viewer_app.py::test_mesh_entry_points_api_and_page` attend encore un lien vers la page séparée `/mesh-entry-points`.

Sur Python 3.10, sélection BBS, planification, migrations, compatibilité, démarrage, visibilité, confidentialité, accès à l’assistant et horloges : **636 réussis**.

JavaScript : **32 tests réussis**, y compris la lecture seule des cibles Time. HTMLHint sur les deux modèles modifiés : aucune erreur. ESLint sur ces modèles : aucune erreur, quatre avertissements. Ruff sur les fichiers Python modifiés : aucune nouvelle anomalie ; les deux avertissements B905 de `tests/test_web_viewer.py` sont déjà présents dans la référence.

Les tests utilisent des bases temporaires et des envois simulés. Aucun message radio n’a été envoyé et aucune base de production n’a été utilisée. La suite Python complète et une vérification visuelle dans un navigateur ne font pas partie de ce bilan.
