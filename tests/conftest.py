from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel

from jobbot.config import PROJECT_ROOT, Settings
from jobbot.db import build_engine
from jobbot.models import Company
from jobbot.profile import Profile, load_profile


@pytest.fixture(scope="session")
def profile() -> Profile:
    """The real applicant profile, which is personal data and not committed.

    Scoring and drafting are meaningless without a concrete profile, and a
    synthetic one would test different thresholds than the ones actually in use.
    So these tests run against the real file when it exists and skip with a
    clear reason when it does not — a fresh clone gets an honest result rather
    than a wall of failures.
    """
    path = PROJECT_ROOT / "cv" / "profile.json"
    if not path.exists():
        pytest.skip("cv/profile.json is absent; copy cv/profile.example.json and fill it in")
    return load_profile(path)


@pytest.fixture
def settings() -> Settings:
    """Settings built from defaults only, ignoring any local .env."""
    return Settings(_env_file=None, database_url="sqlite://")


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = build_engine(settings)
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def company(session: Session) -> Company:
    record = Company(name="Acme Yazilim", domain="acme.com.tr", country="TR")
    session.add(record)
    session.commit()
    session.refresh(record)
    return record
