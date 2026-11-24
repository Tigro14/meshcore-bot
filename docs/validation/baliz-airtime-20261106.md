# Activation Airtime — 6 novembre 2026

## Modification

Le profil public `config.ini.baliz` et la configuration active du LXC103
contiennent désormais `[Channel_Occupancy] enabled = true`. Les onze autres
paramètres de cette section reprennent les valeurs de l'exemple de Tigro ;
`use_device_modulation = true` donne priorité à la modulation de la radio.
L'exemple générique reste désactivé par défaut.

La publication part de `baliz` au commit `1f5556b8`. Elle n'intègre aucun code de
`dev`. Le code du LXC103 reste au commit local `41609163`.

## Vérifications

- Les configurations avant/après ont exactement les mêmes sections et valeurs,
  à l'exception de l'ajout de `Channel_Occupancy`.
- Aucune section ni clé obligatoire ne manque. Les autres sections facultatives
  absentes (BBS, vigilance, batterie, contacts, IO Health, multibyte) conservent
  leurs valeurs par défaut ; elles n'ont pas été ajoutées ou activées.
- Le fichier d'exemple installé, devenu ancien, est aligné sur celui du code
  déployé. La configuration privée n'est pas publiée.
- Les 296 empreintes de fichiers applicatifs sont inchangées. Les préférences
  des canaux publiés et des cibles Clock Sync sont identiques avant/après.
- Après redémarrage : service actif, radio connectée, neuf canaux chargés,
  aucune nouvelle erreur de démarrage hors diagnostic préexistant.
- Les huit pages et six API du contrôle de déploiement répondent normalement.
  `/airtime` répond 200 et `/api/telemetry/occupancy` indique `enabled = true`.
- Un premier bucket contenant deux paquets reçus naturellement est enregistré ;
  le relevé en direct est récent (`stale = false`). Aucun message de test envoyé.
- Le profil public est comparé à sa version précédente et au schéma de Tigro :
  seule la nouvelle section change et les types de ses valeurs sont valides.
  `git diff --check` réussit.

Il s'agit d'une activation de configuration, sans modification du code métier ni
reconstitution d'un historique Airtime antérieur.
