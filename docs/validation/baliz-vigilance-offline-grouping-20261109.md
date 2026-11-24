# Vigilance : cache hors ligne et regroupement

Base : `7a987ea249af4f234182f7d6e81fa2abfd61939e`.

## Comportement

- Conservation du JSON complet, des niveaux, phénomènes, dates de publication,
  validité et récupération dans `bot_metadata`, sans migration de schéma.
- Une indisponibilité est signalée une fois par état et destination, y compris
  après redémarrage. Le passage à des données périmées et le rétablissement sont
  signalés une fois. Un échec radio ne valide pas l'envoi de cette information.
- `vigi` peut répondre sans Internet : niveau et phénomènes du dernier bulletin,
  dates, mention historique et situation actuelle inconnue. Un bulletin ancien
  ne remplace jamais un bulletin plus récent et ne clôt aucune vigilance.
- Regroupement des seules alertes dues, à niveaux et phénomènes identiques.
  Priorité au rouge, puis orange et jaune. Les délais restent individuels.
- Les deux options sont actives dans le profil Baliz, désactivées dans l'exemple
  générique. Contrôle horaire, rappel après 24 h, départements `29,22,35,56`,
  canal `#bretagne` et comportement du LLM conservés.

## Vérification

1 002 tests passent sur la vigilance, les commandes, le routage, les sauts,
la BBS, les mentions, les expéditeurs, les sources et la confidentialité web.
Ruff et mypy passent sur les fichiers concernés.

Les tests simulent les coupures complètes et partielles, données périmées,
réponses plus anciennes, redémarrages, échecs radio, reprises, différences de
phénomènes/niveaux et rappels mêlés à de nouvelles alertes. Les envois sont
capturés ; aucune alerte météo fictive n'est diffusée sur le réseau.

Une répétition en environnement isolé sur le LXC103 utilise la vraie source
Météo-France et une base séparée, avec les envois radio capturés. Le déploiement
vérifie les fichiers, l'état de la radio et les données BBS/horloge/visibilité.
La configuration privée, notamment les messages programmés, reste inchangée
en dehors des deux nouvelles options de vigilance.
