# Provenance sur demande — 9 novembre 2026

Base locale en production : `301e0000`. Ajout destiné au LXC103, sans publication GitHub ni changement de schéma.

## Comportement

- Ajouter `info.provenance` au catalogue fermé pour les demandes explicites d’origine ou de méthode. Ne pas interpréter une demande de source comme une mesure de télémétrie ou un résumé réseau.
- Utiliser des descriptions courtes issues du fonctionnement du code : paquets radio, annonces des nœuds, horodatages reçus, index documentaire, estimation locale de l’airtime et état des messages BBS. La source météo reste fournie par le backend WX initialisé.
- Ne joindre aucune source aux réponses ordinaires. Ne pas transmettre les descriptions du registre aux appels LLM de conversation, de documentation ou de reformulation d’outils. Aucun appel supplémentaire pour ces routes.
- Répondre directement aux demandes de provenance, sans second appel au LLM ni lecture de messages, de logs ou de secrets. Conserver les contrôles d’accès du service concerné. Signaler un index documentaire inactif.
- Donner une vue par fonction, sans prétendre disposer de références précises ou de l’historique de provenance de chaque réponse. Demander la catégorie si elle n’est pas indiquée.
- En production, ajouter uniquement `info` à la liste des routes autorisées ; conserver tous les autres réglages.

## Validation

- 844 tests ciblés Python 3.12 réussis : contrat fermé, limites de taille, permissions, absence de génération pour les sources, réponses et appels LLM ordinaires inchangés, météo, sauts, BBS, déclencheurs, confidentialité et canaux.
- Essais avec le modèle réel sur les sources générales, réseau, positions, horloges et airtime, puis sur les recherches ordinaires : résumé, comptage, proximité, documentation, météo, sauts et conversation.
- Un cas de dépôt BBS parlant de sources a nécessité de préciser la priorité de la demande de transmission sur son contenu. Le contrôle final réussit sur cinq demandes : deux dépôts BBS avec leur texte exact, une origine de coordonnées, une source générale et une recherche de relais proches.
- Ruff et contrôle des différences sans erreur.

Les vérifications du modèle utilisent des questions fictives, sans émission radio ni manipulation des boîtes aux lettres existantes.
