# Séparer destinataire et contenu BBS — 8 novembre 2026

Base locale déployée : `647b59da`. Correction limitée au LXC103, sans publication GitHub ni changement de configuration ou de schéma.

## Cause vérifiée

Pour « Laisse un message à Kam », le modèle a considéré le mot « message » de la consigne comme le contenu à transmettre. Le choix suivant « 2 » a validé ce contenu erroné. La vérification a été limitée à la longueur et à l’égalité avec ce mot, sans afficher les autres messages de la boîte.

## Correction

- Le modèle choisit l’action et le destinataire. Dans la demande initiale, un contenu est accepté seulement s’il est séparé explicitement après le destinataire : deux-points, retour à la ligne ou guillemets. Le code le reprend directement dans la source. Sans ce bloc, la saisie du texte est obligatoire.
- Une réponse au choix du destinataire ne peut jamais créer ni remplacer un contenu, même si le modèle renseigne abusivement `text`. Le schéma de continuation impose également un texte vide pendant ce choix.
- Le texte initial explicitement fourni reste conservé pendant une sélection ambiguë. Le mode de saisie littérale et le refus d’auto-envoi de la correction précédente restent inchangés.
- Les messages déjà présents ne sont ni supprimés ni modifiés. Les commandes historiques BBS ne changent pas.

## Validation

770 tests ciblés réussis sur Python 3.12, comprenant le parcours signalé : demande sans texte → liste → choix « 2 » → question sur le texte → saisie → dépôt du seul texte saisi. Les tests rejouent l’extraction fautive `text=message`, un choix erronément recopié dans `text`, les séparateurs explicites, les URL et la ponctuation dans un message entre guillemets.

Ruff et `git diff --check` réussissent. Aucun message radio de test n’est envoyé. Configuration, schéma 36, messages existants et préférences sont conservés au déploiement.
