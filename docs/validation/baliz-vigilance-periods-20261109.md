# Vigilance : anticipation et erreurs isolées

Base : `d549e807efdfed862727349f39757c203d054694`.

- Lecture indépendante de J0 et J1, avec contrôle de la date de validité.
  État et cache propres à chaque département/date. Libellés et horaires en heure de Paris.
- Une vigilance de demain est diffusée même si aujourd'hui est vert. Un retour
  au vert aujourd'hui ne clôt pas l'alerte du lendemain. Le passage de minuit
  conserve les dates absolues. Aucun horaire n'est inventé en l'absence de chronologie.
- Collecte partielle : les réussites sont conservées malgré l'échec d'un autre
  département/jour. Une page de session défaillante n'empêche pas d'en essayer une autre.
- Le profil Baliz active uniquement `include_tomorrow = true`. Les départements,
  le canal, les contrôles horaires et rappels à 24 h restent inchangés.

1 036 tests passent, dont 22 nouveaux cas sur les deux journées, les fenêtres
horaires, les erreurs partielles, le cache, le passage de minuit et les journées
de 23/25 heures. Ruff et mypy passent sur les cinq fichiers source concernés.

Le paramètre J0/J1 et les champs de temps sont vérifiés contre le JavaScript
du site officiel Météo-France et ses réponses JSON, sans conserver les jetons.
Un essai isolé sur le LXC103 utilise les huit vrais bulletins ; les réponses
et envois radio sont capturés. Les échecs partiels sont simulés.

Le déploiement contrôle les empreintes des fichiers, la reprise du bot, la radio
et le site public. La configuration privée ne change que pour l'option ci-dessus.
Les données BBS, cibles/choix d'horloge et visibilité des canaux sont conservés.
