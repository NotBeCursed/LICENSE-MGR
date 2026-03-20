# =============================================================================
# Nom         : functions.py
# Description : Fonctions pour la gestion du serveur de licences FlexLM
#               (sauvegarde, mise à jour, arrêt/restart, validation)
# Auteur      : Notbecursed
# Date        : 2026-03-18
# Version     : 1.0.0
# =============================================================================

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Fichier de persistance des timestamps de démarrage
UPTIME_FILE = Path(os.environ.get("UPTIME_FILE", "uptime.json"))


# ===== UPTIME TRACKING =====

def _load_uptime() -> dict:
    if UPTIME_FILE.exists():
        try:
            return json.loads(UPTIME_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_uptime(data: dict) -> None:
    UPTIME_FILE.write_text(json.dumps(data, indent=2))


def record_startup(vendor_name: str) -> None:
    """Enregistre le timestamp de démarrage détecté pour un vendor."""
    data = _load_uptime()
    data[vendor_name] = datetime.now(timezone.utc).isoformat()
    _save_uptime(data)


def clear_startup(vendor_name: str) -> None:
    """Efface le timestamp de démarrage (serveur DOWN)."""
    data = _load_uptime()
    data.pop(vendor_name, None)
    _save_uptime(data)


def get_uptime(vendor_name: str) -> dict:
    """
    Retourne l'uptime d'un vendor sous forme de dict :
      { "since": "2026-03-18T10:00:00+00:00", "uptime_seconds": 3600, "uptime_human": "1h 00m" }
    ou None si aucun timestamp enregistré.
    """
    data = _load_uptime()
    since_str = data.get(vendor_name)
    if not since_str:
        return None
    try:
        since = datetime.fromisoformat(since_str)
        now   = datetime.now(timezone.utc)
        delta = int((now - since).total_seconds())
        days  = delta // 86400
        hours = (delta % 86400) // 3600
        mins  = (delta % 3600) // 60
        if days > 0:
            human = f"{days}j {hours:02d}h {mins:02d}m"
        else:
            human = f"{hours}h {mins:02d}m"
        return {
            "since":          since_str,
            "uptime_seconds": delta,
            "uptime_human":   human,
        }
    except Exception:
        return None


# ===== CONFIGURATION =====

import yaml

VENDORS_CONFIG_FILE = Path(os.environ.get("VENDORS_CONFIG", Path(__file__).parent / "vendors.yaml"))


def _load_vendors() -> dict:
    """
    Charge la configuration des vendors depuis vendors.yaml.
    Les valeurs peuvent etre surchargees par variables d environnement.
    """
    if not VENDORS_CONFIG_FILE.exists():
        raise FileNotFoundError(f"Fichier de configuration vendors introuvable : {VENDORS_CONFIG_FILE}")

    with open(VENDORS_CONFIG_FILE, "r") as f:
        raw = yaml.safe_load(f)

    vendors = {}
    for name, cfg in raw.items():
        key = name.upper()
        vendors[name.lower()] = {
            "LIC_PATH":       Path(os.environ.get(f"{key}_LIC_PATH",       cfg["lic_path"])),
            "BACKUP_DIR":     Path(os.environ.get(f"{key}_BACKUP_DIR",     cfg["backup_dir"])),
            "LMUTIL_BIN":          os.environ.get(f"{key}_LMUTIL_BIN",     cfg["lmutil_bin"]),
            "START_SCRIPT":        os.environ.get(f"{key}_START_SCRIPT",   cfg["start_script"]),
            "LICENSE_SERVER":      os.environ.get(f"{key}_LICENSE_SERVER", cfg["license_server"]),
        }
    return vendors


VENDORS = _load_vendors()


# ===== HELPERS =====

def _timestamp() -> str:
    """Retourne un timestamp au format YYYYMMDD_HHMMSS."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_backup_dir(vendor: dict) -> None:
    """Crée le répertoire de backups s'il n'existe pas."""
    backup_dir = vendor["BACKUP_DIR"]
    if not backup_dir.exists():
        print(f"Répertoire {backup_dir} introuvable. Création...")
        backup_dir.mkdir(parents=True, exist_ok=True)


def _run(cmd: list) -> dict:
    """Exécute une commande shell et retourne returncode, stdout, stderr."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return {
            "returncode": result.returncode,
            "stdout":     result.stdout.strip(),
            "stderr":     result.stderr.strip(),
        }
    except (FileNotFoundError, PermissionError, OSError) as e:
        return {
            "returncode": -1,
            "stdout":     "",
            "stderr":     str(e),
        }


# ===== VENDOR =====

def get_vendor_config(vendor: str) -> dict:
    """
    Retourne la configuration du vendor demandé.
    Lève une KeyError si le vendor est inconnu.
    """
    key = vendor.lower()
    if key not in VENDORS:
        raise KeyError(f"Vendor inconnu : '{vendor}'. Vendors disponibles : {list(VENDORS.keys())}")
    return VENDORS[key]


# ===== FONCTIONS MÉTIER =====

