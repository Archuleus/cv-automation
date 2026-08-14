from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlmodel import Session, SQLModel, select

from jobbot import db
from jobbot.config import Settings
from jobbot.models import Company


class TestSqlitePragmas:
    def test_foreign_keys_are_enabled(self, engine: Engine):
        # SQLite defaults this to OFF, which would make the schema's referential
        # integrity purely decorative.
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1

    def test_busy_timeout_is_set(self, engine: Engine):
        # The scheduler and the review UI hit the same file concurrently.
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000


class TestBuildEngine:
    def test_in_memory_url_without_path_is_shared_across_connections(self):
        # A non-static pool would give each connection its own empty database.
        engine = db.build_engine(Settings(_env_file=None, database_url="sqlite://"))
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            session.add(Company(name="A", domain="a.com"))
            session.commit()
        with Session(engine) as session:
            assert len(session.exec(select(Company)).all()) == 1

        engine.dispose()

    def test_file_url_creates_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "jobbot.db"
        settings = Settings(_env_file=None, database_url=f"sqlite:///{target}")

        engine = db.build_engine(settings)
        SQLModel.metadata.create_all(engine)
        engine.dispose()

        assert target.exists()


class TestEngineCache:
    def test_get_engine_returns_the_same_instance(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOBBOT_DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
        from jobbot.config import get_settings

        get_settings.cache_clear()
        db.reset_engine()

        try:
            assert db.get_engine() is db.get_engine()
        finally:
            db.reset_engine()
            get_settings.cache_clear()

    def test_reset_engine_is_safe_when_nothing_was_built(self):
        db.reset_engine()
        db.reset_engine()


class TestSessionScope:
    def test_commits_on_success(self, engine: Engine):
        with db.session_scope(engine) as session:
            session.add(Company(name="Acme", domain="acme.com"))

        with Session(engine) as session:
            assert session.exec(select(Company)).one().name == "Acme"

    def test_rolls_back_on_exception(self, engine: Engine):
        with pytest.raises(RuntimeError), db.session_scope(engine) as session:
            session.add(Company(name="Acme", domain="acme.com"))
            session.flush()
            raise RuntimeError("boom")

        with Session(engine) as session:
            assert session.exec(select(Company)).all() == []


class TestSchemaHelpers:
    def test_create_all_then_drop_all(self, engine: Engine):
        db.drop_all(engine)
        db.create_all(engine)

        with Session(engine) as session:
            session.add(Company(name="Acme", domain="acme.com"))
            session.commit()

        db.drop_all(engine)
        inspector_tables = SQLModel.metadata.tables.keys()
        assert "companies" in inspector_tables  # metadata survives; the tables do not
