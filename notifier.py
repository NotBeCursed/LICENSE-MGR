import os
import smtplib
import sqlite3
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

DB_PATH = Path(os.environ.get("AUTH_DB_PATH", "users.db"))

_stop_event = threading.Event()
_scheduler_thread = None

_DEFAULTS = {
    "enabled":              "0",
    "smtp_host":            "",
    "smtp_port":            "25",
    "smtp_user":            "",
    "smtp_password":        "",
    "smtp_tls":             "none",
    "smtp_verify_ssl":      "1",
    "smtp_need_auth":       "0",
    "smtp_from":            "",
    "recipients":           "",
    "recipients_cc":        "",
    "recipients_bcc":       "",
    "subject_template":     "",
    "body_template":        "",
    "body_is_html":         "0",
    "threshold_days":       "30",
    "check_time":           "08:00",
    "notify_days":          "0,1,2,3,4,5,6",
    "last_check_time":      "",
    "last_check_result":    "",
}

DEFAULT_SUBJECT_TEMPLATE = "[LicenseMGR] {{expired_count}} expirée(s), {{expiring_count}} expire(nt) bientôt"
DEFAULT_BODY_TEMPLATE = (
    "LicenseMGR — Rapport d'expiration\n"
    "Généré le : {{date}}\n"
    "Seuil configuré : {{threshold}} jour(s)\n\n"
    "Licences nécessitant une attention :\n\n"
    "{{list}}\n\n"
    "Connectez-vous à l'interface pour mettre à jour les fichiers de licence."
)


def _render_template(template: str, tokens: dict) -> str:
    out = template
    for key, val in tokens.items():
        out = out.replace(key, val)
    return out


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

def _parse_licenses(content: str, vendor_name: str, threshold_days: int) -> list:
    """Parse toutes les FEATURE/INCREMENT et statue expired/expiring/active."""
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
            results.append({
                "vendor":    vendor_name,
                "feature":   feat_name,
                "exp_date":  "permanent",
                "days_left": None,
                "status":    "active",
            })
            continue

        for fmt in ("%d-%b-%Y", "%d-%b-%y"):
            try:
                exp_date  = datetime.strptime(exp_date_str, fmt).date()
                days_left = (exp_date - today).days
                if days_left < 0:
                    status = "expired"
                elif days_left <= threshold_days:
                    status = "expiring"
                else:
                    status = "active"
                results.append({
                    "vendor":    vendor_name,
                    "feature":   feat_name,
                    "exp_date":  exp_date_str,
                    "days_left": days_left,
                    "status":    status,
                })
                break
            except ValueError:
                continue

    return results


# ===== EMAIL =====

def _make_ssl_ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _build_smtp(config: dict) -> smtplib.SMTP:
    host      = config.get("smtp_host", "")
    port      = int(config.get("smtp_port", 25) or 25)
    tls       = config.get("smtp_tls") or "none"
    verify    = config.get("smtp_verify_ssl", "1") == "1"
    need_auth = config.get("smtp_need_auth", "0") == "1"
    user      = config.get("smtp_user", "") if need_auth else ""
    pwd       = config.get("smtp_password", "") if need_auth else ""
    ctx    = _make_ssl_ctx(verify)

    if tls == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, context=ctx)
        smtp.ehlo()
    else:
        smtp = smtplib.SMTP(host, port, timeout=10)
        smtp.ehlo()
        if tls == "starttls":
            smtp.starttls(context=ctx)
            smtp.ehlo()

    if user and pwd:
        smtp.login(user, pwd)
    return smtp


def _split_addrs(s: str) -> list:
    return [r.strip() for r in (s or "").split(",") if r.strip()]


