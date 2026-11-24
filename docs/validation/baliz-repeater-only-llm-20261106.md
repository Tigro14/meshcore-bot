# Accès au LLM : exclusion des répéteurs — 6 novembre 2026

Le contrôle partagé par `ask`, `llm`, `mesh` et leurs services refuse uniquement
les expéditeurs identifiés comme répéteurs. Compagnons, RoomServers, capteurs et
types inconnus sont acceptés, en DM comme en canal. Une identité ambiguë ou une
recherche impossible n'établit pas que l'expéditeur est un répéteur et ne bloque
donc pas l'assistant.

Le type vient des contacts radio ou de l'annonce enregistrée. Les anciens rôles
déduits du nom, les noms des relais traversés et le texte du message ne sont pas
utilisés pour classer l'expéditeur. La résolution stricte des destinataires DM
reste inchangée. Les autres conditions de déclenchement et les réglages de Baliz
restent identiques.

Validation locale :

- 140 tests ciblés réussis sur Python 3.13 : contrôle des expéditeurs, routage des
  requêtes de canal et DM, noms avec variante emoji, résolution DM, horloges et
  mentions.
- Sur Python 3.10, 125 tests réussis ; le test d'usage restant échoue aussi sur la
  version précédente, pour la même soustraction d'un Mock dans son montage.
- Les 92 tests élargis d'intégration et de routage présentent exactement les
  mêmes 26 échecs avant/après ; aucun nouvel échec.
- Ruff sur le contrôle et ses tests, ainsi que `git diff --check`, réussissent.

Cette publication sur `baliz` reprend uniquement la règle déjà déployée sur le
LXC103, sans intégrer la branche `dev` de Tigro. Les sept fichiers applicatifs
modifiés correspondent exactement à la correction déployée. Les 140 tests
ciblés réussissent aussi sur cette branche de publication.

Aucun réglage modifié, aucun message radio de test ni appel réel au LLM pendant
les vérifications.
