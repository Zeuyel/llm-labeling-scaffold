from __future__ import annotations

import argparse
import json
import os
import uuid

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

    materialize = db_sub.add_parser("materialize")
    materialize.add_argument("--database-url")
    materialize.add_argument("--runs-root", default="runs")
    materialize.add_argument("--tasks-root", default="tasks")
    materialize.add_argument("--worker-id")
    materialize.add_argument("--lease-seconds", type=float, default=30.0)
    materialize.add_argument("--retry-delay-seconds", type=float, default=1.0)
    materialize.add_argument("--max-attempts", type=int, default=5)
    materialize.add_argument("--poll-seconds", type=float, default=1.0)
    materialize.add_argument("--materialization-id", type=uuid.UUID)
    materialize.add_argument("--drain-seconds", type=float)
    materialize.add_argument("--once", action="store_true")

    legacy_import = db_sub.add_parser("import-legacy-tasks")
    legacy_import.add_argument("--database-url")
    legacy_import.add_argument("--registry", required=True)
    legacy_import.add_argument("--workspace", required=True)
    legacy_import.add_argument("--actor-issuer", required=True)
    legacy_import.add_argument("--actor-subject", required=True)
    legacy_import.add_argument("--caller-issuer")
    legacy_import.add_argument("--caller-subject")
    legacy_import.add_argument("--confirm", action="store_true")

    status = db_sub.add_parser("materialization-status")
    status.add_argument("materialization_id", type=uuid.UUID)
    status.add_argument("--database-url")
    status.add_argument("--runs-root", default="runs")


def handle_db_command(args: argparse.Namespace) -> None:
    if args.db_cmd == "upgrade":
        result = upgrade_database(args.database_url, args.revision)
        _print_json(result.to_dict())
        return

    if args.db_cmd == "materialization-status":
        from .materialization import TaskMaterializationWorker

        worker = TaskMaterializationWorker.from_url(
            args.database_url,
            runs_root=args.runs_root,
        )
        try:
            _print_json(worker.status(args.materialization_id).to_dict())
        finally:
            worker.close()
        return

    if args.db_cmd == "import-legacy-tasks":
        from .legacy_import import import_legacy_registry
        from .service import ExternalIdentity

        caller_issuer = args.caller_issuer or args.actor_issuer
        caller_subject = args.caller_subject or args.actor_subject
        engine = create_database_engine(args.database_url)
        try:
            with Session(engine) as session, session.begin():
                result = import_legacy_registry(
                    session,
                    registry_path=args.registry,
                    workspace_slug=args.workspace,
                    actor_identity=ExternalIdentity(args.actor_issuer, args.actor_subject),
                    caller_identity=ExternalIdentity(caller_issuer, caller_subject),
                    confirm=args.confirm,
                )
        finally:
            engine.dispose()
        _print_json(result)
        return

    if args.db_cmd == "materialize":
        from .materialization import TaskMaterializationWorker

        if args.drain_seconds is not None and args.materialization_id is None:
            raise SystemExit("--drain-seconds requires --materialization-id")
        if args.materialization_id is not None and not args.once and args.drain_seconds is None:
            raise SystemExit("--materialization-id requires --once or --drain-seconds")
        worker = TaskMaterializationWorker.from_url(
            args.database_url,
            runs_root=args.runs_root,
            tasks_root=args.tasks_root,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            retry_delay_seconds=args.retry_delay_seconds,
            max_attempts=args.max_attempts,
            poll_seconds=args.poll_seconds,
        )
        try:
            if args.drain_seconds is not None:
                _print_json(
                    worker.drain(
                        args.materialization_id,
                        timeout_seconds=args.drain_seconds,
                    ).to_dict(),
                )
            elif args.once:
                result = worker.run_once(args.materialization_id)
                payload = result.to_dict()
                if args.materialization_id is not None:
                    payload["status"] = worker.status(args.materialization_id).to_dict()
                _print_json(payload)
            else:
                worker.run_forever()
        finally:
            worker.close()
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
