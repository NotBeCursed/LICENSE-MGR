import os
import smtplib
import sqlite3
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

DB_PATH = Path(os.environ.get("AUTH_DB_PATH", "users.db"))

_stop_event = threading.Event()
_scheduler_thread = None

_DEFAULTS = {
    "enabled":              "0",
    "smtp_host":            "",
    "smtp_port":            "587",
    "smtp_user":            "",
    "smtp_password":        "",
    "smtp_tls":             "starttls",
    "smtp_from":            "",
    "recipients":           "",
    "threshold_days":       "30",
    "check_interval_hours": "24",
    "last_check_time":      "",
    "last_check_result":    "",
}


# ===== DB =====

def init_notif_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        for k, v in _DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO notification_config (key, value) VALUES (?, ?)",
                (k, v),
            )
        conn.commit()


def get_config() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM notification_config").fetchall()
        cfg = dict(_DEFAULTS)
        cfg.update({r["key"]: r["value"] for r in rows})
        return cfg


def set_config(data: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO notification_config (key, value) VALUES (?, ?)",
                (k, str(v)),
            )
        conn.commit()


# ===== EXPIRY PARSING =====

def _parse_expiring(content: str, vendor_name: str, threshold_days: int) -> list:
    today = datetime.now().date()
    results = []

    # Merge continuation lines
    raw_lines = content.splitlines()
    merged = []
    buf = ""
    for raw in raw_lines:
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1].strip() + " "
        else:
            merged.append((buf + stripped.strip()).strip())
            buf = ""
    if buf:
        merged.append(buf.strip())

    for line in merged:
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if not tokens or tokens[0].upper() not in ("FEATURE", "INCREMENT"):
            continue
        if len(tokens) < 5:
            continue

        feat_name    = tokens[1]
        exp_date_str = tokens[4]

        if exp_date_str.lower() == "permanent":
            continue

        for fmt in ("%d-%b-%Y", "%d-%b-%y"):
            try:
                exp_date  = datetime.strptime(exp_date_str, fmt).date()
                days_left = (exp_date - today).days
                if days_left <= threshold_days:
                    results.append({
                        "vendor":    vendor_name,
                        "feature":   feat_name,
                        "exp_date":  exp_date_str,
                        "days_left": days_left,
                        "status":    "expired" if days_left < 0 else "expiring",
                    })
                break
            except ValueError:
                continue

    return results


# ===== EMAIL =====

def _build_smtp(config: dict) -> smtplib.SMTP:
    host = config.get("smtp_host", "")
    port = int(config.get("smtp_port", 587) or 587)
    tls  = config.get("smtp_tls", "starttls")
    user = config.get("smtp_user", "")
    pwd  = config.get("smtp_password", "")

    if tls == "ssl":
        ctx  = ssl.create_default_context()
        smtp = smtplib.SMTP_SSL(host, port, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port, timeout=10)
        if tls == "starttls":
            smtp.starttls(context=ssl.create_default_context())

    if user and pwd:
        smtp.login(user, pwd)
    return smtp


def send_email(subject: str, body: str, config: dict) -> None:
    from_addr  = config.get("smtp_from") or config.get("smtp_user", "")
    recipients = [r.strip() for r in config.get("recipients", "").split(",") if r.strip()]
    if not recipients:
        raise ValueError("Aucun destinataire configuré")
    if not config.get("smtp_host"):
        raise ValueError("Serveur SMTP non configuré")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(recipients)
    msg.set_content(body)

    with _build_smtp(config) as smtp:
        smtp.send_message(msg)


# ===== CHECK =====

def check_and_notify() -> str:
    from functions import VENDORS

    config    = get_config()
    threshold = int(config.get("threshold_days", 30) or 30)

    expiring = []
    for vendor_name, cfg in VENDORS.items():
        lic_path = cfg["LIC_PATH"]
        if not lic_path.exists():
            continue
        content = lic_path.read_text(errors="replace")
        expiring.extend(_parse_expiring(content, vendor_name, threshold))

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if expiring:
        lines = []
        for item in expiring:
            if item["status"] == "expired":
                lines.append(f"  [EXPIREE]  [{item['vendor'].upper()}] {item['feature']} — expirée le {item['exp_date']}")
            else:
                lines.append(f"  [J-{item['days_left']:>3}]    [{item['vendor'].upper()}] {item['feature']} — expire le {item['exp_date']}")

        body = (
            f"LicenseMGR — Rapport d'expiration\n"
            f"Généré le : {ts}\n"
            f"Seuil configuré : {threshold} jour(s)\n\n"
            f"Licences nécessitant une attention :\n\n"
            + "\n".join(lines)
            + "\n\nConnectez-vous à l'interface pour mettre à jour les fichiers de licence."
        )
        expired_count  = sum(1 for i in expiring if i["status"] == "expired")
        expiring_count = sum(1 for i in expiring if i["status"] == "expiring")
        subject_parts  = []
        if expired_count:
            subject_parts.append(f"{expired_count} expirée(s)")
        if expiring_count:
            subject_parts.append(f"{expiring_count} expire bientôt")

        try:
            send_email(
                subject=f"[LicenseMGR] {', '.join(subject_parts)}",
                body=body,
                config=config,
            )
            result_str = f"OK — {len(expiring)} alerte(s) envoyée(s) → {config.get('recipients', '')}"
        except Exception as e:
            result_str = f"ERREUR envoi email : {e}"
    else:
        result_str = f"OK — aucune licence expirant dans les {threshold} prochains jours"

    set_config({"last_check_time": ts, "last_check_result": result_str})
    return result_str


# ===== SCHEDULER =====

def _scheduler_loop(interval_hours: float) -> None:
    try:
        check_and_notify()
    except Exception:
        pass
    while not _stop_event.wait(timeout=interval_hours * 3600):
        try:
            check_and_notify()
        except Exception:
            pass


def start_scheduler() -> None:
    global _scheduler_thread, _stop_event
    config = get_config()
    if config.get("enabled") != "1":
        return
    interval = float(config.get("check_interval_hours", 24) or 24)
    _stop_event.set()
    _stop_event = threading.Event()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, args=(interval,), daemon=True
    )
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _stop_event.set()