def backup(vendor: dict) -> str:
    """
    Sauvegarde le fichier vendor.lic dans le répertoire de backups.
    Retourne le chemin du fichier de sauvegarde créé.
    """
    lic_path = vendor["LIC_PATH"]
    if not lic_path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {lic_path}")

    _ensure_backup_dir(vendor)

    backup_name = f"{lic_path.stem}_{_timestamp()}{lic_path.suffix}"
    backup_path = vendor["BACKUP_DIR"] / backup_name
    shutil.copy2(lic_path, backup_path)
    print(f"Sauvegarde : {backup_path}")
    return str(backup_path)


def list_backups(vendor: dict) -> list:
    """
    Liste les sauvegardes disponibles pour un vendor, ordre antéchronologique.
    Retourne une liste de dicts {name, path, size_bytes, created_at}.
    La date est extraite du nom de fichier (format: name_YYYYMMDD_HHMMSS.lic).
    Fallback sur st_mtime si le nom ne contient pas de timestamp.
    """
    backup_dir = vendor["BACKUP_DIR"]
    if not backup_dir.exists():
        return []

    def _parse_date(f: Path) -> datetime:
        try:
            # Extrait les deux derniers segments du stem : YYYYMMDD_HHMMSS
            parts = f.stem.rsplit("_", 2)
            if len(parts) == 3:
                return datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
        except ValueError:
            pass
        return datetime.fromtimestamp(f.stat().st_mtime)

    files = sorted(backup_dir.glob("*.lic"), key=lambda f: _parse_date(f), reverse=True)
    return [
        {
            "name":       f.name,
            "path":       str(f),
            "size_bytes": f.stat().st_size,
            "created_at": _parse_date(f).isoformat(),
        }
        for f in files
    ]


def upload(vendor: dict, file_content: bytes, auto_backup: bool = True) -> dict:
    """
    Remplace le vendor.lic par le contenu fourni.
    Effectue une sauvegarde automatique si auto_backup=True.
    Retourne un dict avec les chemins concernés.
    """
    lic_path = vendor["LIC_PATH"]
    lic_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if auto_backup and lic_path.exists():
        backup_path = backup(vendor)

    lic_path.write_bytes(file_content)
    print(f"Fichier mis à jour : {lic_path}")
    return {
        "vendor_lic": str(lic_path),
        "backup":     backup_path,
    }


def lmdown(vendor: dict) -> dict:
    """Arrête le serveur de licences du vendor via lmutil lmdown."""
    return _run([vendor["LMUTIL_BIN"], "lmdown", "-c", vendor["LICENSE_SERVER"], "-q"])


def restart(vendor: dict) -> dict:
    """Relance le serveur de licences du vendor via son script de démarrage."""
    return _run([vendor["START_SCRIPT"]])


def lmstat(vendor: dict) -> dict:
    """Retourne le statut des licences du vendor via lmutil lmstat -a."""
    result = _run([vendor["LMUTIL_BIN"], "lmstat", "-a", "-c", str(vendor["LIC_PATH"])])
    result["output"] = result.pop("stdout")
    return result


def is_server_up(vendor: dict) -> bool:
    """
    Retourne True si le serveur est UP.
    Met à jour automatiquement le timestamp d'uptime.
    """
    result = lmstat(vendor)
    output = result.get("output", "").lower()
    up     = "license server up" in output or "up v" in output
    name   = next((k for k, v in VENDORS.items() if v is vendor), None)
    if name:
        data = _load_uptime()
        if up and name not in data:
            record_startup(name)
        elif not up and name in data:
            clear_startup(name)
    return up


