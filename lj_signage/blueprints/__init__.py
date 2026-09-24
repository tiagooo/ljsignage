"""HTTP routes. The web process only reads/writes the database and creates jobs."""

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from . import activity, api, auth, dashboard, devices, library, plan, schedule

    for module in (auth, dashboard, devices, library, schedule, plan, activity, api):
        app.register_blueprint(module.bp)
