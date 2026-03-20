# =============================================================================
# Nom         : app.py
# Description : Backend Flask pour la gestion du serveur de licences FlexLM
#               (sauvegarde, mise à jour, arrêt/restart, validation)
# Auteur      : Notbecursed
# Date        : 2026-03-18
# Version     : 1.0.0
# =============================================================================

import os
import shutil
import threading

from flask import (
    Flask, jsonify, redirect, render_template,
    request, session, url_for, flash, send_file
)

from functions import (
    VENDORS,
    get_vendor_config,
    backup,
    list_backups,
    upload,
    lmdown,
    restart,
    lmstat,
    update,
    validate_lic,
    is_server_up,
    get_uptime,
)

from auth import (
    init_db,
    authenticate,
    get_all_users,
    create_user,
    update_user_role,
    update_user_password,
    delete_user,
    login_required,
    admin_required,
    permission_required,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-in-production")

# Initialisation de la base utilisateurs au démarrage
with app.app_context():
    init_db()


# ===== AUTH =====

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = authenticate(username, password)
        if user:
            session["user"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        flash("Identifiants incorrects.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ===== PAGES =====

@app.route("/")
@login_required
def dashboard():
    vendors = []
    for name, cfg in VENDORS.items():
        stat     = lmstat(cfg)
        online   = stat["returncode"] == 0
        bkps     = list_backups(cfg)
        lic_name = cfg["LIC_PATH"].name if cfg["LIC_PATH"].exists() else "—"
        features = 0
        if cfg["LIC_PATH"].exists():
            text = cfg["LIC_PATH"].read_text(errors="replace")
            features = sum(
                1 for line in text.splitlines()
                if line.strip().upper().startswith(("FEATURE", "INCREMENT"))
            )
        vendors.append({
            "name":           name.capitalize(),
            "license_server": cfg["LICENSE_SERVER"],
            "status":         "ok" if online else "error",
            "features":       features,
            "backups":        len(bkps),
            "lic_file":       lic_name,
            "uptime":         get_uptime(name),
        })
    return render_template("dashboard.html", vendors=vendors)


@app.route("/vendor/<vendor>")
@login_required
def vendor_page(vendor):
    try:
        cfg = get_vendor_config(vendor)
    except KeyError:
        flash(f"Vendor inconnu : {vendor}", "error")
        return redirect(url_for("dashboard"))
    bkps   = list_backups(cfg)
    status = "ok" if is_server_up(cfg) else "error"
    return render_template("vendor.html", vendor_name=vendor, config=cfg, backups=bkps, status=status)


# ===== ADMIN — GESTION UTILISATEURS =====

@app.route("/admin/users")
@admin_required
def admin_users():
    users = get_all_users()
    return render_template("admin_users.html", users=users)


@app.route("/admin/api/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Données manquantes"}), 400
    try:
        result = create_user(data.get("username", ""), data.get("password", ""), data.get("role", "readonly"))
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/admin/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    try:
        result = delete_user(user_id, session["user"])
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/admin/api/users/<int:user_id>/role", methods=["PUT"])
@admin_required
def api_update_role(user_id):
    data = request.get_json()
    try:
        result = update_user_role(user_id, data.get("role", ""))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/admin/api/users/<int:user_id>/password", methods=["PUT"])
@admin_required
def api_update_password(user_id):
    data = request.get_json()
    try:
        result = update_user_password(user_id, data.get("password", ""))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ===== HELPERS API =====

def _vendor_or_400(vendor_name: str):
    try:
        return get_vendor_config(vendor_name), None
    except KeyError as e:
        return None, (jsonify({"error": str(e)}), 400)


# ===== API =====

@app.route("/api/<vendor>/uptime", methods=["GET"])
@login_required
@permission_required("lmstat")
def route_uptime(vendor):
    """Retourne l'uptime du vendor et met à jour le tracking si nécessaire."""
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    is_server_up(cfg)   # déclenche record/clear automatiquement
    uptime = get_uptime(vendor)
    return jsonify(uptime if uptime else {"uptime_human": None})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/<vendor>/backup", methods=["POST"])
@login_required
@permission_required("backup")
def route_backup(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    try:
        path = backup(cfg)
        return jsonify({"message": "Sauvegarde effectuée", "backup": path}), 201
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/<vendor>/backups", methods=["GET"])
@login_required
@permission_required("download")
def route_list_backups(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    return jsonify(list_backups(cfg))


@app.route("/api/<vendor>/upload", methods=["POST"])
@login_required
@permission_required("upload")
def route_upload(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "Aucun fichier fourni (champ : 'file')"}), 400
    result = upload(cfg, request.files["file"].read())
    return jsonify({"message": "Fichier mis à jour", **result}), 200


@app.route("/api/<vendor>/lmdown", methods=["POST"])
@login_required
@permission_required("lmdown")
def route_lmdown(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    result = lmdown(cfg)
    return jsonify(result), 200 if result["returncode"] == 0 else 500


@app.route("/api/<vendor>/restart", methods=["POST"])
@login_required
@permission_required("restart")
def route_restart(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    threading.Thread(target=restart, args=(cfg,), daemon=True).start()
    return jsonify({"returncode": 0, "stdout": "Restart lancé en arrière-plan", "stderr": ""}), 200


@app.route("/api/<vendor>/lmstat", methods=["GET"])
@login_required
@permission_required("lmstat")
def route_lmstat(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    result = lmstat(cfg)
    return jsonify(result), 200 if result["returncode"] == 0 else 500


@app.route("/api/<vendor>/lic/read", methods=["GET"])
@login_required
@permission_required("lmstat")
def route_lic_read(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    lic_path = cfg["LIC_PATH"]
    if not lic_path.exists():
        return jsonify({"error": f"Fichier introuvable : {lic_path}"}), 404
    content = lic_path.read_text(errors="replace")
    return jsonify({"content": content, "path": str(lic_path)})


@app.route("/api/<vendor>/lic/save", methods=["POST"])
@login_required
@permission_required("save")
def route_lic_save(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    data = request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "Champ 'content' manquant"}), 400
    try:
        backup_path = backup(cfg) if cfg["LIC_PATH"].exists() else None
        cfg["LIC_PATH"].write_text(data["content"], encoding="utf-8")
        return jsonify({
            "message": "Fichier sauvegardé",
            "path":    str(cfg["LIC_PATH"]),
            "backup":  backup_path,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/<vendor>/lic/validate", methods=["POST"])
@login_required
@permission_required("validate")
def route_lic_validate(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    data = request.get_json(silent=True)
    if data and "content" in data:
        content = data["content"]
    else:
        lic_path = cfg["LIC_PATH"]
        if not lic_path.exists():
            return jsonify({"error": f"Fichier introuvable : {lic_path}"}), 404
        content = lic_path.read_text(errors="replace")
    return jsonify(validate_lic(content))


@app.route("/api/<vendor>/lic/download", methods=["GET"])
@login_required
@permission_required("download")
def route_lic_download(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    lic_path = cfg["LIC_PATH"]
    if not lic_path.exists():
        return jsonify({"error": f"Fichier introuvable : {lic_path}"}), 404
    return send_file(lic_path, as_attachment=True, download_name=lic_path.name)


@app.route("/api/<vendor>/backup/<filename>/restore", methods=["POST"])
@login_required
@permission_required("restore")
def route_backup_restore(vendor, filename):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    backup_path = (cfg["BACKUP_DIR"] / filename).resolve()
    if not str(backup_path).startswith(str(cfg["BACKUP_DIR"].resolve())):
        return jsonify({"error": "Accès refusé"}), 403
    if not backup_path.exists():
        return jsonify({"error": "Fichier de backup introuvable"}), 404
    try:
        current_backup = backup(cfg) if cfg["LIC_PATH"].exists() else None
        shutil.copy2(backup_path, cfg["LIC_PATH"])
        return jsonify({
            "message":  "Fichier restauré",
            "restored": str(cfg["LIC_PATH"]),
            "backup":   current_backup,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/<vendor>/backup/<filename>/download", methods=["GET"])
@login_required
@permission_required("download")
def route_backup_download(vendor, filename):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    backup_path = (cfg["BACKUP_DIR"] / filename).resolve()
    if not str(backup_path).startswith(str(cfg["BACKUP_DIR"].resolve())):
        return jsonify({"error": "Accès refusé"}), 403
    if not backup_path.exists():
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(backup_path, as_attachment=True, download_name=filename)


@app.route("/api/<vendor>/update", methods=["POST"])
@login_required
@permission_required("update")
def route_update(vendor):
    cfg, err = _vendor_or_400(vendor)
    if err:
        return err
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "Aucun fichier fourni (champ : 'file')"}), 400
    try:
        steps = update(cfg, request.files["file"].read())
        return jsonify({"message": "Mise à jour effectuée", "steps": steps}), 200
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
