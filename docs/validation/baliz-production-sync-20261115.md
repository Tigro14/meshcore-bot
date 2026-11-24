# Alignement GitHub / LXC103 — 15 novembre 2026

## Source vérifiée

- Source installée : `3706f23eda89adf032f24eeb98924b41ca5915b5`.
- Base publiée sur `baliz` : `7dbb84a9676b57533fe99aa079b10845d016cf16`.
- 34 commits de production conservés avec leur historique.
- 310 fichiers applicatifs comparés par SHA-256 : identiques, sans fichier
  supplémentaire sous `modules`.
- Schéma installé : 39. Service et radio opérationnels lors du contrôle.
- Le tag `baliz-production-2026-11-15` désigne la source installée, avant les
  ajouts de documentation et de profil public de cette publication.

## Périmètre

Vigilance des quatre départements bretons, bulletins J0/J1 et cache hors ligne,
alertes regroupées par journée/niveau/phénomène, attribution des réponses
concurrentes, graphiques de batterie et affichage des noms du tableau de bord.

Airtime conserve l’analyse passive des réceptions et chemins, la reconstruction
partielle des émissions et le scénario de retransmissions attendues. La page
présente les moyennes sur la période choisie, le pic glissant sur cinq minutes,
le duty cycle estimé sur la dernière heure, un histogramme adapté au zoom et
la courbe des paquets/minute. Les trous de capture ne deviennent pas des zéros.

Le profil public active le relevé batterie quotidien à 03:00, heure de Paris,
et les réglages durables de vigilance. Les cibles Clock Sync restent vides.
La destination temporaire `#meteo` reste locale ; le profil conserve `#bretagne`.
Les valeurs des deux profils publics sont identiques, leur documentation reste
adaptée au rôle de chaque branche.

La publication utilise `sync/lxc103-production-20261115` vers `baliz` et
`sync/baliz-identity-production-20261115` vers `config/baliz-identity`.
`main` reste à `d3ba88887b19f123dd9a8905a31857da4472f4a9`.
Aucune branche existante n’est supprimée ni aucun historique réécrit.

## Validation

- 357 tests Python ciblés réussis, couvrant les 14 fichiers de tests modifiés
  depuis la précédente publication.
- 52 tests JavaScript réussis.
- HTMLHint : aucune erreur sur Airtime, Contacts et Time.
- ESLint : aucune erreur ; 14 avertissements sur les fichiers contrôlés.
- Lecture stricte des profils INI, égalité des valeurs entre les deux branches
  et comparaison des réglages publics durables avec le serveur.
- Aucun fichier actif `config.ini`, secret, base de données, message, état
  d’alerte ou liste de cibles exporté.

Le workflow Tests existant ne cible pas `baliz` ni `config/baliz-identity`.
Ces résultats sont des contrôles locaux, pas un résultat GitHub Actions ni
une nouvelle exécution de toute la suite Python.

Cette synchronisation ne modifie ni ne redémarre le LXC103.
