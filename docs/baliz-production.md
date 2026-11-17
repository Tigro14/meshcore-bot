# Baliz : production et branches

## Organisation

| Branche | Rôle |
| --- | --- |
| `baliz` | Dernière version publiée de Baliz, avec ses fonctions et particularités. Le commit effectivement déployé est indiqué dans le suivi de production ci-dessous. |
| `main` | Branche historique du fork, distincte de la production Baliz. Cette synchronisation ne la modifie pas. |
| `config/baliz-identity` | Profil public et documentation de l'identité de Baliz, actualisés séparément du code. |
| `sync/production-20261105` | Publication des changements déjà déployés le 5 novembre 2026, puis intégration dans `baliz`. |
| `sync/tigro-20261106` | Intégration de `Tigro14/meshcore-bot:main` après fusion de la PR #80. |
| `sync/lxc103-production-20261107` | Publication de la source réellement déployée dans le LXC103, avec ses correctifs et ses réglages publics. |
| `sync/lxc103-production-20261109` | Publication des ajouts BBS et assistant effectivement déployés les 8 et 9 novembre. |
| `sync/lxc103-production-20261115` | Publication de la source du LXC103 : vigilance, Airtime et corrections de l’interface. |
| `feat/*`, `fix/*`, `sync/tigro-*` | Développements et intégrations isolés, à relire avant intégration dans `baliz`. |

Les mises à jour de Tigro sont intégrées dans Baliz en conservant ses particularités. Publier une version de production ne remplace pas `main`, ne réécrit pas l'historique et ne supprime pas les branches de travail existantes.

## Intégration de Tigro main — 17 novembre 2026

La branche `sync/tigro-main-20261117` intègre les 35 commits de Tigro depuis
`d3ba8888` jusqu’à `5c3ef93b18df74c5a32fc7295abc7c2d1eb2b28a` dans `baliz`.

- Cache Open-Meteo avec repli en cas d’échec, sélection des modèles Météo-France
  pour la métropole, nouvelle tentative des requêtes et unités du vent explicites.
- Libellés français du bulletin météo quotidien et géocodage amélioré.
- Page Channels et QR codes d’ajout de canal, édition des canaux surveillés dans
  Settings, QR d’ajout de contact et sessions web conservées après redémarrage.
- Page Real-time publique avec filtrage des canaux, réponses privées et journaux
  réservés aux administrateurs. Nouveaux liens réseau dans Infos.
- Corrections Blitzortung MQTT, alertes de collision de préfixe et installation
  des dépendances de géocodage.

L’assistant Baliz, la vigilance bretonne, Airtime, Clock Sync et les migrations
sont conservés. Les bulletins fournis à l’assistant conservent leur lieu pour
empêcher une reformulation vers une autre ville. Le profil garde ses activations
et réglages ; seule l’option supprimée `include_prefix_free_hint` est retirée.
Cette suppression est répercutée dans `config/baliz-identity`.

Publication GitHub uniquement : le LXC103 reste à
`611cd5cb29235ba77481e61e9d66db79a6d3b562`. `main` n’est pas modifiée.
Voir la [validation de l’intégration](validation/baliz-tigro-main-20261117.md).

## Alignement avec le LXC103 — 17 novembre 2026

La source installée reste `611cd5cb29235ba77481e61e9d66db79a6d3b562`, déjà
publiée dans `baliz` par la PR #25. Les 312 fichiers contrôlés (310 fichiers
applicatifs, `requirements.txt` et `config.ini.example`) sont identiques à
GitHub. Le service et la radio sont opérationnels.

Les deux profils publics reprennent les changements de réglage constatés :
`Channels.monitor_channels = #bretagne` et
`Vigilance_Service.group_similar_alerts = false`. Les réponses aux DM restent
activées ; la vigilance continue de couvrir les quatre départements bretons.
La destination temporaire `#meteo` reste locale ; le profil public conserve
`#bretagne` pour les alertes.

La publication passe par `sync/lxc103-production-20261117` vers `baliz` et
`sync/baliz-identity-production-20261117` vers `config/baliz-identity`.
Elle conserve `main` et ne modifie ni ne redémarre le serveur. Voir la
[validation](validation/baliz-production-sync-20261117.md).