def validate_lic(content: str) -> dict:
    """
    Analyse le contenu d'un fichier .lic FlexLM et retourne un rapport de validation.
    Vérifie : doublons, dates expirées, lignes SERVER/VENDOR manquantes, syntaxe de base.
    """
    errors   = []
    warnings = []
    infos    = []
    today    = datetime.now().date()

    VALID_KEYWORDS = {
        "SERVER", "VENDOR", "DAEMON", "FEATURE", "INCREMENT", "UPGRADE",
        "PACKAGE", "USE_SERVER", "INCLUDE", "EXCLUDE", "INCLUDEALL",
        "EXCLUDEALL", "GROUP", "HOST_GROUP", "INTERNET", "MAX",
        "LINGER", "TIMEOUT", "SUPERSEDE", "ISSUED", "BORROW",
    }

    has_server   = False
    has_vendor   = False
    server_lines: list[int] = []
    vendor_lines: list[int] = []
    feature_keys: dict[str, list[int]]       = {}
    feature_phys: dict[str, list[list[int]]] = {}

    raw_lines = content.splitlines()

    # Fusion des lignes de continuation
    merged: list[tuple[int, str, list[int]]] = []
    buf        = ""
    buf_lineno = 1
    buf_phys: list[int] = []
    for lineno, raw in enumerate(raw_lines, start=1):
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            if not buf:
                buf_lineno = lineno
            buf += stripped[:-1].strip() + " "
            buf_phys.append(lineno)
        else:
            if buf:
                buf_phys.append(lineno)
                merged.append((buf_lineno, (buf + stripped.strip()).strip(), buf_phys))
                buf = ""
                buf_phys = []
            else:
                merged.append((lineno, stripped.strip(), [lineno]))
    if buf:
        merged.append((buf_lineno, buf.strip(), buf_phys))

    for lineno, line, phys_lines in merged:
        if not line or line.startswith("#"):
            continue

        tokens = line.split()
        if not tokens:
            continue

        keyword = tokens[0].upper()

        if keyword not in VALID_KEYWORDS:
            errors.append({
                "line":    lineno,
                "message": f"Mot-clé inconnu ou ligne malformée : '{tokens[0]}'"
            })
            continue

        if keyword == "SERVER":
            has_server = True
            server_lines.append(lineno)
            if len(tokens) < 3:
                errors.append({
                    "line":    lineno,
                    "message": "Ligne SERVER incomplète (format : SERVER <host> <hostid> [port])"
                })

        elif keyword in ("VENDOR", "DAEMON"):
            has_vendor = True
            vendor_lines.append(lineno)

        elif keyword in ("FEATURE", "INCREMENT"):
            if len(tokens) < 7:
                errors.append({
                    "line":    lineno,
                    "message": f"Ligne {keyword} incomplète ({len(tokens)} champs, minimum 7 attendus)"
                })
                continue

            feat_name    = tokens[1]
            feat_version = tokens[3]
            exp_date_str = tokens[4]

            key = f"{feat_name.upper()}@{feat_version}"
            feature_keys.setdefault(key, []).append(lineno)
            feature_phys.setdefault(key, []).append(phys_lines)

            if exp_date_str.lower() == "permanent":
                infos.append({
                    "line":    lineno,
                    "message": f"{feat_name} : licence permanente"
                })
            else:
                for fmt in ("%d-%b-%Y", "%d-%b-%y"):
                    try:
                        exp_date = datetime.strptime(exp_date_str, fmt).date()
                        if exp_date < today:
                            errors.append({
                                "line":    lineno,
                                "message": f"{feat_name} : licence expirée le {exp_date_str}"
                            })
                        elif (exp_date - today).days <= 30:
                            warnings.append({
                                "line":    lineno,
                                "message": f"{feat_name} : expire dans {(exp_date - today).days} jour(s) ({exp_date_str})"
                            })
                        break
                    except ValueError:
                        continue
                else:
                    warnings.append({
                        "line":    lineno,
                        "message": f"{feat_name} : date d'expiration non reconnue ('{exp_date_str}')"
                    })

    duplicate_lines_to_remove: list[int] = []

    for key, line_numbers in feature_keys.items():
        if len(line_numbers) > 1:
            name, version = key.split("@")
            errors.append({
                "line":    line_numbers[0],
                "message": f"Feature '{name}' v{version} dupliquée (lignes {', '.join(str(l) for l in line_numbers)})"
            })
            for phys in feature_phys[key][1:]:
                duplicate_lines_to_remove.extend(phys)

    if len(server_lines) > 1:
        errors.append({
            "line":    server_lines[0],
            "message": f"Ligne SERVER dupliquée (lignes {', '.join(str(l) for l in server_lines)})"
        })
        duplicate_lines_to_remove.extend(server_lines[1:])

    if len(vendor_lines) > 1:
        errors.append({
            "line":    vendor_lines[0],
            "message": f"Ligne VENDOR dupliquée (lignes {', '.join(str(l) for l in vendor_lines)})"
        })
        duplicate_lines_to_remove.extend(vendor_lines[1:])

    if not has_server:
        errors.append({"line": 0, "message": "Ligne SERVER absente du fichier"})
    if not has_vendor:
        errors.append({"line": 0, "message": "Ligne VENDOR / DAEMON absente du fichier"})

    if not raw_lines or all(l.strip() == "" or l.strip().startswith("#") for l in raw_lines):
        errors.append({"line": 0, "message": "Le fichier est vide ou ne contient que des commentaires"})

    return {
        "valid":                     len(errors) == 0,
        "errors":                    errors,
        "warnings":                  warnings,
        "infos":                     infos,
        "duplicate_lines_to_remove": sorted(set(duplicate_lines_to_remove)),
    }


def update(vendor: dict, file_content: bytes) -> dict:
    """
    Workflow complet :
      1. Sauvegarde automatique
      2. Remplacement du vendor.lic
      3. lmdown
      4. restart
      5. lmstat (validation)
    """
    steps = {}
    steps["upload"]  = upload(vendor, file_content, auto_backup=True)
    steps["lmdown"]  = lmdown(vendor)
    steps["restart"] = restart(vendor)
    if steps["restart"]["returncode"] != 0:
        raise RuntimeError(f"Échec du restart : {steps['restart']['stderr']}")
    steps["lmstat"] = lmstat(vendor)
    return steps


# ===== MAIN (tests locaux) =====

if __name__ == "__main__":
    cfg = get_vendor_config("synopsys")
    print(backup(cfg))
    print(list_backups(cfg))
    print(lmstat(cfg))
    print(is_server_up(cfg))