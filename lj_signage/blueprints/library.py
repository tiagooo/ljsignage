"""Biblioteca: upload, conversion status, thumbnail, duration, where it is scheduled
(SPEC §9.3). Uploads only store the original and queue the conversion job."""

from __future__ import annotations

from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import or_
from werkzeug.utils import secure_filename
from wtforms import StringField
from wtforms.validators import DataRequired, Length

from .. import audit, jobs
from ..extensions import db
from ..models import Assignment, Device, DeviceFile, Job, Video
from ..naming import slugify
from ..summaries import target_label
from ..timeutil import utcnow

bp = Blueprint("library", __name__, url_prefix="/biblioteca")

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mxf", ".webm", ".mpg", ".mpeg", ".ts",
    ".h264", ".wmv", ".3gp",
}  # fmt: skip


class VideoForm(FlaskForm):
    title = StringField("Título", validators=[DataRequired(), Length(max=200)])


def _settings():
    return current_app.config["LJ_SETTINGS"]


def _video(video_id: int) -> Video:
    video = db.session.get(Video, video_id)
    if video is None or video.deleted_at is not None:
        abort(404)
    return video


def _latest_jobs(video_ids: list[int]) -> dict[int, Job]:
    if not video_ids:
        return {}
    rows = db.session.execute(
        db.select(Job).where(Job.type == "transcode", Job.video_id.in_(video_ids)).order_by(Job.id)
    ).scalars()
    return {job.video_id: job for job in rows}


def _schedule_counts(video_ids: list[int]) -> dict[int, int]:
    now = utcnow()
    counts: dict[int, int] = {}
    rows = db.session.execute(
        db.select(Assignment.video_id).where(
            Assignment.video_id.in_(video_ids),
            or_(Assignment.end_at.is_(None), Assignment.end_at > now),
        )
    ).scalars()
    for video_id in rows:
        counts[video_id] = counts.get(video_id, 0) + 1
    return counts


def title_from_filename(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    return " ".join(stem.split())[:200] or "Vídeo"


@bp.get("/")
def index():
    query = request.args.get("q", "").strip()
    select = db.select(Video).where(Video.deleted_at.is_(None))
    if query:
        select = select.where(Video.title.ilike(f"%{query}%"))
    videos = db.session.execute(select.order_by(Video.created_at.desc())).scalars().all()
    ids = [v.id for v in videos]
    return render_template(
        "library/index.html",
        videos=videos,
        latest_jobs=_latest_jobs(ids),
        schedule_counts=_schedule_counts(ids),
        query=query,
        accept=",".join(sorted(ALLOWED_EXTENSIONS)),
    )


@bp.post("/enviar")
def upload():
    settings = _settings()
    file = request.files.get("file")
    if file is None or not file.filename:
        return render_template("library/_upload_error.html", message="Nenhum ficheiro."), 400
    extension = Path(file.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        message = f"Formato {extension or '(sem extensão)'} não suportado."
        return render_template("library/_upload_error.html", message=message), 400
    title = (request.form.get("title") or "").strip()[:200] or title_from_filename(file.filename)
    video = Video(
        title=title,
        slug=slugify(title),
        original_filename=file.filename[:255],
        status="processing",
        origin="upload",
        created_by=current_user.email,
    )
    db.session.add(video)
    db.session.flush()
    safe = secure_filename(file.filename) or f"video{extension}"
    target = settings.media_dir / "originals" / f"{video.id}-{safe}"
    file.save(target)
    video.original_path = str(target.relative_to(settings.data_dir))
    job = jobs.enqueue("transcode", video_id=video.id, created_by=current_user.email)
    audit.record(
        "video_uploaded",
        f"«{video.title}» ({file.filename}, {target.stat().st_size / 1e6:.1f} MB).",
    )
    db.session.commit()
    return (
        render_template("library/_card.html", video=video, job=job, scheduled=0),
        201,
    )


@bp.get("/<int:video_id>")
def detail(video_id: int):
    video = _video(video_id)
    now = utcnow()
    assignments = (
        db.session.execute(
            db.select(Assignment)
            .where(Assignment.video_id == video.id)
            .order_by(Assignment.start_at.desc())
        )
        .scalars()
        .all()
    )
    on_devices = (
        db.session.execute(
            db.select(DeviceFile, Device)
            .join(Device, DeviceFile.device_id == Device.id)
            .where(DeviceFile.video_id == video.id)
            .order_by(Device.store_number)
        )
        .tuples()
        .all()
    )
    form = VideoForm(title=video.title)
    return render_template(
        "library/detail.html",
        video=video,
        job=_latest_jobs([video.id]).get(video.id),
        assignments=assignments,
        on_devices=on_devices,
        form=form,
        now=now,
        targets=_target_labels(assignments),
        blocking=_blocking_assignments(video, now),
    )


@bp.post("/<int:video_id>")
def update(video_id: int):
    video = _video(video_id)
    form = VideoForm()
    if form.validate_on_submit():
        old = video.title
        video.title = form.title.data.strip()
        audit.record("video_updated", f"Título: «{old}» → «{video.title}».")
        db.session.commit()
        flash("Título atualizado.", "success")
    else:
        flash("O título é obrigatório (máximo 200 caracteres).", "error")
    return redirect(url_for("library.detail", video_id=video.id))


def _blocking_assignments(video: Video, now) -> list[Assignment]:
    return [a for a in video.assignments if a.end_at is None or a.end_at > now]


@bp.post("/<int:video_id>/apagar")
def delete(video_id: int):
    settings = _settings()
    video = _video(video_id)
    if _blocking_assignments(video, utcnow()):
        flash(
            "Este vídeo ainda está programado. Termine ou apague primeiro a programação.",
            "error",
        )
        return redirect(url_for("library.detail", video_id=video.id))
    video.deleted_at = utcnow()
    for relative in (video.original_path, video.media_path, video.thumbnail_path):
        if relative:
            (settings.data_dir / relative).unlink(missing_ok=True)
    video.original_path = video.media_path = video.thumbnail_path = None
    audit.record(
        "video_deleted",
        f"«{video.title}» apagado da biblioteca (sai das lojas na próxima sincronização).",
    )
    db.session.commit()
    flash(f"«{video.title}» foi apagado da biblioteca.", "success")
    return redirect(url_for("library.index"))


@bp.post("/<int:video_id>/repetir")
def retry(video_id: int):
    video = _video(video_id)
    if video.status != "failed" or not video.original_path:
        abort(400)
    video.status = "processing"
    video.error = None
    jobs.enqueue("transcode", video_id=video.id, created_by=current_user.email)
    db.session.commit()
    flash("A conversão vai ser repetida.", "success")
    return redirect(url_for("library.detail", video_id=video.id))


@bp.get("/<int:video_id>/estado")
def status(video_id: int):
    video = _video(video_id)
    job = _latest_jobs([video.id]).get(video.id)
    scheduled = _schedule_counts([video.id]).get(video.id, 0)
    return render_template("library/_card.html", video=video, job=job, scheduled=scheduled)


@bp.get("/<int:video_id>/miniatura")
def thumbnail(video_id: int):
    video = _video(video_id)
    if not video.thumbnail_path:
        abort(404)
    path = _settings().data_dir / video.thumbnail_path
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=3600)


@bp.get("/<int:video_id>/video")
def media(video_id: int):
    video = _video(video_id)
    if video.status != "ready" or not video.media_path:
        abort(404)
    path = _settings().data_dir / video.media_path
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=3600)


def _target_labels(assignments: list[Assignment]) -> dict[int, str]:
    return {a.id: target_label(a) for a in assignments}
