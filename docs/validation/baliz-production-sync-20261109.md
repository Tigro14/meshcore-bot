# Alignement GitHub / LXC103 — 9 novembre 2026

## Référence de production

- Source installée : `933731dcce070658ea44289cff585c8a96139a15`.
- Base publiée sur `baliz` : `425037448ceedc474b51b20463e714a67b1106b7`.
- 300 fichiers applicatifs comparés par SHA-256 : identiques, sans fichier
  Python, JavaScript, CSS ou HTML supplémentaire sous `modules`.
- Schéma : 36. Service, radio et site vérifiés opérationnels après le déploiement.

## Publication

Les six commits déjà déployés sont publiés sans réécriture :

- `9d8964c0` : BBS en langage naturel via les fonctions existantes.
- `647b59da` : texte BBS littéral et refus de l'envoi à son propre nœud.
- `cf90e30b` : séparation du choix de destinataire et du contenu.
- `8d210628` : nombre de sauts à partir des métadonnées reçues.
- `301e0000` : fournisseur météo issu du service initialisé.
- `933731dc` : provenance des données uniquement sur demande explicite.

Les seuls ajouts à cette source pendant la synchronisation sont la documentation
et le profil public. Le tag `baliz-production-2026-11-09` désigne la source
installée, et non le commit de publication de ces documents.

Les routes `bbs` et `info` sont activées dans les deux profils publics.
Le profil de la branche `baliz` corrige aussi l'état de la commande directe
`test` : désactivée en production. La branche `config/baliz-identity` reprend
les réglages publics déjà vérifiés dans `baliz` : Open-Meteo, lieu et libellé
Bretagne, scope de démarrage des canaux, effort de raisonnement et accès direct
à `path`. Le délai effectif du routeur reste à 300 secondes, hérité du délai LLM
sur le serveur et explicité dans les profils.

`main` reste au commit `d3ba88887b19f123dd9a8905a31857da4472f4a9`. Aucune
branche existante n'est supprimée. Aucun secret, prompt privé, message ou état
de base de données n'est publié. Le LXC103 n'est ni modifié ni redémarré.

## Validation

- 844 tests ciblés réussis sur la source installée, couvrant les ajouts de
  l'assistant, la BBS, les sauts, la météo, les déclencheurs et la confidentialité.
- Essais du routeur avec le modèle réel, dont la séparation entre une demande
  de source et un message BBS contenant cette question. Les résultats sont
  détaillés dans les notes de validation des six commits.
- Contrôle des empreintes de production et validation des valeurs du profil
  public avec la configuration effective du serveur.
- Les autres valeurs des profils sont conservées ; le profil ne remplace pas
  la configuration privée de l'installation.

Cette synchronisation ne constitue pas une nouvelle exécution de la suite
complète. Les échecs hérités déjà documentés ne sont pas présentés comme résolus.