## Mise à jour des alertes — 15 novembre 2026

La source installée est `1b23c8215d8de191184d3da21e0487b57aa7949c`.
Les 310 fichiers applicatifs correspondent à cette source ; aucun fichier
supplémentaire n’a été trouvé sous `modules`. Le service et la radio sont actifs.

Le mode par département réunit les journées dues, conserve les horaires et les
niveaux, et affiche les noms des départements bretons. Les messages respectent
la taille radio disponible sans tronquer les phénomènes. Le regroupement entre
départements reste disponible et activé dans le profil public, comme sur le serveur.

La publication conserve `main` et actualise séparément `config/baliz-identity`.
La destination temporaire `#meteo` reste locale. Voir la
[validation](validation/baliz-vigilance-sync-20261115.md).

## Alignement avec le LXC103 — 15 novembre 2026

La source installée est `3706f23eda89adf032f24eeb98924b41ca5915b5`,
repérée par le tag `baliz-production-2026-11-15`. Les **310 fichiers applicatifs**
ont été comparés au serveur par SHA-256 : aucune différence et aucun fichier
supplémentaire sous `modules`. Le schéma est à **39**.

La publication conserve les 34 commits depuis `7dbb84a9` avec leur historique :

- Vigilance des quatre départements bretons, bulletins datés d’aujourd’hui et
  demain, cache hors ligne, rappels persistants et alertes condensées par journée,
  niveau et phénomène.
- Attribution passive de l’airtime, reconstruction des émissions des répéteurs
  et scénario de retransmissions attendues, avec couverture et limites explicites.
- Histogramme adapté à la période, courbe des paquets/minute, taille moyenne,
  repères de minuit et quatre indicateurs de synthèse.
- Graphiques de batterie dans Time et Contacts, libellés de dates, noms du
  tableau de bord et attribution des réponses aux requêtes concurrentes.

Le profil public ajoute le relevé batterie quotidien à 03:00 et reprend les
réglages publics durables de vigilance. `#meteo` est une destination temporaire
du serveur ; le profil conserve `#bretagne`. L’identité publique est actualisée
séparément dans `config/baliz-identity`.

`main` reste à `d3ba88887b19f123dd9a8905a31857da4472f4a9`. Les ajouts
de publication portent uniquement sur le profil public et la documentation ;
ils ne remplacent pas la configuration active. Aucun secret, prompt privé,
message ou état de cible n’est exporté. Cette synchronisation ne modifie ni
ne redémarre le LXC103.

Voir la [validation de l’alignement](validation/baliz-production-sync-20261115.md).

## Alignement avec le LXC103 — 9 novembre 2026

La source déployée est `933731dcce070658ea44289cff585c8a96139a15`,
repérée par le tag `baliz-production-2026-11-09`. Les 300 fichiers applicatifs
ont été comparés au serveur : aucune différence ni fichier applicatif
supplémentaire. Le schéma reste à 36.

Les six commits de production depuis `42503744` sont conservés avec leur
historique : BBS en langage naturel, saisie littérale et choix du destinataire,
compteur de sauts reçu, fournisseur météo et provenance sur demande. Les
messages BBS restent traités par le service existant. Les autres réponses de
l'assistant ne reçoivent aucun ajout de provenance.

Le profil public active `bbs` et `info` et conserve `test` désactivé en accès
direct, comme sur le serveur. Le délai sémantique de 300 secondes est explicité
dans le profil ; en production, il est hérité du délai LLM. L'identité publique
est actualisée séparément dans `config/baliz-identity`.

