"""Plano: what the next reconciliation would do in each store (SPEC §9.5).

Computed here from the last state read from each Pi and the current schedule,
with the same pure planner the worker uses — no FTP from the web process.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

from ..extensions import db
from ..models import Device
from ..summaries import summarize
from ..timeutil import utcnow

bp = Blueprint("plan", __name__, url_prefix="/plano")


@bp.get("/")
def index():
    settings = current_app.config["LJ_SETTINGS"]
    now = utcnow()
    devices = db.session.execute(
        db.select(Device).where(Device.in_config.is_(True)).order_by(Device.store_number)
    ).scalars()
    summaries = [summarize(device, now, settings) for device in devices]
    total = sum(s.pending_actions for s in summaries)
    return render_template("plan/index.html", summaries=summaries, total=total)
