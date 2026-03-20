# LicenseMGR

Interface web de gestion des serveurs de licences FlexLM.

---

## Fonctionnalités

- **Dashboard** — vue d'ensemble de tous les vendors (statut, uptime, features, backups)
- **Page de maintenance** par vendor
  - Éditeur de fichier `.lic` inline avec numéros de lignes
  - Upload / remplacement du fichier de licences
  - Sauvegarde manuelle et automatique avant toute modification
  - Historique des sauvegardes avec restauration et téléchargement
  - Contrôle du serveur : `lmstat`, restart (avec polling de disponibilité), `lmdown`
  - Validation du fichier `.lic` : doublons, dates expirées, syntaxe, SERVER/VENDOR manquants
- **Gestion des utilisateurs** avec trois rôles :
  - `admin` — accès complet
  - `operator` — restart, backup, restore, consultation
  - `readonly` — consultation et téléchargement uniquement
- **Uptime** — suivi du dernier démarrage détecté par `lmstat`
- **SSL** — compatible nginx reverse proxy

---

## Prérequis

- Python 3.11+
- pip
- nginx (optionnel, pour SSL)

---

## Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/NotBeCursed/LICENSE-MGR.git
cd LICENSE-MGR
```

### 2. Créer un environnement virtuel

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Installer les dépendances

```bash
pip install flask pyyaml
```

### 4. Configurer les vendors

Éditer `vendors.yaml` :

```yaml
vendor:
  lic_path:       /opt/vendor/vendor.lic
  backup_dir:     /opt/vendor/backups
  lmutil_bin:     /opt/vendor/bin_lm/lmutil
  start_script:   /opt/vendor/start_licenses_vendor.sh
  license_server: 27000@license
```

Ajouter autant de blocs que nécessaire. Redémarrer Flask après modification.

### 5. Lancer l'application

```bash
python app.py
```

L'interface est accessible sur `http://localhost:5000`.

Identifiants par défaut : `admin` / `admin` — **à changer immédiatement** via `/admin/users`.

---

## Structure du projet

```
licmgr/
├── app.py              # Application Flask, routes
├── functions.py        # Logique métier FlexLM
├── auth.py             # Gestion des utilisateurs et rôles (SQLite)
├── vendors.yaml        # Configuration des vendors
├── templates/
│   ├── base.html       # Layout commun
│   ├── login.html      # Page de connexion
│   ├── dashboard.html  # Vue d'ensemble des vendors
│   ├── vendor.html     # Page de maintenance par vendor
│   └── admin_users.html# Gestion des utilisateurs
└── .gitignore
```

Fichiers générés au runtime (exclus du dépôt) :
```
users.db        # Base de données utilisateurs (SQLite)
uptime.json     # Timestamps de démarrage des vendors
```

---

## Configuration avancée

### Variables d'environnement

Toutes les valeurs de `vendors.yaml` peuvent être surchargées par variable d'environnement :

```bash
export VENDOR_LIC_PATH=/chemin/custom/vendor.lic
export VENDOR_LMUTIL_BIN=/chemin/custom/lmutil
export SECRET_KEY=une-cle-secrete-robuste
export AUTH_DB_PATH=/var/lib/licmgr/users.db
export UPTIME_FILE=/var/lib/licmgr/uptime.json
export VENDORS_CONFIG=/etc/licmgr/vendors.yaml
```

### SSL avec nginx

Créer `/etc/nginx/conf.d/licmgr.conf` :

```nginx
server {
    listen 80;
    server_name <hostname>;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name <hostname>;

    ssl_certificate     /etc/ssl/licmgr/server.crt;
    ssl_certificate_key /etc/ssl/licmgr/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Avec SSL, modifier `app.py` pour écouter uniquement en local :

```python
app.run(host="127.0.0.1", port=5000, debug=False)
```

---

## Rôles et permissions

| Action                        | admin | operator | readonly |
|-------------------------------|:-----:|:--------:|:--------:|
| Consultation / lmstat         | ✓     | ✓        | ✓        |
| Téléchargement / validation   | ✓     | ✓        | ✓        |
| Backup / restore / restart    | ✓     | ✓        | ✗        |
| lmdown                        | ✓     | ✓        | ✗        |
| Upload / édition du .lic      | ✓     | ✗        | ✗        |
| Gestion des utilisateurs      | ✓     | ✗        | ✗        |

---

## Sécurité

- Changer le mot de passe `admin` par défaut dès le premier démarrage
- Définir `SECRET_KEY` via variable d'environnement (ne pas laisser la valeur par défaut en production)
- Ne pas exposer le port Flask (5000) directement — utiliser nginx
- Les fichiers `.lic`, certificats SSL et la base `users.db` sont exclus du dépôt git
