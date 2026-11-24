# Fournisseur des prévisions météo — 9 novembre 2026

Base locale en production : `8d210628`. Correction destinée au LXC103, sans publication GitHub ni changement de configuration ou de schéma.

## Problème

« Quel fournisseur te fournit la météo ? » a reçu une clarification sur la batterie, la température ou le signal. Il manquait une fonction donnant la source des prévisions de Baliz, distincte d’une prévision météo et d’une mesure de capteur.

## Correction

- Ajouter `weather.provider` au catalogue fermé avec des arguments vides, une description et un exemple. La demande est complète : elle ne nécessite ni lieu, ni période, ni mesure de télémétrie.
- Décrire le backend effectivement initialisé par `WxCommand` : Open-Meteo, NOAA/NWS ou NOAA/NWS avec le complément Open-Meteo configuré. La réponse est construite directement par le service, sans reformulation par le LLM, sans appel à une API météo et sans lecture ou exposition de secret.
- Conserver `weather.forecast`, ses arguments lieu/période et le contrôle du lieu. Garder les contrôles d’accès du service WX, les routes existantes et les réglages de Baliz.
- Enregistrer ce type d’appel sous le libellé « Fournisseur météo » dans les statistiques anonymes de l’assistant.

Le réglage de production vérifié est `weather_provider = openmeteo`. La réponse prévue est : « Mon fournisseur principal pour les prévisions météo est Open-Meteo. »

## Validation

- 811 tests ciblés Python 3.12 réussis, comprenant les deux fournisseurs, le complément Open-Meteo, l’absence de fuite d’URL privée ou d’identifiant, les contrôles d’accès, l’absence d’appel de prévision ou de génération, le contrat d’arguments, les prévisions ordinaires et les corrections récentes de sauts et BBS.
- 42 contrôles complémentaires réussis sur le catalogue météo et ses traductions existantes.
- Vérification avec le LLM réel sur la question signalée, deux variantes, une prévision météo, le nombre de sauts, un message BBS parlant de fournisseur météo et un résumé réseau.
- Ruff et `git diff --check` réussissent.

Les essais utilisent des requêtes fictives et des envois simulés. Aucun message radio de test n’est envoyé. La configuration, les messages existants, les cibles Clock Sync et les préférences de canaux restent inchangés.
