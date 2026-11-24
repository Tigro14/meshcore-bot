# Alignement GitHub / LXC103 — 7 novembre 2026

## Référence vérifiée

- Branche publiée avant synchronisation : `baliz`, commit `e4a5d0ec`.
- Source réellement installée : `75e06d7256b93fa627314137c84ab4eac8992b19`.
- Schéma de production : 35, avec le BBS ajouté après l'occupation radio 34.
- Empreintes comparées : 296 fichiers applicatifs, tous identiques à la source
  de production connue. Aucun fichier applicatif supplémentaire dans `modules`.
- L'intégration conserve ces 296 empreintes ainsi que celle du fichier
  `config.ini.example` installé.

## Publication

L'historique de production est intégré à `baliz`, dont les corrections déjà
publiées et le test du démarrage du viewer sont conservés. Le BBS, la vigilance
et les régions dans Contacts sont ceux du LXC103 ; aucune mise à jour ultérieure
de Tigro n'est ajoutée. La migration BBS conserve son numéro 35.

Le profil public est comparé à une liste explicite de réglages non sensibles.
Sur `baliz`, les seuls écarts à corriger sont la limite de réponse (7 → 10 sauts),
le rayon Clock Sync (5 → 10 sauts) et le délai sémantique (45 → 300 secondes).
Ce dernier est explicité à la valeur héritée du délai LLM en production.

La branche `config/baliz-identity` actualise séparément ce profil et sa
documentation : modèle, délais, routes et nombre de pages de l'assistant,
limites de sauts et Airtime. Le département de vigilance 29 et les alertes
automatiques désactivées sont conservés. Les autres valeurs du profil ne sont
pas modifiées.

Les prompts, secrets, clés de canaux, données, messages et préférences conservés
dans la base du serveur ne sont pas copiés. Aucun fichier de production n'est
modifié ; le service n'est pas redémarré par cette synchronisation.

## Validation

- Python 3.13 : **385 tests ciblés réussis** (BBS, vigilance, migrations,
  visibilité des canaux, confidentialité des logs, contrôle des expéditeurs,
  réponses Clock Sync, observations d'horloge, carte et démarrage du viewer).
- Python 3.13 : **28 contrôles web ciblés réussis** (BBS, régions de Contacts,
  horloges et rendu/CSP).
- Comparaison des profils avant/après : seuls les champs de la liste publique
  explicitement sélectionnée changent ; toutes les autres valeurs sont intactes.
- `git diff --check` réussit.

Ces contrôles ne constituent pas une nouvelle exécution de la suite complète.
Le bilan complet et les échecs préexistants de l'intégration initiale restent
documentés dans [sa validation](baliz-dev-lxc103-20261106.md).