Cette publication ne modifie ni ne redémarre le LXC103. Elle conserve `main`
au commit `d3ba88887b19f123dd9a8905a31857da4472f4a9`. Les secrets, prompts
privés, messages, cibles Clock Sync et préférences de canaux ne sont pas copiés.
Voir la [validation de l'alignement](validation/baliz-production-sync-20261109.md).

## Intégration de Tigro main — 7 novembre 2026

La branche `sync/tigro-main-20261107` intègre Tigro `main` au commit
`d3ba88887b19f123dd9a8905a31857da4472f4a9`, puis rejoint `baliz` par PR.
Elle reprend les notifications privées du BBS, la résolution des noms dans ses
statistiques et les pages publiques en lecture seule de Tigro. Les commandes
administratives restent protégées lorsque l’authentification est activée.

La migration des rappels BBS utilise le numéro 36 : les migrations 1 à 35 de Baliz
restent inchangées. Le profil public, l’assistant, les règles d’accès au LLM,
les corrections DM et horloges, la carte et le tutoriel français de Time sont
conservés. Les anciens substituts BBS des messages planifiés sont retirés comme
chez Tigro ; les nouveaux rappels passent en DM.

Cette mise à jour concerne GitHub uniquement. Le LXC103 reste sur la source
`75e06d7256b93fa627314137c84ab4eac8992b19` et n’est ni modifié ni redémarré.
Les branches `main` et `config/baliz-identity` ne changent pas.
Voir la [validation de l’intégration](validation/baliz-tigro-main-20261107.md).

## Alignement avec le LXC103 — 7 novembre 2026

La source de production est `75e06d7256b93fa627314137c84ab4eac8992b19`. Les
empreintes de 296 fichiers applicatifs ont été comparées au serveur : aucune
différence, aucun fichier applicatif supplémentaire. L'intégration conserve ces
fichiers à l'identique et reprend les tests déjà publiés pour le démarrage du
viewer.

Le BBS, la vigilance, les régions de Contacts et la migration BBS 35 font
désormais partie de `baliz`. Les corrections DM, Clock Sync, accès au LLM et le
tutoriel français de Time sont conservés. Aucun nouveau code de Tigro, postérieur
à celui déployé, n'est intégré par cette synchronisation.

Le profil public reprend les valeurs vérifiées du LXC103, notamment les deux
limites de sauts à 10 et le délai du routeur à 300 secondes. L'identité est
actualisée séparément. Le fichier actif privé, les bases de données, les messages
et les préférences de canaux et d'horloges restent sur le serveur.

Cette opération publie la production existante ; elle ne redéploie ni ne
redémarre le LXC103. La branche historique `main` reste inchangée. Voir la
[validation de l'alignement](validation/baliz-production-sync-20261107.md).

## Intégration initialement locale de dev — 6 novembre 2026

La branche locale `deploy/tigro-dev-local-20261106` reprend Tigro `dev` au commit `522409eb23288d2fc4ffae3a409db0be493e7f3c`, avec le BBS, la vigilance et l'indication de région dans Contacts. Elle conserve les particularités de Baliz et utilise la migration 35 pour le BBS, sans réutiliser la migration 34 d'occupation radio.

Cette intégration avait été limitée au LXC103, sans publication sur GitHub.
L'alignement du 7 novembre décrit ci-dessus l'intègre maintenant dans `baliz`.
Voir la [validation locale initiale](validation/baliz-dev-lxc103-20261106.md).

## Synchronisation GitHub du 6 novembre 2026

La branche `baliz` intègre le `main` de Tigro au commit `d040c5ad94c03b29b29ade6001e1084264adc46f` (PR #80). Cette fusion conserve les fonctions propres à Baliz et rapproche les historiques des corrections DM et horloges déjà présentes dans les deux versions.

Les corrections de typage et d'isolation des tests de Tigro, la documentation des DM et horloges et l'exécution des tests JavaScript dans le workflow sont reprises. Les textes français de Baliz, ses symboles de configuration, ses filtres et ses analyses de carte sont conservés. Le LLM, le routage sémantique, les règles d'accès et le profil public ne changent pas.

Les nouveautés BBS, vigilance et indication de région dans Contacts présentes uniquement sur `dev` de Tigro ne font pas partie de cette fusion de `main`. Voir le [compte rendu de validation](validation/baliz-sync-20261106.md).

Cette synchronisation met à jour le dépôt GitHub ; elle ne déploie pas de nouvelle version sur le LXC103.

## Production du 5 novembre 2026

- Base GitHub : `72315457c6f97136a78b8cb29b5bbbcdbdc222bc`, intégration de Tigro du même jour.
- Source déployée sur le LXC103 : `a274f5a1625ab7b2776b110c1e2c425097a52808`.
- Tag de référence de cette source : `baliz-production-2026-11-05`.
- Les 12 commits suivants sont publiés avec leur historique d'origine. Les seuls ajouts de cette synchronisation sont cette documentation et son lien dans le README.
- Les empreintes de 295 fichiers applicatifs suivis (modules, point d'entrée et dépendances), dont les 25 fichiers applicatifs modifiés depuis la base, ont été comparées aux fichiers du LXC103 avant publication : aucune différence ni fichier manquant.
- Les anciennes notes de validation mentionnant une diffusion limitée au LXC décrivent l'autorisation initiale. La publication GitHub de cet ensemble a été autorisée ensuite, le 5 novembre 2026.

### Envois privés et assistant

La préparation d'un DM vérifie une identité unique dans la radio, peut restaurer un contact à partir de données complètes et distingue une absence d'émission d'une réception non confirmée ou confirmée. Le délai minimal d'attente d'un ACK passe à 30 secondes, et la synchronisation d'horloge conserve les réponses reçues pendant cette attente.

Seuls les expéditeurs identifiés comme compagnons peuvent déclencher le LLM. Les types inconnus, répéteurs et autres appareils ne déclenchent pas l'assistant. Le routage sémantique et les règles spécifiques de Baliz sont conservés.

Validation : [résolution et livraison des DM](validation/baliz-dm-resolution-20261105.md), [ACK et compagnons](validation/baliz-ack-companions-20261105.md).

### Horloges

Contacts et Time partagent les mêmes observations datées et les mêmes badges de dérive. La réception d'une commande ne vaut pas confirmation d'une horloge réglée. La dérive mesurée reste visible indépendamment du statut. Le bouton Auto clkreboot enregistre correctement le choix de chaque cible.

Validation : [observations et statut](validation/baliz-clock-evidence-20261105.md), [dérive affichée](validation/baliz-clock-drift-display-20261105.md), [Auto clkreboot](validation/baliz-auto-clkreboot-toggle-20261105.md), [badges communs](validation/baliz-shared-clock-badges-20261105.md).

### Carte et téléphone

La carte indique les dérives par un symbole d'horloge distinct des avertissements de configuration. Les avertissements restent au premier plan et les fiches contenant beaucoup d'informations s'adaptent à l'espace disponible. Sur téléphone, les cibles Time s'affichent en cartes lisibles ; le bloc de configuration reste directement visible, comme demandé.

Validation : [carte et fiches](validation/baliz-mesh-clock-markers-20261105.md), [Time sur téléphone](validation/baliz-time-mobile-20261105.md).

## Vérifications et limites

Les contrôles ciblés Python 3.10/3.13, JavaScript et les vérifications visuelles sont détaillés dans les notes ci-dessus. Aucun message radio de test n'a été envoyé par ces contrôles.

Le dernier bilan complet, réalisé sur la base d'intégration de Tigro, était de 6 754 tests réussis, 44 échecs préexistants, 17 ignorés et 8 exclus, sans nouvel échec par rapport à sa référence. Les corrections de cette publication ont ensuite été vérifiées par des sélections ciblées ; ce bilan complet ne constitue donc pas un résultat de suite complète sur le présent commit. Les tests hérités encore en échec ne sont pas présentés comme réussis.

Le workflow GitHub de tests existant cible `main`, `master` et `dev`. Une PR vers `baliz` ne le déclenche pas automatiquement. Les résultats locaux documentés restent distincts d'un résultat GitHub Actions.

## Configuration et données locales

Le dépôt conserve le profil public `config.ini.baliz`. Le fichier actif de production, les secrets, les bases de données, les messages privés et les préférences d'affichage des canaux restent sur le serveur. Le profil public n'est pas une copie à appliquer sur la configuration active.

Cette publication ne déploie aucun changement supplémentaire dans le LXC103 et ne modifie aucun réglage en production.
