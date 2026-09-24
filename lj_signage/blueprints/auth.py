"""Login (Google OAuth, only authorised accounts), logout and user administration."""

from __future__ import annotations

from urllib.parse import urlsplit

from authlib.integrations.base_client import OAuthError
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import EmailField
from wtforms.validators import DataRequired, Length, Regexp

from .. import audit
from ..auth import admin_required, google_client
from ..extensions import db
from ..models import User
from ..timeutil import utcnow

bp = Blueprint("auth", __name__)


class AuthorizeForm(FlaskForm):
    email = EmailField(
        "Email",
        validators=[DataRequired(), Length(max=254), Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")],
    )


def _settings():
    return current_app.config["LJ_SETTINGS"]


def _safe_next(target: str | None) -> str | None:
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _dev_login_enabled() -> bool:
    return _settings().dev_login and current_app.debug


@bp.get("/entrar")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.args.get("next"):
        session["login_next"] = _safe_next(request.args.get("next"))
    settings = _settings()
    dev_users = []
    if _dev_login_enabled():
        authorised = db.session.execute(
            db.select(User.email).where(User.authorized.is_(True))
        ).scalars()
        dev_users = sorted(set(settings.admin_emails) | set(authorised))
    return render_template(
        "auth/login.html",
        google_ready=google_client() is not None,
        dev_login=_dev_login_enabled(),
        dev_users=dev_users,
        domain=settings.allowed_domain,
    )


@bp.get("/entrar/google")
def login_google():
    client = google_client()
    if client is None:
        abort(404)
    return client.authorize_redirect(
        url_for("auth.callback", _external=True),
        hd=_settings().allowed_domain,
        prompt="select_account",
    )


@bp.get("/auth/callback")
def callback():
    client = google_client()
    if client is None:
        abort(404)
    try:
        token = client.authorize_access_token()
        info = token.get("userinfo") or client.userinfo(token=token)
    except OAuthError:
        flash("Não foi possível iniciar sessão com a Google. Tente de novo.", "error")
        return redirect(url_for("auth.login"))
    return _complete_login(dict(info))


@bp.post("/entrar/dev")
def dev_login():
    if not _dev_login_enabled():
        abort(404)
    email = (request.form.get("email") or "").strip().lower()
    name = email.split("@", 1)[0].replace(".", " ").title() or None
    return _complete_login(
        {"email": email, "email_verified": True, "hd": _settings().allowed_domain, "name": name}
    )


def _complete_login(info: dict):
    settings = _settings()
    domain = settings.allowed_domain
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or "").strip()[:200] or None
    in_domain = (
        bool(info.get("email_verified"))
        and email.endswith("@" + domain)
        and info.get("hd") == domain
    )
    if not in_domain:
        audit.record(
            "login_denied",
            f"Conta fora do domínio {domain}.",
            actor=audit.Actor(email or None, name),
            result="denied",
        )
        db.session.commit()
        flash(f"Só são aceites contas @{domain}.", "error")
        return redirect(url_for("auth.login"))

    user = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
    if email not in settings.admin_emails and not (user and user.authorized):
        audit.record(
            "login_denied",
            "Conta sem acesso ao painel.",
            actor=audit.Actor(email, name),
            result="denied",
        )
        db.session.commit()
        flash(
            "A sua conta ainda não tem acesso ao painel. Peça acesso a um administrador.", "error"
        )
        return redirect(url_for("auth.login"))

    if user is None:
        user = User(email=email)
        db.session.add(user)
    user.name = name or user.name
    user.last_login_at = utcnow()
    audit.record("login", "", actor=audit.Actor(email, user.name))
    db.session.commit()
    login_user(user)
    session.permanent = True
    return redirect(session.pop("login_next", None) or url_for("dashboard.index"))


@bp.post("/sair")
def logout():
    if current_user.is_authenticated:
        audit.record("logout", "")
        db.session.commit()
    logout_user()
    flash("Sessão terminada.", "info")
    return redirect(url_for("auth.login"))


# --- user administration (admins) -------------------------------------------------------


@bp.get("/utilizadores")
@admin_required
def users():
    settings = _settings()
    rows = db.session.execute(db.select(User).order_by(User.email)).scalars().all()
    by_email = {u.email: u for u in rows}
    admins = [(email, by_email.get(email)) for email in sorted(settings.admin_emails)]
    editors = [u for u in rows if u.email not in settings.admin_emails]
    return render_template(
        "auth/users.html",
        admins=admins,
        editors=editors,
        form=AuthorizeForm(),
        domain=settings.allowed_domain,
    )


@bp.post("/utilizadores")
@admin_required
def authorize():
    settings = _settings()
    form = AuthorizeForm()
    if not form.validate_on_submit():
        flash("Indique um email válido.", "error")
        return redirect(url_for("auth.users"))
    email = form.email.data.strip().lower()
    if not email.endswith("@" + settings.allowed_domain):
        flash(f"Só é possível autorizar contas @{settings.allowed_domain}.", "error")
        return redirect(url_for("auth.users"))
    if email in settings.admin_emails:
        flash(f"{email} já é administrador (definido no .env).", "info")
        return redirect(url_for("auth.users"))
    user = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
    if user is None:
        user = User(email=email)
        db.session.add(user)
    user.authorized = True
    user.authorized_by = current_user.email
    user.authorized_at = utcnow()
    audit.record("user_authorized", f"{email} pode entrar como editor.")
    db.session.commit()
    flash(f"{email} já pode entrar no painel como editor.", "success")
    return redirect(url_for("auth.users"))


@bp.post("/utilizadores/<int:user_id>/remover")
@admin_required
def revoke(user_id: int):
    user = db.session.get(User, user_id) or abort(404)
    if user.email in _settings().admin_emails:
        flash("Os administradores são definidos no .env (ADMIN_EMAILS).", "error")
        return redirect(url_for("auth.users"))
    user.authorized = False
    audit.record("user_revoked", f"{user.email} deixou de ter acesso.")
    db.session.commit()
    flash(f"{user.email} deixou de ter acesso ao painel.", "success")
    return redirect(url_for("auth.users"))
