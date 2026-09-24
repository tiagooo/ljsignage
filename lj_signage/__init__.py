"""lj-signage: gestão central dos vídeos das TVs de frente de loja do Lugar da Jóia."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from tempfile import SpooledTemporaryFile

from flask import Flask, Request, current_app
from sqlalchemy import event
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Settings
from .extensions import csrf, db, login_manager, migrate, oauth

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class _Request(Request):
    """Uploads are spooled to DATA_DIR/tmp, not to the container's /tmp."""

    def _get_file_stream(
        self, total_content_length, content_type, filename=None, content_length=None
    ):
        return SpooledTemporaryFile(
            max_size=1024 * 1024, mode="rb+", dir=current_app.config["LJ_SETTINGS"].tmp_dir
        )


def create_app(settings: Settings | None = None) -> Flask:
    if settings is None:
        _load_dotenv()
        settings = Settings.from_env()
    settings.ensure_dirs()
    _configure_logging(settings.log_level)

    app = Flask(__name__)
    app.request_class = _Request
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SQLALCHEMY_DATABASE_URI=settings.sqlalchemy_url,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
        MAX_CONTENT_LENGTH=settings.max_upload_mb * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_TIME_LIMIT=None,
        LJ_SETTINGS=settings,
    )
    if settings.trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(MIGRATIONS_DIR), render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)
    oauth.init_app(app)
    with app.app_context():
        if db.engine.dialect.name == "sqlite":
            event.listen(db.engine, "connect", _sqlite_pragmas)

    from . import auth, cli, web
    from .blueprints import register_blueprints

    auth.init_app(app)
    web.init_app(app)
    register_blueprints(app)
    cli.init_app(app)
    return app


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a runtime dependency
        return
    load_dotenv(override=False)


def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(level)
    logging.getLogger("pyftpdlib").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
