# Intégration de Tigro main — 17 novembre 2026

## Références

- Base Baliz : `e8c49b0621486851664ad78d28cd24572d443981`.
- Tigro main : `5c3ef93b18df74c5a32fc7295abc7c2d1eb2b28a`, soit 35 commits
  depuis le dernier ancêtre commun `d3ba8888`.
- Branche de travail `sync/tigro-main-20261117`, destination `baliz`.
- Publication GitHub uniquement ; aucun déploiement, redémarrage, envoi radio
  ou accès à une base de production.

## Résolution des chevauchements

- La météo reprend le cache, le choix de modèle et les nouvelles tentatives de
  Tigro. L’ancien chemin de requête redondant est supprimé. Les résumés destinés
  à l’assistant conservent aussi le lieu effectivement renvoyé par le fournisseur.
- Les imports du viewer réunissent la gestion des canaux et le fuseau Baliz.
- La documentation et les flux publics conservent le filtrage des messages et
  des logs. Les QR de canaux privés restent soumis aux autorisations de publication.
- Infos conserve les liens Bretagne et ajoute MeshQual, Mesh Info FR, Topo
  Meshtools et MeshStats France Live, sans doublonner les liens existants.
- Les tests JavaScript conservent les estimations de duty cycle de Baliz et
  ajoutent les contrôles des QR de contacts. Les références météo suivent les
  nouvelles tentatives de Tigro ; l’inventaire des routes inclut l’API Airtime Baliz.
- Les modules de l’assistant, le traitement des messages, les observations
  d’horloge, les migrations et les pages Airtime et Time sont inchangés.
- Le profil retire uniquement `RepeaterPrefixCollision_Service.include_prefix_free_hint`,
  supprimé par Tigro. Ses autres valeurs restent identiques ; le service demeure
  désactivé. La branche d’identité reçoit cette seule adaptation du profil.

## Validation

Sous Python 3.12 : **1 809 tests ciblés réussis, 21 ignorés et 2 échecs préexistants**.
Les références météo et l’inventaire des routes ont été corrigés puis leurs tests
réexécutés. Les deux échecs de `tests/test_web_viewer_app.py` sont reproduits sur
la base Baliz, avec les mêmes assertions :

- `TestAllowedTables::test_whitelist_contains_expected_tables` attend une liste
  qui omet une table propre à Baliz.
- `test_mesh_entry_points_api_and_page` attend un lien vers la page séparée
  `/mesh-entry-points`.

JavaScript : **55 tests réussis**. HTMLHint : aucune erreur sur les cinq modèles
modifiés. ESLint : aucune erreur, 9 avertissements. Ruff : aucune erreur sur les
12 fichiers Python applicatifs contrôlés. Mypy : aucune erreur sur les six
fichiers contrôlés. Compilation Python et syntaxe de l’installateur vérifiées.
Les deux profils sont lus strictement et comparés ; ils contiennent les mêmes
valeurs. Le validateur du projet ne signale aucune erreur sur les profils.

La suite complète et une vérification visuelle dans un navigateur ne font pas
partie de ce bilan. `main` reste à `d3ba88887b19f123dd9a8905a31857da4472f4a9`.
