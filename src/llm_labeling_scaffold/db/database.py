from __future__ import annotations

import os
from collections.abc import Callable

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(RuntimeError):
    pass


def resolve_database_url(database_url: str | None = None) -> str:
    value = database_url or os.environ.get("LLS_DATABASE_URL")
    if value:
        return value

    components = {
        "username": os.environ.get("LLS_DATABASE_USER"),
        "password": os.environ.get("LLS_DATABASE_PASSWORD"),
        "host": os.environ.get("LLS_DATABASE_HOST"),
        "port": os.environ.get("LLS_DATABASE_PORT"),
        "database": os.environ.get("LLS_DATABASE_NAME"),
    }
    if not any(components.values()):
        raise DatabaseConfigurationError(
            "LLS_DATABASE_URL or all LLS_DATABASE_{USER,PASSWORD,HOST,PORT,NAME} values are required",
        )
    missing = [name for name, component in components.items() if not component]
    if missing:
        raise DatabaseConfigurationError(f"incomplete database configuration: {', '.join(missing)}")
    try:
        port = int(components["port"])
    except ValueError as exc:
        raise DatabaseConfigurationError("LLS_DATABASE_PORT must be an integer") from exc
    return URL.create(
        "postgresql+psycopg",
        username=components["username"],
        password=components["password"],
        host=components["host"],
        port=port,
        database=components["database"],
    ).render_as_string(hide_password=False)


def create_database_engine(database_url: str | None = None, **kwargs) -> Engine:
    url = resolve_database_url(database_url)
    backend = make_url(url).get_backend_name()
    if backend != "sqlite":
        kwargs.setdefault("pool_pre_ping", True)
    engine = create_engine(url, **kwargs)
    if backend == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def create_session_factory(engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
