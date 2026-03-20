# =============================================================================
# Nom         : auth.py
# Description : Gestion des utilisateurs et des rôles (SQLite + bcrypt)
# Auteur      : Notbecursed
# Date        : 2026-03-18
# Version     : 1.0.0
# =============================================================================

import sqlite3
import hashlib
import os
from pathlib import Path
from functools import wraps
from flask import session, redirect, url_for, jsonify, request

DB_PATH = Path(os.environ.get("AUTH_DB_PATH", "users.db"))

ROLES = ["admin", "operator", "readonly"]

ROLE_PERMISSIONS = {
    "admin":    {"lmstat", "backup", "restore", "upload", "save", "validate", "download", "restart", "lmdown", "update", "users"},
    "operator": {"lmstat", "backup", "restore", "restart", "lmdown", "download", "validate"},
    "readonly": {"lmstat", "download", "validate"},
}


# ===== DB =====

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crée la table users et insère l'admin par défaut si vide."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT    UNIQUE NOT NULL,
                password TEXT    NOT NULL,
                role     TEXT    NOT NULL DEFAULT 'readonly'
            )
        """)
        conn.commit()
        # Admin par défaut si aucun utilisateur
        row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                ("admin", _hash_password("admin"), "admin")
            )
            conn.commit()
            print("[auth] Compte admin par défaut créé (admin/admin) — changez le mot de passe !")


# ===== PASSWORD =====

def _hash_password(password: str) -> str:
    """Hash SHA-256 simple — remplacer par bcrypt en production."""
    return hashlib.sha256(password.encode()).hexdigest()


def check_password(password: str, hashed: str) -> bool:
    return _hash_password(password) == hashed


# ===== USER CRUD =====

def get_user(username: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_all_users() -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT id, username, role FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def create_user(username: str, password: str, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"Rôle invalide : {role}")
    if not username or not password:
        raise ValueError("Username et password requis")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                (username.strip(), _hash_password(password), role)
            )
            conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"L'utilisateur '{username}' existe déjà")
    return {"message": f"Utilisateur '{username}' créé avec le rôle '{role}'"}


def update_user_role(user_id: int, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"Rôle invalide : {role}")
    with get_db() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        conn.commit()
    return {"message": "Rôle mis à jour"}


def update_user_password(user_id: int, new_password: str) -> dict:
    if not new_password:
        raise ValueError("Mot de passe vide")
    with get_db() as conn:
        conn.execute("UPDATE users SET password = ? WHERE id = ?", (_hash_password(new_password), user_id))
        conn.commit()
    return {"message": "Mot de passe mis à jour"}


def delete_user(user_id: int, current_username: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError("Utilisateur introuvable")
        if row["username"] == current_username:
            raise ValueError("Impossible de supprimer votre propre compte")
        # Empêcher la suppression du dernier admin
        if row["role"] == "admin":
            count = conn.execute("SELECT COUNT(*) as c FROM users WHERE role = 'admin'").fetchone()
            if count["c"] <= 1:
                raise ValueError("Impossible de supprimer le dernier administrateur")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    return {"message": f"Utilisateur supprimé"}


# ===== AUTH =====

def authenticate(username: str, password: str) -> dict | None:
    user = get_user(username)
    if user and check_password(password, user["password"]):
        return user
    return None


def has_permission(role: str, action: str) -> bool:
    return action in ROLE_PERMISSIONS.get(role, set())


# ===== DECORATORS =====

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Non authentifié"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Accès refusé — rôle admin requis"}), 403
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def permission_required(action: str):
    """Décorateur pour les routes API — vérifie la permission selon le rôle."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user" not in session:
                return jsonify({"error": "Non authentifié"}), 401
            role = session.get("role", "readonly")
            if not has_permission(role, action):
                return jsonify({"error": f"Accès refusé — permission '{action}' requise (rôle actuel : {role})"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator
