"""Template filters, shared template context, error pages and security headers."""

from __future__ import annotations

from flask import Flask, current_app, render_template, request
from flask_login import current_user
from flask_wtf.csrf import CSRFError

from . import audit, jobs
from .models import wire_to_display
from .naming import ISSUE_LABELS
from .reconciler import planner as pl
from .status import worker_status
from .summaries import STATUS_LABELS
from .timeutil import (
    fmt_bytes,
    fmt_date,
    fmt_datetime,
    fmt_duration,
    fmt_number,
    fmt_relative,
    fmt_time,
    utcnow,
)

NAV = [
    ("dashboard.index", "Painel"),
    ("library.index", "Biblioteca"),
    ("schedule.index", "Programação"),
    ("plan.index", "Plano"),
    ("activity.index", "Atividade"),
]

CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
    "script-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'self'"
)

TARGET_LABELS = {"all": "Todas as lojas", "group": "Grupo", "device": "Loja"}
VIDEO_STATUS_LABELS = {"processing": "A processar", "ready": "Pronto", "failed": "Falhou"}


def init_app(app: Flask) -> None:
    settings = app.config["LJ_SETTINGS"]
    tz = settings.tz
    app.jinja_env.filters.update(
        dt=lambda value: fmt_datetime(value, tz),
        date=lambda value: fmt_date(value, tz),
        time=lambda value: fmt_time(value, tz),
        ago=lambda value: fmt_relative(value, utcnow(), tz),
        duration=fmt_duration,
        filesize=fmt_bytes,
        number=fmt_number,
        wire=wire_to_display,
    )
    app.jinja_env.globals.update(
        ACTION_LABELS=audit.ACTION_LABELS,
        RESULT_LABELS=audit.RESULT_LABELS,
        JOB_LABELS=jobs.JOB_LABELS,
        JOB_STATUS_LABELS=jobs.STATUS_LABELS,
        ISSUE_LABELS=ISSUE_LABELS,
        STATUS_LABELS=STATUS_LABELS,
        TARGET_LABELS=TARGET_LABELS,
        VIDEO_STATUS_LABELS=VIDEO_STATUS_LABELS,
        describe=pl.describe,
        NAV=NAV,
    )

    @app.context_processor
    def shared_context():
        if request.endpoint in (None, "static"):
            return {}
        current = current_app.config["LJ_SETTINGS"]
        context = {"dry_run": current.dry_run, "settings_view": _settings_view(current)}
        if current_user.is_authenticated:
            context["worker"] = worker_status()
        return context

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    @app.errorhandler(CSRFError)
    def csrf_error(_error):
        message = "O formulário expirou. Recarregue a página e tente de novo."
        return render_template("errors/error.html", code=400, message=message), 400

    for code, message in (
        (403, "Não tem permissão para esta ação. Fale com um administrador."),
        (404, "Página não encontrada."),
        (413, f"Ficheiro demasiado grande (máximo {_size_label(settings.max_upload_mb)})."),
        (500, "Ocorreu um erro inesperado. Tente de novo dentro de momentos."),
    ):
        app.register_error_handler(code, _handler(code, message))


def _handler(code: int, message: str):
    def handle(_error):
        return render_template("errors/error.html", code=code, message=message), code

    return handle


def _settings_view(settings) -> dict:
    """The only settings templates may see (no secrets)."""
    return {
        "reconcile_interval_min": settings.reconcile_interval_min,
        "staging_lookahead_h": settings.staging_lookahead_h,
        "allowed_domain": settings.allowed_domain,
        "audio_enabled": settings.audio_enabled,
        "max_upload_mb": settings.max_upload_mb,
        "max_upload_label": _size_label(settings.max_upload_mb),
    }


def _size_label(megabytes: int) -> str:
    if megabytes < 1024:
        return f"{megabytes} MB"
    gigabytes = f"{megabytes / 1024:.1f}".rstrip("0").rstrip(".")
    return f"{gigabytes.replace('.', ',')} GB"
