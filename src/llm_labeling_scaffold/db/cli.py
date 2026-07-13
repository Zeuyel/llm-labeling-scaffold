from __future__ import annotations

import argparse
import json
import os

from sqlalchemy.orm import Session

from .bootstrap import bootstrap_admin
from .database import create_database_engine, resolve_database_url
from .enums import PrincipalType
from .migration import upgrade_database


def add_db_parser(subparsers) -> None:
    db = subparsers.add_parser("db")
    db_sub = db.add_subparsers(dest="db_cmd", required=True)

    upgrade = db_sub.add_parser("upgrade")
    upgrade.add_argument("--database-url")
    upgrade.add_argument("--revision", default="head")

    bootstrap = db_sub.add_parser("bootstrap")
    bootstrap.add_argument("--database-url")
    bootstrap.add_argument("--issuer")
    bootstrap.add_argument("--subject")
    bootstrap.add_argument("--principal-type", choices=[value.value for value in PrincipalType])
    bootstrap.add_argument("--display-name")
    bootstrap.add_argument("--email")
    bootstrap.add_argument("--workspace-slug")
    bootstrap.add_argument("--workspace-name")


def handle_db_command(args: argparse.Namespace) -> None:
    if args.db_cmd == "upgrade":
        result = upgrade_database(args.database_url, args.revision)
        _print_json(result.to_dict())
        return

    database_url = resolve_database_url(args.database_url)
    issuer = _required_value(args.issuer, "LLS_BOOTSTRAP_ISSUER", "--issuer")
    subject = _required_value(args.subject, "LLS_BOOTSTRAP_SUBJECT", "--subject")
    workspace_slug = _required_value(
        args.workspace_slug,
        "LLS_BOOTSTRAP_WORKSPACE_SLUG",
        "--workspace-slug",
    )
    workspace_name = _required_value(
        args.workspace_name,
        "LLS_BOOTSTRAP_WORKSPACE_NAME",
        "--workspace-name",
    )
    principal_type = PrincipalType(
        args.principal_type or os.environ.get("LLS_BOOTSTRAP_PRINCIPAL_TYPE", PrincipalType.USER.value),
    )
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            result = bootstrap_admin(
                session,
                issuer=issuer,
                subject=subject,
                workspace_slug=workspace_slug,
                workspace_name=workspace_name,
                principal_type=principal_type,
                display_name=args.display_name or os.environ.get("LLS_BOOTSTRAP_DISPLAY_NAME"),
                email=args.email or os.environ.get("LLS_BOOTSTRAP_EMAIL"),
            )
    finally:
        engine.dispose()
    _print_json(result.to_dict())


def _required_value(argument: str | None, environment_name: str, option_name: str) -> str:
    value = argument or os.environ.get(environment_name)
    if not value:
        raise SystemExit(f"{option_name} or {environment_name} is required")
    return value


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