def send_email(subject: str, body: str, config: dict) -> None:
    from_addr = config.get("smtp_from") or config.get("smtp_user", "")
    to_addrs  = _split_addrs(config.get("recipients", ""))
    cc_addrs  = _split_addrs(config.get("recipients_cc", ""))
    bcc_addrs = _split_addrs(config.get("recipients_bcc", ""))
    if not (to_addrs or cc_addrs or bcc_addrs):
        raise ValueError("Aucun destinataire configuré")
    if not config.get("smtp_host"):
        raise ValueError("Serveur SMTP non configuré")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if bcc_addrs:
        msg["Bcc"] = ", ".join(bcc_addrs)

    if config.get("body_is_html", "0") == "1":
        msg.set_content("Ce message nécessite un client de messagerie compatible HTML.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    with _build_smtp(config) as smtp:
        smtp.send_message(msg)


# ===== CHECK =====

def check_and_notify() -> str:
    from functions import VENDORS

    config    = get_config()
    threshold = int(config.get("threshold_days", 30) or 30)

    all_items = []
    for vendor_name, cfg in VENDORS.items():
        lic_path = cfg["LIC_PATH"]
        if not lic_path.exists():
            continue
        content = lic_path.read_text(errors="replace")
        all_items.extend(_parse_licenses(content, vendor_name, threshold))

    expiring = [i for i in all_items if i["status"] in ("expired", "expiring")]
    active   = [i for i in all_items if i["status"] == "active"]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if expiring:
        lines         = []
        lines_expire  = []
        lines_bientot = []
        for item in expiring:
            if item["status"] == "expired":
                line = f"  [EXPIREE]  [{item['vendor'].upper()}] {item['feature']} — expirée le {item['exp_date']}"
                lines_expire.append(line)
            else:
                line = f"  [J-{item['days_left']:>3}]    [{item['vendor'].upper()}] {item['feature']} — expire le {item['exp_date']}"
                lines_bientot.append(line)
            lines.append(line)

        lines_active = [
            f"  [ACTIVE]   [{item['vendor'].upper()}] {item['feature']} — {'permanente' if item['exp_date'] == 'permanent' else 'expire le ' + item['exp_date']}"
            for item in active
        ]

        expired_count  = len(lines_expire)
        expiring_count = len(lines_bientot)
        active_count   = len(lines_active)
        sep = "<br>\n" if config.get("body_is_html") == "1" else "\n"

        tokens = {
            "{{date}}":           ts,
            "{{threshold}}":      str(threshold),
            "{{list}}":           sep.join(lines),
            "{{expired_list}}":   sep.join(lines_expire)  or "  (aucune)",
            "{{expiring_list}}":  sep.join(lines_bientot) or "  (aucune)",
            "{{active_list}}":    sep.join(lines_active)  or "  (aucune)",
            "{{expired_count}}":  str(expired_count),
            "{{expiring_count}}": str(expiring_count),
            "{{active_count}}":   str(active_count),
        }
        subject_tmpl = config.get("subject_template") or DEFAULT_SUBJECT_TEMPLATE
        body_tmpl    = config.get("body_template") or DEFAULT_BODY_TEMPLATE
        subject      = _render_template(subject_tmpl, tokens)
        body         = _render_template(body_tmpl, tokens)

        dests = ", ".join(filter(None, [config.get("recipients", ""), config.get("recipients_cc", ""), config.get("recipients_bcc", "")]))
        try:
            send_email(subject=subject, body=body, config=config)
            result_str = f"OK — {len(expiring)} alerte(s) envoyée(s) → {dests}"
        except Exception as e:
            result_str = f"ERREUR envoi email : {e}"
    else:
        result_str = f"OK — aucune licence expirant dans les {threshold} prochains jours"

    set_config({"last_check_time": ts, "last_check_result": result_str})
    return result_str


# ===== SCHEDULER =====

def _next_trigger(check_time: str) -> float:
    """Seconds until next daily occurrence of HH:MM."""
    try:
        h, m = map(int, check_time.split(":"))
    except Exception:
        h, m = 8, 0
    now    = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _parse_notify_days(notify_days: str) -> set:
    """'0,1,2,3,4,5,6' -> {0..6} (0=lundi, cf. datetime.weekday())."""
    try:
        days = {int(d) for d in notify_days.split(",") if d.strip() != ""}
    except ValueError:
        days = set()
    return days & set(range(7)) or set(range(7))


def _scheduler_loop(check_time: str, notify_days: set) -> None:
    while True:
        wait = _next_trigger(check_time)
        if _stop_event.wait(timeout=wait):
            break
        if datetime.now().weekday() not in notify_days:
            continue
        try:
            check_and_notify()
        except Exception:
            pass


def start_scheduler() -> None:
    global _scheduler_thread, _stop_event
    config = get_config()
    if config.get("enabled") != "1":
        return
    check_time  = config.get("check_time") or "08:00"
    notify_days = _parse_notify_days(config.get("notify_days") or "0,1,2,3,4,5,6")
    _stop_event.set()
    _stop_event = threading.Event()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, args=(check_time, notify_days), daemon=True
    )
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _stop_event.set()
