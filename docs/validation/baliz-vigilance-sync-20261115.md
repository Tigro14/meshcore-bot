# Alignement des alertes de vigilance — 15 novembre 2026

- Source installée : `1b23c8215d8de191184d3da21e0487b57aa7949c`.
- Base de publication : `baliz` à `8c95af1943b9130368a6fbfb9320b258b4907348`.
- 310 fichiers applicatifs comparés par SHA-256 avec le LXC103 : identiques.
- Aucun fichier supplémentaire sous `modules` ; service et radio opérationnels.
- Résumés datés par département, noms bretons et numéros, horaires et niveaux
  conservés, découpage selon la taille radio disponible.
- Regroupement activé dans les deux profils publics, comme sur le serveur.
- Destination temporaire `#meteo` conservée sur le serveur ; profil public `#bretagne`.
- 208 tests Python ciblés réussis ; Ruff et mypy sans erreur sur les fichiers contrôlés.
- Aucun fichier de configuration privé, secret, message, état d’alerte ou cible
  Clock Sync exporté. Aucune modification ni aucun redémarrage du serveur.

Le code est intégré dans `baliz` ; le profil et sa documentation sont alignés
séparément dans `config/baliz-identity`. `main` est conservée à
`d3ba88887b19f123dd9a8905a31857da4472f4a9`.
