from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from .database import create_database_engine, resolve_database_url
from .enums import MigrationStatus
from .models import MigrationRun


@dataclass(frozen=True)
class MigrationResult:
    target_revision: str
    applied_revision: str | None
    status: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def build_alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("alembic")))
    config.set_main_option("sqlalchemy.url", resolve_database_url(database_url).replace("%", "%%"))
    return config


def upgrade_database(database_url: str | None = None, target_revision: str = "head") -> MigrationResult:
    url = resolve_database_url(database_url)
    started_at = datetime.now(timezone.utc)
    try:
        command.upgrade(build_alembic_config(url), target_revision)
    except Exception as exc:
        _record_run_if_available(
            url,
            target_revision=target_revision,
            applied_revision=None,
            status=MigrationStatus.FAILED,
            started_at=started_at,
            error=str(exc),
        )
        raise

    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            applied_revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    _record_run_if_available(
        url,
        target_revision=target_revision,
        applied_revision=applied_revision,
        status=MigrationStatus.SUCCEEDED,
        started_at=started_at,
        error=None,
    )
    return MigrationResult(
        target_revision=target_revision,
        applied_revision=applied_revision,
        status=MigrationStatus.SUCCEEDED.value,
    )


def _record_run_if_available(
    database_url: str,
    *,
    target_revision: str,
    applied_revision: str | None,
    status: MigrationStatus,
    started_at: datetime,
    error: str | None,
) -> None:
    engine = create_database_engine(database_url)
    try:
        if not inspect(engine).has_table(MigrationRun.__tablename__):
            return
        with Session(engine) as session, session.begin():
            session.add(
                MigrationRun(
                    command="upgrade",
                    target_revision=target_revision,
                    applied_revision=applied_revision,
                    status=status,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    error=error,
                )
            )
    finally:
        engine.dispose()
