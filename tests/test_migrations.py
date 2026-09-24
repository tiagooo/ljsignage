"""The Alembic migrations create exactly the schema described by the models."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import upgrade

from lj_signage import create_app
from lj_signage.extensions import db
from tests.conftest import make_settings


def test_migrations_match_the_models(tmp_path):
    app = create_app(make_settings(tmp_path))
    with app.app_context():
        upgrade()
        with db.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, db.metadata)
        assert differences == []
        journal = db.session.execute(db.text("PRAGMA journal_mode")).scalar()
        assert journal == "wal"
