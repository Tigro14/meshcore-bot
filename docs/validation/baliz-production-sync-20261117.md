# Alignement GitHub / LXC103 — 17 novembre 2026

- Source installée : `611cd5cb29235ba77481e61e9d66db79a6d3b562`.
- Branche `baliz` avant publication : `87591aab2cfc3c63c9b4429925d1adb2c3ad603e`.
- 312 fichiers comparés par SHA-256 : 310 fichiers applicatifs,
  `requirements.txt` et `config.ini.example`, sans différence avec GitHub.
- Service actif, initialisation terminée et radio connectée.

Seuls le profil public et sa documentation sont modifiés : réponses sur canal
limitées à `#bretagne`, regroupement des alertes entre départements désactivé.
Les réglages effectifs ont été comparés aux options publiques du serveur.
`semantic_timeout_seconds = 300` reste explicite dans le profil ; le serveur
hérite de cette valeur depuis le délai LLM. La liste de régions vide du serveur
et l’absence de cette option dans le profil ne filtrent aucune région.
La destination d’alertes temporaire `#meteo` reste locale.

Les deux profils ont été lus avec un parseur INI strict, validés par le
validateur du projet et comparés entre eux. Aucun code applicatif n’ayant
changé, aucune nouvelle exécution de la suite de tests n’est revendiquée.

La publication conserve `main` à
`d3ba88887b19f123dd9a8905a31857da4472f4a9` et actualise séparément
`config/baliz-identity`. Aucun secret, fichier actif de configuration, message,
état d’alerte ou cible de synchronisation n’est publié. Le serveur n’est ni
modifié ni redémarré.
