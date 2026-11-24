# BBS en langage naturel — LXC103, 8 novembre 2026

Base : Baliz `425037448ceedc474b51b20463e714a67b1106b7`.
Branche locale : `feat/baliz-bbs-assistant-lxc-20261108`. Cette livraison n’est pas publiée sur GitHub.

## Fonctionnement

Le catalogue fermé de l’assistant expose `bbs.send`, `bbs.count`, `bbs.read`, `bbs.continue` et `bbs.cancel`. Le LLM choisit l’action et extrait des arguments littéraux ; il ne produit ni SQL ni commande à exécuter.

Exemples : « Laisse un message à Alpha : rendez-vous à midi », « J’ai des messages ? », « Lis mes messages ». Un destinataire absent ou ambigu provoque une question. Le nom, son numéro et le texte peuvent être fournis dans les réponses suivantes. Une demande indépendante garde son routage habituel. « Réponds-lui » n’est pas implémenté dans cette étape.

Les choix sont attachés à la clé de l’expéditeur et au contexte DM/canal, pendant cinq minutes. Les noms seuls ne servent pas d’identité de session. Le nombre de sessions est borné et les opérations BBS concurrentes d’un même expéditeur sont sérialisées.

Le service utilise les contacts, quotas, stockage, purge et notifications BBS existants. Les textes sont issus de la demande ; les mentions, espaces internes et ponctuations sont conservés. Le code rétablit une ponctuation terminale omise par le modèle uniquement lorsqu’elle est directement présente à la suite de l’extrait source. Aucun mot n’est ajouté ou réécrit.

La lecture est exécutée directement par la BBS en DM. Les contenus reçus ne sont ni reformulés ni envoyés au LLM. Les marques de lecture restent soumises au résultat des envois réels. Le contexte fourni au routeur contient les candidats et l’étape en cours, sans texte déjà mémorisé ni contenu de boîte.

Les commandes historiques restent disponibles. Les notifications proposent « lis mes messages » et `bbs r` lorsque cette route est activée. Les déclencheurs DM et canaux ainsi que l’exclusion des expéditeurs identifiés comme répéteurs restent inchangés.

## Validation

- Python 3.12 : 400 contrôles ciblés réussis : service BBS, catalogue, contrat d’arguments, commandes BBS historiques, expéditeurs, mentions, réseau, confidentialité des logs et visibilité des canaux.
- Tests d’intégration et de conversation : mêmes 25 échecs que sur la référence, aucun nouveau. Le test historique de comptage des appels assistant échoue également sur la référence (budget simulé par un Mock).
- Ruff : aucun problème dans les fichiers modifiés.
- LLM réel : essais fictifs d’envoi, comptage, lecture, demande incomplète, choix de destinataire, saisie du texte, annulation et demandes indépendantes météo/réseau/documentation. Après correction du contexte, nouvelle vérification réussie du choix « Alpha », de la réponse « 1 » et de la copie intégrale du texte demandé.
- Aucun message de test n’a été émis sur la radio et aucun message réel n’a été ajouté, lu ou marqué comme lu par les essais.

## Installation prévue

Ajouter seulement `bbs` à `[Ask_Command] enabled_routes` dans le fichier actif, sans changer les autres réglages. Aucun changement de schéma de base. Déployer les fichiers applicatifs de cette branche locale, puis vérifier le service, la radio et le site. Le profil GitHub et les branches distantes restent inchangés.
