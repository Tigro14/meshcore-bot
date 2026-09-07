# MeshCore Bot — Agent Context

## Architecture

- **Bot**: Python 3.13, Flask + Flask-SocketIO 5.x (`async_mode='threading'`), Werkzeug 3.1.8
- **Deploy**: DietPi @ 192.168.1.16, code en `/opt/meshcore-bot`, config en `/etc/meshcore-bot/config.ini`
- **Service**: `meshcore-bot.service` (systemd), `WorkingDirectory=/opt/meshcore-bot`
- **Proxy**: Apache @ 5.39.84.154 (`bot.tigro.fr`), vhost `/etc/apache2/sites-enabled/005-bot.conf`
- **Backend IP (Tailscale)**: `100.102.87.107:8083`
- **SSH bot**: `ssh dietpi@192.168.1.16`

## Web Viewer — Socket.IO

### Transports
- `config.ini` → `[Web_Viewer] websocket_enabled = false` (défaut)
- Quand `false` : serveur `transports=['polling']`, client JS `transports:['polling'], upgrade:false`
- Quand `true` : serveur `transports=['websocket','polling']`, client tente WS d'abord
- Le context processor injecte `websocket_enabled` dans tous les templates

### Apache Proxy (socket.io)
```apache
ProxyPass /socket.io/ http://100.102.87.107:8083/socket.io/
ProxyPassReverse /socket.io/ http://100.102.87.107:8083/socket.io/
```
- Les 3 `SetEnv` (proxy-nokeepalive, proxy-initial-not-pooled, force-proxy-request-1.0) doivent être **commentées** pour que le polling fonctionne
- `ProxyTimeout 60` global est suffisant pour le polling (ping interval 25s)

### CSP
- Géré **uniquement** par Apache (header `Content-Security-Policy`)
- Flask ne set plus de CSP quand `X-Forwarded-For` ou `X-Real-Ip` est présent (déviation proxy)
- Domains autorisés : `cdnjs.cloudflare.com`, `cdn.jsdelivr.net`, `unpkg.com`, `tiles.openfreemap.org`
- Directives : `script-src`, `style-src`, `connect-src`, `font-src`, `img-src`, `worker-src blob:`

### Deadlock Fix (RLock)
- `_clients_lock` est un `threading.RLock()` (reentrant) — `handle_connect` appelle `disconnect()` qui ré-entre dans `handle_disconnect`

## Fichiers Clés

| Fichier | Rôle |
|---------|------|
| `modules/web_viewer/app.py` | Flask app, SocketIO, handlers, CSP, context processor |
| `modules/web_viewer/templates/base.html` | Template base, Socket.IO client init (~ligne 689) |
| `config.ini.example` | Config de référence (`[Web_Viewer]` ~ligne 1737) |
| `/etc/apache2/sites-enabled/005-bot.conf` | Vhost Apache (proxy, CSP, auth) |

## Commands Utiles

```bash
# Deploy sur le bot
ssh dietpi@192.168.1.16
cd /opt/meshcore-bot && git pull
sudo systemctl restart meshcore-bot

# Logs bot
sudo journalctl -u meshcore-bot --since '5 min ago' --no-pager

# Test Socket.IO direct (sur le bot)
curl -s 'http://127.0.0.1:8083/socket.io/?EIO=4&transport=polling'

# Test Socket.IO via proxy
curl -s 'https://bot.tigro.fr/socket.io/?EIO=4&transport=polling'

# Vérifier CSP envoyée
curl -sI 'https://bot.tigro.fr/mesh' | grep -i content-security-policy

# Apache reload
sudo systemctl reload apache2
```

## Lint / Typecheck

```bash
ruff check modules/web_viewer/
ruff format --check modules/web_viewer/
```

## Notes

- `mod_proxy_wstunnel` est chargé mais Apache strippe `Upgrade`/`Connection` headers en HTTP proxy classique — d'où le choix du polling par défaut
- `socat` service (`ws-proxy.service`) existe sur le serveur Apache mais n'est plus nécessaire avec le polling
- Le `LogLevel proxy:trace8` dans le vhost est verbose — remettre à `info` en production
