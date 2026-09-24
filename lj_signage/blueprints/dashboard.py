"""Painel: grid of stores with state, loop duration, last read and alerts (SPEC §9.1)."""

from __future__ import annotations

from collections import Counter

from flask import Blueprint, current_app, render_template

from ..extensions import db
from ..models import Device
from ..summaries import summarize
from ..timeutil import utcnow

bp = Blueprint("dashboard", __name__)


def _context() -> dict:
    settings = current_app.config["LJ_SETTINGS"]
    now = utcnow()
    devices = db.session.execute(
        db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    ).scalars()
    summaries = [summarize(device, now, settings) for device in devices]
    counts = Counter(s.device.status for s in summaries)
    attention = sum(1 for s in summaries if s.worst in ("error", "warning"))
    return {"summaries": summaries, "counts": counts, "attention": attention}


@bp.get("/")
def index():
    return render_template("dashboard/index.html", **_context())


@bp.get("/painel/grelha")
def grid():
    return render_template("dashboard/_grid.html", **_context())
