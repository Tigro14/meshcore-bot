# Commande vigi : tous les départements surveillés

Base : `1f73bf11a8bb021fccdf470f5bb2ffc4ed070f04`.

- Sans argument, la commande interroge tous les départements configurés :
  `29,22,35,56` sur Baliz. Un argument explicite reste limité au département demandé.
- Une seule collecte partagée, avec la temporisation Météo-France existante.
  Les niveaux et phénomènes identiques sont regroupés. Tous les risques actifs
  sont indiqués dans la réponse groupée, en priorité rouge/orange/jaune.
- Les réponses historiques restent datées et distinctes des résultats actuels.
  Leur regroupement exige aussi des dates de publication et de validité identiques.
- Aucun changement de réglage ni de délai des alertes automatiques.

1 014 tests passent, dont 12 nouveaux cas consacrés à la commande multi-départements.
Ruff et mypy passent sur les fichiers concernés.

Vérification isolée sur le LXC103 avec la vraie source, réponse capturée sans
diffusion radio. Le contrôle après redémarrage vérifie les fichiers déployés,
le site public et la connexion radio. La configuration privée et les données
BBS/horloge/visibilité sont conservées.
