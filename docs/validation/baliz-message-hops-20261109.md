# Nombre de sauts du message reçu — 9 novembre 2026

Base locale en production : `cf90e30b`. Correction destinée au LXC103, sans publication GitHub ni changement de configuration ou de schéma.

## Problème

La question « Combien de sauts jusqu’à toi ? » a reçu un résumé du réseau. Les journaux du créneau montrent un choix `llm.clarify` : la question ne disposait pas d’une fonction nommant précisément le compteur de sauts.

## Correction

- Ajouter `path.hops` au catalogue fermé, avec une description et des exemples distinguant nombre de sauts, noms des relais et résumé du réseau. Le choix de fonction reste sémantique ; aucune règle sur les mots de la question ne remplace le LLM.
- Construire la réponse numérique directement depuis les métadonnées reçues, sans seconde rédaction par le LLM et sans lancer de trace ou de recherche radio.
- Conserver le champ brut `path_len` de la trame reçue, séparément de l’ancien affichage des sauts. Décoder les classes de 1, 2 et 3 octets comme le décodeur du bot.
- Traiter `255` comme une absence de compteur entrant. Ne pas transformer en zéro l’absence de trajet complet d’un DM routé, ni utiliser le chemin de retour enregistré d’un contact. Un chemin FLOOD corrélé peut fournir le compteur lorsque le champ reçu est absent.
- Conserver les contrôles d’accès du service Path, l’accès au LLM, la saisie BBS et les autres réglages de Baliz.

## Référence de protocole

Le firmware compagnon écrit `0xFF` pour les messages reçus hors FLOOD : [MyMesh::queueMessage](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp#L400). Le routage DIRECT retire les relais de son chemin au fil du trajet : [Mesh::removeSelfFromPath](https://github.com/meshcore-dev/MeshCore/blob/main/src/Mesh.cpp#L312). Un chemin DIRECT vide à l’arrivée ne prouve donc pas un trajet sans relais.

## Validation

- 800 tests ciblés Python 3.12 réussis : compteur, contrat du catalogue, contrôle d’accès, ingestion des DM, mentions, gestion et exécution des commandes, BBS, confidentialité et visibilité des canaux.
- 37 contrôles complémentaires réussis : modèle de message, corrélation des chemins entrants et décodage multi-octets existant.
- Vérification avec le LLM réel sur des demandes fictives : trois formulations du nombre de sauts, une demande de noms des relais, nombre de répéteurs, résumé réseau et demande BBS.
- Ruff et `git diff --check` réussissent.

Aucun message de test n’est envoyé par radio. Aucun changement de configuration, de schéma, de préférences ni de message stocké n’est requis.
