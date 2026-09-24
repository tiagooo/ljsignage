"""Database models (SPEC §4). Every datetime column is timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import current_app
from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    types,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db
from .timeutil import utcnow


class UTCDateTime(types.TypeDecorator):
    """Stores aware datetimes as naive UTC and returns aware UTC datetimes.

    Naive datetimes are rejected so a local time can never be stored by mistake.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: store timezone-aware UTC datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def wire_to_display(name: str) -> str:
    """FTP names travel as latin-1 (byte-faithful); most Pis store UTF-8 names."""
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


device_groups = db.Table(
    "device_groups",
    db.Column("device_id", ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
    db.Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class User(UserMixin, db.Model):
    """Panel user. Only name and email are kept (RGPD).

    Admins come from ADMIN_EMAILS; editors are authorised by an admin in the panel.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    authorized: Mapped[bool] = mapped_column(default=False)
    authorized_by: Mapped[str | None] = mapped_column(String(254))
    authorized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def role(self) -> str | None:
        if self.email in current_app.config["LJ_SETTINGS"].admin_emails:
            return "admin"
        return "editor" if self.authorized else None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.role is not None

    @property
    def display_name(self) -> str:
        return self.name or self.email


class Device(db.Model):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    store_number: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    hostname: Mapped[str] = mapped_column(String(253))
    host: Mapped[str] = mapped_column(String(253))
    ftp_port: Mapped[int] = mapped_column(default=21)
    login_home: Mapped[str] = mapped_column(String(500), default="/")
    config_video_path: Mapped[str | None] = mapped_column(String(500))
    # Effective FTP path of the video folder, only set once confirmed (or configured).
    video_path: Mapped[str | None] = mapped_column(String(500))
    video_path_source: Mapped[str | None] = mapped_column(String(20))  # config | discovery
    video_path_confirmed_by: Mapped[str | None] = mapped_column(String(254))
    video_path_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    enabled: Mapped[bool] = mapped_column(default=False)
    max_bytes: Mapped[int | None] = mapped_column(BigInteger)
    in_config: Mapped[bool] = mapped_column(default=True)

    status: Mapped[str] = mapped_column(String(20), default="unknown")
    ftp_banner: Mapped[str | None] = mapped_column(String(200))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reconcile_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    snapshot_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    staging_exists: Mapped[bool] = mapped_column(default=False)
    discovery: Mapped[dict | None] = mapped_column(JSON)
    discovery_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    plan_digest: Mapped[str | None] = mapped_column(String(64))
    lock_owner: Mapped[str | None] = mapped_column(String(100))
    lock_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    groups: Mapped[list[Group]] = relationship(
        secondary=device_groups, back_populates="devices", order_by="Group.name"
    )
    files: Mapped[list[DeviceFile]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        order_by="DeviceFile.remote_name",
        foreign_keys="DeviceFile.device_id",
    )

    @property
    def label(self) -> str:
        return f"{self.store_number} · {self.name}"

    @property
    def video_path_display(self) -> str | None:
        return wire_to_display(self.video_path) if self.video_path else None


class Group(db.Model):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    in_config: Mapped[bool] = mapped_column(default=True)

    devices: Mapped[list[Device]] = relationship(
        secondary=device_groups, back_populates="groups", order_by="Device.store_number"
    )


class Video(db.Model):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(40))
    original_filename: Mapped[str] = mapped_column(String(255))
    original_path: Mapped[str | None] = mapped_column(String(500))  # relative to DATA_DIR
    media_path: Mapped[str | None] = mapped_column(String(500))
    thumbnail_path: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_s: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None]
    height: Mapped[int | None]
    fps: Mapped[float | None] = mapped_column(Float)
    source_info: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    error: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(20), default="upload")  # upload | legacy
    origin_detail: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    assignments: Mapped[list[Assignment]] = relationship(back_populates="video")

    __table_args__ = (
        CheckConstraint("status IN ('processing', 'ready', 'failed')", name="status"),
    )


class Assignment(db.Model):
    """Schedule entry: a video on all stores, a group or a single store, for a period."""

    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="RESTRICT"))
    target_type: Mapped[str] = mapped_column(String(10))  # all | group | device
    target_id: Mapped[int | None]
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    position: Mapped[int] = mapped_column(default=100)
    is_fallback: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(254))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    video: Mapped[Video] = relationship(back_populates="assignments")

    __table_args__ = (
        CheckConstraint("target_type IN ('all', 'group', 'device')", name="target_type"),
        CheckConstraint("end_at IS NULL OR end_at > start_at", name="period"),
        CheckConstraint(
            "(target_type = 'all' AND target_id IS NULL)"
            " OR (target_type != 'all' AND target_id IS NOT NULL)",
            name="target",
        ),
    )


class DeviceFile(db.Model):
    """Last known state of a file on a Pi (refreshed at every reconciliation)."""

    __tablename__ = "device_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    location: Mapped[str] = mapped_column(String(10))  # root | staging
    remote_name: Mapped[str] = mapped_column(String(1000))  # as on the wire
    is_dir: Mapped[bool] = mapped_column(default=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    remote_mtime: Mapped[datetime | None] = mapped_column(UTCDateTime)
    state: Mapped[str] = mapped_column(String(20))  # active | staged | partial | legacy | other
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    issues: Mapped[list] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delete_requested_by: Mapped[str | None] = mapped_column(String(254))
    delete_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    adopted_video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL")
    )

    device: Mapped[Device] = relationship(back_populates="files", foreign_keys=[device_id])
    video: Mapped[Video | None] = relationship(foreign_keys=[video_id])
    adopted_video: Mapped[Video | None] = relationship(foreign_keys=[adopted_video_id])

    __table_args__ = (UniqueConstraint("device_id", "location", "remote_name"),)

    @property
    def display_name(self) -> str:
        return wire_to_display(self.remote_name)


class AuditLog(db.Model):
    """Activity log. Users are identified by name and email only (RGPD)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    user_email: Mapped[str | None] = mapped_column(String(254), index=True)  # None = system
    user_name: Mapped[str | None] = mapped_column(String(200))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    device_code: Mapped[str | None] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[str] = mapped_column(String(20), default="ok")  # ok|error|dry_run|info|denied


class Job(db.Model):
    """Worker task queue (no Redis): transcode, reconcile, discover, adopt."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    progress: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    not_before: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    worker_id: Mapped[str | None] = mapped_column(String(100))

    device: Mapped[Device | None] = relationship()
    video: Mapped[Video | None] = relationship()


class SystemState(db.Model):
    """Small key/value store (worker heartbeat, next reconciliation time)."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
