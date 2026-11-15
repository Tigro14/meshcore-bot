# Déclenchement de l’assistant après une ponctuation

La demande « Baliz, quelle météo a coray » a été reçue dans `#bretagne`, mais
la virgule accolée à l’alias empêchait la reconnaissance de la commande.

Le correctif accepte `, : ; ! ?` après les mots-clés et alias de l’assistant.
La question conserve sa casse et sa ponctuation. Les autres commandes, canaux,
autorisations et réglages de vigilance ne sont pas modifiés.

Validation locale :

- Échec reproduit avec le gestionnaire réel de commandes avant correction.
- 206 tests ciblés réussis : déclenchement, préfixes, mentions, accès des types
  de nœuds, BBS et conservation du texte envoyé à l’assistant.
- Ruff sans erreur sur les trois fichiers Python contrôlés.
- Le fichier d’intégration historique `test_assistant_integration.py` conserve
  ses 25 échecs préexistants, identiques avec et sans le correctif. Il ne fait
  pas partie des 206 tests annoncés réussis.

Le contrôle après installation vérifie le déclenchement avec la configuration
active sans envoyer de message radio de test ni appeler le LLM.
