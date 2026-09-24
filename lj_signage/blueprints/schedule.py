"""Programação: create and edit assignments (video, target, start/end, order), list and
calendar views (SPEC §9.4). Times are typed in Europe/Lisbon and stored in UTC."""

from __future__ import annotations

import calendar as cal
from datetime import date, datetime, time, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import or_
from wtforms import BooleanField, IntegerField, RadioField, SelectField, StringField
from wtforms.fields import DateTimeLocalField
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from .. import audit
from ..extensions import db
from ..models import Assignment, Device, Group, Video
from ..reconciler.state import applicable
from ..status import worker_status
from ..summaries import on_air_estimate, summarize, target_label
from ..timeutil import NonexistentLocalTime, local_to_utc, to_local, utcnow

bp = Blueprint("schedule", __name__, url_prefix="/programacao")

MONTHS = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]  # fmt: skip
WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


class AssignmentForm(FlaskForm):
    video_id = SelectField("Vídeo", coerce=int, validators=[DataRequired("Escolha um vídeo.")])
    target_type = RadioField(
        "Onde passa",
        choices=[("all", "Todas as lojas"), ("group", "Um grupo de lojas"), ("device", "Uma loja")],
        default="all",
    )
    group_id = SelectField("Grupo", coerce=int, validate_choice=False, default=0)
    device_id = SelectField("Loja", coerce=int, validate_choice=False, default=0)
    start_at = DateTimeLocalField(
        "Início", format="%Y-%m-%dT%H:%M", validators=[DataRequired("Indique o início.")]
    )
    end_at = DateTimeLocalField("Fim", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    position = IntegerField(
        "Ordem",
        default=100,
        validators=[NumberRange(1, 9999, "A ordem tem de estar entre 1 e 9999.")],
    )
    is_fallback = BooleanField("Vídeo de reserva")
    note = StringField("Nota interna", validators=[Optional(), Length(max=200)])


def _settings():
    return current_app.config["LJ_SETTINGS"]


def _choices(form: AssignmentForm, current: Assignment | None = None) -> None:
    videos = db.session.execute(
        db.select(Video)
        .where(Video.deleted_at.is_(None), Video.status != "failed")
        .order_by(Video.title)
    ).scalars()
    form.video_id.choices = [(0, "— escolher —")] + [
        (v.id, v.title + ("" if v.status == "ready" else " (a processar)")) for v in videos
    ]
    if current is not None and current.video_id not in {c[0] for c in form.video_id.choices}:
        form.video_id.choices.append((current.video_id, current.video.title))
    groups = db.session.execute(
        db.select(Group).where(Group.in_config.is_(True)).order_by(Group.name)
    ).scalars()
    form.group_id.choices = [(0, "— escolher —")] + [
        (g.id, f"{g.name} ({len(g.devices)} lojas)") for g in groups
    ]
    devices = db.session.execute(
        db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    ).scalars()
    form.device_id.choices = [(0, "— escolher —")] + [(d.id, d.label) for d in devices]


def _to_utc(form_field, label: str, errors: list[str], notes: list[str]) -> datetime | None:
    value = form_field.data
    if value is None:
        return None
    try:
        utc, ambiguous = local_to_utc(value, _settings().tz)
    except NonexistentLocalTime:
        errors.append(
            f"{label}: {value:%d/%m/%Y %H:%M} não existe (a hora avança nessa noite). "
            "Escolha outra hora."
        )
        return None
    if ambiguous:
        notes.append(
            f"{label}: {value:%d/%m/%Y %H:%M} acontece duas vezes (a hora recua nessa noite); "
            "foi usada a primeira vez (hora de verão)."
        )
    return utc


def _apply_form(form: AssignmentForm, assignment: Assignment) -> list[str] | None:
    """Validate and copy the form into the assignment. Returns notes, or None on error."""
    errors: list[str] = []
    notes: list[str] = []
    video = db.session.get(Video, form.video_id.data) if form.video_id.data else None
    if video is None or video.deleted_at is not None:
        errors.append("Escolha um vídeo da biblioteca.")
    target_type = form.target_type.data
    target_id = None
    if target_type == "group":
        group = db.session.get(Group, form.group_id.data) if form.group_id.data else None
        if group is None:
            errors.append("Escolha o grupo de lojas.")
        else:
            target_id = group.id
    elif target_type == "device":
        device = db.session.get(Device, form.device_id.data) if form.device_id.data else None
        if device is None:
            errors.append("Escolha a loja.")
        else:
            target_id = device.id
    elif target_type != "all":
        errors.append("Escolha onde o vídeo passa.")
    start = _to_utc(form.start_at, "Início", errors, notes)
    end = _to_utc(form.end_at, "Fim", errors, notes)
    if start and end and end <= start:
        errors.append("O fim tem de ser depois do início.")
    if errors:
        for message in errors:
            flash(message, "error")
        return None
    assignment.video_id = video.id
    assignment.target_type = target_type
    assignment.target_id = target_id
    assignment.start_at = start
    assignment.end_at = end
    assignment.position = form.position.data
    assignment.is_fallback = bool(form.is_fallback.data)
    assignment.note = (form.note.data or "").strip() or None
    return notes


def _describe(assignment: Assignment) -> str:
    tz = _settings().tz
    period = f"{to_local(assignment.start_at, tz):%d/%m/%Y %H:%M}"
    period += (
        f" → {to_local(assignment.end_at, tz):%d/%m/%Y %H:%M}"
        if assignment.end_at
        else " → sem fim"
    )
    kind = " (reserva)" if assignment.is_fallback else ""
    return f"«{assignment.video.title}»{kind} em {target_label(assignment)}, {period}"


def state_of(assignment: Assignment, now: datetime) -> tuple[str, str]:
    """(key, label) for the list."""
    if assignment.end_at is not None and assignment.end_at <= now:
        return "ended", "Terminada"
    if assignment.video.deleted_at is not None:
        return "invalid", "Vídeo apagado"
    if assignment.video.status == "failed":
        return "invalid", "Vídeo com erro"
    if assignment.video.status != "ready":
        return "waiting", "À espera do vídeo"
    if assignment.start_at > now:
        return "scheduled", "Agendada"
    return "running", "A decorrer"


def _target_devices(assignment: Assignment) -> list[Device]:
    devices = db.session.execute(
        db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    ).scalars()
    result = []
    for device in devices:
        if (
            assignment.target_type == "all"
            or assignment.target_type == "group"
            and any(g.id == assignment.target_id for g in device.groups)
            or assignment.target_type == "device"
            and assignment.target_id == device.id
        ):
            result.append(device)
    return result


@bp.get("/")
def index():
    settings = _settings()
    now = utcnow()
    view = request.args.get("ver", "atuais")
    select = db.select(Assignment).join(Video)
    if view == "atuais":
        select = select.where(or_(Assignment.end_at.is_(None), Assignment.end_at > now))
    elif view == "terminadas":
        select = select.where(Assignment.end_at.is_not(None), Assignment.end_at <= now)
    assignments = (
        db.session.execute(
            select.order_by(Assignment.is_fallback, Assignment.position, Assignment.start_at)
        )
        .scalars()
        .all()
    )
    worker = worker_status(now)
    summaries = {}
    rows = []
    for assignment in assignments:
        key, label = state_of(assignment, now)
        devices = _target_devices(assignment)
        estimate = None
        if key in ("running", "scheduled") and assignment.start_at <= now + timedelta(hours=24):
            for device in devices:
                if device.id not in summaries:
                    summaries[device.id] = summarize(device, now, settings)
            estimate = on_air_estimate(assignment, devices, summaries, worker, now, settings)
        rows.append(
            {
                "a": assignment,
                "state": key,
                "state_label": label,
                "target": target_label(assignment),
                "devices": devices,
                "estimate": estimate,
            }
        )
    fallback_ok = any(r["a"].is_fallback and r["state"] == "running" for r in rows)
    return render_template(
        "schedule/index.html", rows=rows, view=view, now=now, fallback_ok=fallback_ok
    )


@bp.route("/nova", methods=["GET", "POST"])
def create():
    form = AssignmentForm()
    _choices(form)
    if request.method == "GET":
        tz = _settings().tz
        start = to_local(utcnow(), tz).replace(second=0, microsecond=0, tzinfo=None)
        form.start_at.data = start
        video_id = request.args.get("video", type=int)
        if video_id:
            form.video_id.data = video_id
        if request.args.get("reserva"):
            form.is_fallback.data = True
    elif form.validate_on_submit():
        assignment = Assignment(created_by=current_user.email, updated_by=current_user.email)
        notes = _apply_form(form, assignment)
        if notes is not None:
            db.session.add(assignment)
            db.session.flush()
            audit.record("assignment_created", _describe(assignment))
            db.session.commit()
            for note in notes:
                flash(note, "info")
            flash("Programação criada.", "success")
            return redirect(url_for("schedule.index"))
    else:
        _flash_form_errors(form)
    return render_template("schedule/form.html", form=form, assignment=None)


@bp.route("/<int:assignment_id>/editar", methods=["GET", "POST"])
def edit(assignment_id: int):
    assignment = db.session.get(Assignment, assignment_id) or abort(404)
    form = AssignmentForm(obj=None)
    _choices(form, assignment)
    tz = _settings().tz
    if request.method == "GET":
        form.video_id.data = assignment.video_id
        form.target_type.data = assignment.target_type
        form.group_id.data = assignment.target_id if assignment.target_type == "group" else 0
        form.device_id.data = assignment.target_id if assignment.target_type == "device" else 0
        form.start_at.data = to_local(assignment.start_at, tz).replace(tzinfo=None)
        form.end_at.data = (
            to_local(assignment.end_at, tz).replace(tzinfo=None) if assignment.end_at else None
        )
        form.position.data = assignment.position
        form.is_fallback.data = assignment.is_fallback
        form.note.data = assignment.note
    elif form.validate_on_submit():
        before = _describe(assignment)
        notes = _apply_form(form, assignment)
        if notes is not None:
            assignment.updated_by = current_user.email
            audit.record("assignment_updated", f"{before} → {_describe(assignment)}")
            db.session.commit()
            for note in notes:
                flash(note, "info")
            flash("Programação alterada.", "success")
            return redirect(url_for("schedule.index"))
        db.session.rollback()
    else:
        _flash_form_errors(form)
    return render_template("schedule/form.html", form=form, assignment=assignment)


@bp.post("/<int:assignment_id>/terminar")
def end_now(assignment_id: int):
    assignment = db.session.get(Assignment, assignment_id) or abort(404)
    now = utcnow()
    if assignment.end_at is not None and assignment.end_at <= now:
        flash("Esta programação já terminou.", "info")
    else:
        if assignment.start_at >= now:
            assignment.start_at = now - timedelta(seconds=1)
        assignment.end_at = now
        assignment.updated_by = current_user.email
        audit.record("assignment_ended", _describe(assignment))
        db.session.commit()
        flash("Programação terminada: o vídeo sai das lojas na próxima sincronização.", "success")
    return redirect(request.referrer or url_for("schedule.index"))


@bp.post("/<int:assignment_id>/apagar")
def delete(assignment_id: int):
    assignment = db.session.get(Assignment, assignment_id) or abort(404)
    audit.record("assignment_deleted", _describe(assignment))
    db.session.delete(assignment)
    db.session.commit()
    flash("Programação apagada.", "success")
    return redirect(url_for("schedule.index"))


def _flash_form_errors(form: FlaskForm) -> None:
    for errors in form.errors.values():
        for message in errors:
            flash(str(message), "error")


# --- calendar -------------------------------------------------------------------------


@bp.get("/calendario")
def calendar():
    settings = _settings()
    tz = settings.tz
    today = to_local(utcnow(), tz).date()
    try:
        year, month = (int(x) for x in request.args.get("mes", "").split("-"))
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    weeks = cal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    grid_start = datetime.combine(weeks[0][0], time(0, 0), tzinfo=tz)
    grid_end = datetime.combine(weeks[-1][-1] + timedelta(days=1), time(0, 0), tzinfo=tz)
    device_code = request.args.get("loja") or None
    select = (
        db.select(Assignment)
        .join(Video)
        .where(
            Video.deleted_at.is_(None),
            Assignment.start_at < grid_end,
            or_(Assignment.end_at.is_(None), Assignment.end_at > grid_start),
        )
    )
    device = None
    if device_code:
        device = db.session.execute(
            db.select(Device).filter_by(code=device_code)
        ).scalar_one_or_none()
        if device is not None:
            select = select.where(applicable(device))
    assignments = (
        db.session.execute(select.order_by(Assignment.position, Assignment.start_at))
        .scalars()
        .all()
    )
    days = []
    for week in weeks:
        row = []
        for day in week:
            # Local midnight to midnight: 23 or 25 hours on DST change days.
            start = datetime.combine(day, time(0, 0), tzinfo=tz)
            end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
            items = [
                a
                for a in assignments
                if a.start_at < end and (a.end_at is None or a.end_at > start)
            ]
            row.append(
                {
                    "date": day,
                    "in_month": day.month == first.month,
                    "today": day == today,
                    "items": items,
                }
            )
        days.append(row)
    previous = (first - timedelta(days=1)).replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    devices = db.session.execute(
        db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    ).scalars()
    return render_template(
        "schedule/calendar.html",
        weeks=days,
        title=f"{MONTHS[first.month - 1]} de {first.year}",
        weekdays=WEEKDAYS,
        previous=f"{previous:%Y-%m}",
        following=f"{following:%Y-%m}",
        devices=list(devices),
        device=device,
        colors={a.id: a.video_id % 6 for a in assignments},
    )
