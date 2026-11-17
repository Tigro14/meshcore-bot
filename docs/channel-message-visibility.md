# Visibilité des messages de canal sur le web

Dans **Radio → Channel Management**, la case **Live Channel Messages** autorise
la publication des messages déchiffrés d'un canal dans la page **Real-time**.
Elle est désactivée par défaut, y compris à la création d'un canal.

Un canal masqué reste configuré et surveillé par Baliz. Ses messages restent
disponibles localement pour le traitement et les statistiques ; le filtrage porte
sur la diffusion web et l'historique rejoué aux navigateurs.

La visibilité est conservée dans la base et associée au nom et à la clé du canal,
indépendamment de son emplacement radio. Un changement de clé ou une ambiguïté
entre des canaux ne réutilise pas automatiquement une autorisation. L'ancien
historique sans identité de canal vérifiable n'est pas republié.

Les messages et commandes d'un canal masqué sont filtrés avant émission Socket.IO,
y compris lors du replay. Les données déchiffrées ajoutées aux paquets côté serveur
sont retirées du flux des paquets. Le décodeur de la page n'est configuré qu'avec
les noms des canaux explicitement visibles ; les clés privées ne sont jamais
retournées par les listes publiques.

## Administration et API

- `GET /api/channels` et `/api/channels/live` : liste des canaux visibles, sans clés.
- `GET /radio/api/channels` : gestion complète des canaux.
- `PUT /radio/api/channels/live-visibility` : booléen `live_messages_visible`, avec
  `channel_idx` et `channel_name` pour identifier le canal.
- `POST /radio/api/channels` : création avec `live_messages_visible=false` par défaut.
  L'ancienne API de création ne permet pas d'activer la publication.

Les routes de gestion sont protégées par l'authentification du Web Viewer lorsqu'elle
est configurée. En cas d'authentification au niveau du reverse proxy, protéger aussi
le préfixe `/radio/`, comme sur l'installation Baliz. La protection des pages et
API d'administration reste indispensable pour empêcher une modification non autorisée
et protéger les clés et journaux locaux.

La migration 33 ajoute une table indépendante : la synchronisation des canaux depuis
la radio n'efface pas les préférences d'affichage.


La page publique **Channels** (`/channels`) propose aussi des QR codes d’ajout.
Les canaux hashtag utilisent une clé dérivée de leur nom public. Le QR d’un canal
personnalisé ne peut être présenté aux visiteurs que lorsque sa publication est
autorisée ; le canal `Public` n’est pas proposé. Voir [web-viewer.md](web-viewer.md).

La page Real-time est accessible sans connexion, mais les messages et commandes
des canaux masqués restent filtrés avant diffusion et replay. Les réponses privées
restent réservées aux administrateurs, tout comme le flux des logs Socket.IO.

## Logs : métadonnées, pas archive de conversations

La réception, le traitement, les envois de canal et le LLM ne recopient plus le
texte des échanges dans leurs messages de log courants. Les métadonnées techniques
(type, routage, sauts, SNR/RSSI et taille lorsqu'ils sont disponibles) restent
exploitables. Le traitement utilise toujours le message original : les fonctions
du bot et les statistiques ne dépendent pas du texte affiché dans les logs.

La diffusion des logs web, en direct comme en historique, masque de façon ciblée
les champs de texte des messages, les prompts/réponses LLM et les anciennes lignes
qui recopiaient ces contenus. Les autres diagnostics restent lisibles : démarrage,
connexions, erreurs, annonces, statistiques, routage, paquets et données radio
brutes/chiffrées. Les champs de texte sont également masqués dans les structures
imbriquées des logs de débogage. Les continuations sans en-tête sont ignorées pour
ne pas republier la suite d'un message sur plusieurs lignes.

Ce filtre n'est pas une liste fermée d'événements autorisés : les nouveaux
diagnostics techniques ne sont pas remplacés par une ligne générique. Tout nouveau
composant qui journalise le texte d'un échange doit utiliser les métadonnées du
message ou déclarer un champ de contenu reconnu par le filtre.

La case d'un canal autorise la publication dans Live Channel Messages ; elle
n'active pas une seconde copie des conversations dans les logs. Les anciens fichiers
sur disque et les messages conservés localement ne sont ni supprimés ni réécrits.
Leurs droits d'accès et leur durée de conservation restent importants. Les durées
de rétention existantes ne sont pas modifiées par cette correction.
