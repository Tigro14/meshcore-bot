# Réponses concurrentes : association question, réponse et paquet

Base : `038e60f8fc601af3efdcdf944d225878b22e0f50`.

## Cause et correction

Le suivi web lisait `_last_response` après la fin de l'exécution et de l'attente
radio. Une autre commande pouvait écraser ce champ entre-temps. Les exemples
de FR29-Keff correspondent à des routes fournisseur/météo effectivement choisies,
mais à des textes affichés sous la mauvaise demande lorsque les envois se chevauchent.

- Capture propre à chaque exécution via `ContextVar`, avant toute attente radio.
- Identifiant unique attribué avant exécution et transmis au suivi des paquets,
  puis réutilisé pour la capture web. Aucun rapprochement tardif par le seul texte
  pour les réponses normales.
- Réponses en plusieurs parties rattachées à la même demande. Sorties d'outils
  en mode rendu exclues de la capture des réponses réelles.
- Le rapprochement de compatibilité pour les anciens plugins vérifie aussi le
  destinataire et le type DM/canal. Aucun réemploi du texte global d'un autre échange.
- La question sur le LLM utilise la provenance explicite et lit le nom du modèle
  configuré dans le service initialisé. Aucun point d'accès ou secret n'est affiché.
  Le routage reste sémantique ; aucune règle de mots-clés n'est ajoutée.

La correction du suivi fonctionne sans LLM. Les délais ACK et les paramètres radio
ne changent pas. Une réception sans ACK reste non confirmée. Les anciennes lignes
déjà enregistrées ne sont pas réécrites.

## Vérification

1 108 tests ciblés passent : captures concurrentes DM/canal, même expéditeur ou
expéditeurs différents, échecs d'envoi, plusieurs parties, exceptions, identifiants
explicites, transmissions identiques vers d'autres destinataires, routage, BBS,
confidentialité, vigilance et suivi des transmissions. Ruff et mypy passent.

Les 25 échecs des anciens tests `test_assistant_integration.py` sont présents à
l'identique sur la base déployée, vérifiée dans une copie isolée ; aucune nouvelle
défaillance dans ce groupe. Ils portent sur l'ancien routage et ses replis.

Essai avec le vrai modèle configuré, sans émission radio : fournisseur météo,
prévision à Combrit, nom du LLM, remerciement, nombre de sauts et nombre de répéteurs
sélectionnent tous la fonction attendue. Aucun message de test n'est envoyé à FR29-Keff.

Le déploiement conserve intégralement la configuration privée, notamment la
surveillance batterie quotidienne à 03:00, les deux journées de vigilance et
les réglages des horloges. Contrôle des fichiers, de la radio et du site public.
