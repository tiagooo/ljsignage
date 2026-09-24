"""Authentication (Google OAuth, lugardajoia.com) and roles.

Only authorised accounts get in: admins listed in ADMIN_EMAILS and editors that
an admin authorised in the panel. Roles are re-evaluated on every request, so
removing someone from ADMIN_EMAILS or revoking an editor takes effect at once.
"""

from __future__ import annotations

from functools import wraps

from flask import Flask, abort, request
from flask_login import current_user

from .extensions import db, login_manager, oauth
from .models import User

PUBLIC_ENDPOINTS = {
    "auth.login",
    "auth.login_google",
    "auth.callback",
    "auth.dev_login",
    "api.health",
    "static",
}


def init_app(app: Flask) -> None:
    settings = app.config["LJ_SETTINGS"]
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Inicie sessão para continuar."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "basic"

    if settings.google_client_id and settings.google_client_secret:
        oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
            overwrite=True,
        )

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        return None


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    return user if user is not None and user.is_active else None


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def google_client():
    return oauth.create_client("google")
