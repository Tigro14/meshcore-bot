# Clarification des demandes ambiguës — 9 octobre 2026

Modèle réel : qwen3.5:2b-q4_K_M, serveur configuré par le LXC103.
Tests isolés : aucune fonction radio exécutée, aucun message RF envoyé.

## Version retenue

- Le LLM peut demander une précision via llm.clarify.
- Les statistiques sont présentées au modèle par résultat concret ; elles utilisent
  toujours les commandes, permissions et compteurs existants.
- La conversation et la clarification n'injectent ni le trafic global ni le wiki.
- Aucune détection lexicale ajoutée pour choisir une fonction.
- Le modèle, le délai de routage et les réglages de production restent identiques.

## Validation finale

49 tests pytest passent (catalogue, validation, dispatch, conversation, budgets UTF-8).

Sept appels au modèle réel passent sur la version finale :

| Demande | Résultat |
| --- | --- |
| statistiques de la télémétrie ? | llm.clarify |
| Par où mon message est-il arrivé jusqu'à toi ? | path.message |
| Quels canaux sont les plus bavards ? | mesh.stats / channels |
| Ta réponse a été vraiment lente | llm.chat |
| Tu as un bilan des données de télémétrie ? | llm.clarify |
| Sur quels canaux bretons peut-on discuter ? | wiki.lookup |
| Merci beaucoup, ça m’aide ! | llm.chat |

Une série exploratoire de dix demandes a également couvert les nœuds actifs,
les conditions HF, le résumé réseau, les utilisateurs du bot et une mesure de
batterie indisponible. Les premières versions ont échoué sur certaines variantes
et n'ont pas été déployées.

Limite observée : un premier appel a dépassé le délai de 45 secondes ; son nouvel
essai a correctement demandé une clarification. Ce travail ne garantit donc pas
la disponibilité du moteur à chaque appel ni un routage parfait sur toute phrase.
Les questions générées respectaient le budget de 158 octets dans les essais.
