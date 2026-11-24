# Texte BBS et sélection de son propre nœud — 8 novembre 2026

Base locale déployée : `9d8964c0`. Correction limitée au LXC103, sans publication GitHub ni changement de configuration ou de schéma.

## Cause observée

Les journaux de 09:18–09:20 montrent `bbs.send`, puis `bbs.continue` pour le choix du contact, puis `test.receive` pour « Salut c’est un test ! ». L’assistant reclassait le texte attendu comme une demande indépendante. Le refus d’auto-envoi intervenait seulement après la saisie du texte.

## Correction

- Refuser son propre nœud dès la sélection, avant de demander un texte. Le choix d’un autre contact reste possible dans la même liste.
- Lorsqu’un destinataire est choisi et que Baliz demande le texte, traiter la réponse comme une donnée littérale. Ce texte ne passe plus dans le routeur LLM.
- Réserver ce message à l’assistant aux deux étapes de sélection et d’exécution des commandes. Bloquer également les raccourcis directs `advert` et RandomLine pour ce seul contexte.
- Conserver les vérifications d’accès, les limites, l’expiration, la séparation par expéditeur et par canal et les déclencheurs habituels des canaux. Les priorités ordinaires des commandes restent identiques en dehors de cette saisie.
- Une réponse différée reste liée à la session qui l’a demandée : une expiration ou un nouvel envoi empêche sa réinterprétation comme commande ou son dépôt pour un autre contact.
- Le texte de la question précise que la réponse est transmise telle quelle. Les commandes explicites `annule`, `annule l’envoi` et `bbs cancel` quittent cette saisie. Une demande indépendante reste possible pendant le choix du destinataire ; après la question de saisie, il faut annuler pour changer de sujet.

## Validation

756 contrôles ciblés sur Python 3.12 : parcours BBS, envoi à soi-même, isolation, pipeline de sélection et d’exécution, mentions, gestionnaire de messages, commandes BBS historiques, contrôle des expéditeurs, confidentialité et visibilité des canaux. Le cas signalé et les textes `test`, `advert`, `help`, `météo Brest ?`, `Baliz bonjour`, `bbs r` et des mentions sont testés avec des envois simulés ; le LLM est configuré pour faire échouer le test s’il est appelé pour un corps de message.

Ruff et `git diff --check` réussissent. Aucun message radio de test n’est envoyé. Configuration, schéma 36, boîtes BBS et préférences conservés à l’installation.
