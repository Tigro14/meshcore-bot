# Surveillance de la vigilance en Bretagne — 9 novembre 2026

Source de production précédente : `933731dc`. Développement local destiné au
LXC103, sans publication GitHub.

## Comportement

- Surveiller `29,22,35,56` toutes les heures et publier dans `#bretagne`.
  Le premier département reste le Finistère pour préserver le défaut de `vigi`.
- Rester silencieux au vert sans épisode précédemment annoncé. Transmettre les
  premières vigilances jaune, orange et rouge et leurs évolutions, notamment
  lorsqu'un phénomène s'aggrave sans changement de la couleur globale.
- Répéter après 24 heures depuis la dernière transmission réussie si rien ne
  change. La simple date d'édition d'un nouveau bulletin ne déclenche pas d'envoi.
- Annoncer le retour au vert une fois après une vigilance précédemment diffusée.
- Conserver l'état des alertes transmises dans `bot_metadata`, par cible et
  département. Aucun changement de schéma. Un redémarrage ne répète pas l'alerte
  et un échec d'envoi ne décale pas le rappel ni ne ferme l'épisode.
- Refuser les données incomplètes et le repli HTML qui pouvait annoncer du vert
  sans bulletin structuré. Une expiration fournie par la source empêche aussi
  de clore une alerte avec un bulletin périmé.

Le nouveau mode est sélectionné par `notify_on_change = true` ; les installations
qui ne le choisissent pas gardent leur politique `alert_throttle`. Les règles
radio et la taille des messages restent gérées par le gestionnaire existant.
Cette étape n'ajoute pas de source de secours ni de diffusion de bulletins
hors ligne. Aucun appel au LLM n'est ajouté.

## Vérifications

- 92 tests vigilance réussis sur la base avant modification.
- 973 tests ciblés réussis après modification, dont 129 concernant la vigilance,
  ses sources et sa politique de changement. Les 37 nouveaux cas couvrent les
  transitions, les phénomènes, les rappels, les échecs, les reprises après
  redémarrage avec SQLite, les données incomplètes et l'expiration.
- Ruff et mypy réussis sur les modules modifiés.
- Vérification de la source réelle pour les quatre départements sans émission
  radio. Les essais synthétiques utilisent des envois capturés et un état isolé.

L'activation modifie uniquement la section `Vigilance_Service` du fichier actif.
Les autres réglages, les cibles et préférences d'horloge, la visibilité des
canaux, les messages BBS et les messages programmés existants sont conservés.
