from __future__ import annotations

import argparse
import json
from pathlib import Path

from .annotation import annotate
from .audit import audit_run
from .batching import batch_records
from .config import load_task
from .gold import build_gold
from .merge import merge_run
from .sampling import sample_records
from .schema import write_output_schema


def _parse_params(values: list[str] | None) -> dict:
    out = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"--param must be key=value: {item}")
        key, value = item.split("=", 1)
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _load_task_reference(task_path: str | None, task_id: str | None, tasks_root: str):
    if task_path:
        return load_task(task_path)
    if task_id:
        from .pipeline import load_task_by_id

        return load_task_by_id(tasks_root, task_id)
    raise SystemExit("requires --task or --task-id")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lls")
    sub = p.add_subparsers(dest="cmd", required=True)

    schema = sub.add_parser("schema")
    schema_sub = schema.add_subparsers(dest="schema_cmd", required=True)
    schema_build = schema_sub.add_parser("build")
    schema_build.add_argument("--task", required=True)
    schema_build.add_argument("--output")

    sample = sub.add_parser("sample")
    sample.add_argument("--task", required=True)
    sample.add_argument("--rows", type=int, required=True)
    sample.add_argument("--sample-id", required=True)
    sample.add_argument("--strategy", default="random", choices=["random", "head"])
    sample.add_argument("--seed", type=int, default=20260617)
    sample.add_argument("--source")

    batch = sub.add_parser("batch")
    batch.add_argument("--task", required=True)
    batch.add_argument("--sample", required=True)
    batch.add_argument("--batch-size", type=int, required=True)
    batch.add_argument("--output-dir")

    ann = sub.add_parser("annotate")
    ann.add_argument("--task", required=True)
    ann.add_argument("--provider", default="local_stub")
    ann.add_argument("--run-id", required=True)
    ann.add_argument("--sample", required=True)
    ann.add_argument("--batch-size", type=int, default=100)
    ann.add_argument("--skip-existing", action="store_true")

    audit = sub.add_parser("audit")
    audit.add_argument("--task", required=True)
    audit.add_argument("--run", required=True)

    merge = sub.add_parser("merge")
    merge.add_argument("--task", required=True)
    merge.add_argument("--run", required=True)

    gold = sub.add_parser("gold")
    gold_sub = gold.add_subparsers(dest="gold_cmd", required=True)
    gold_build = gold_sub.add_parser("build")
    gold_build.add_argument("--task", required=True)
    gold_build.add_argument("--run")
    gold_build.add_argument("--version", required=True)
    gold_build.add_argument("--decisions")
    gold_build.add_argument("--sample")

    train = sub.add_parser("train")
    train.add_argument("--task", required=True)
    train.add_argument("--gold", required=True)
    train.add_argument("--model-id", required=True)
    train.add_argument("--trainer", default="tfidf_sgd")
    train.add_argument("--param", action="append", default=[])

    infer = sub.add_parser("infer")
    infer.add_argument("--task", required=True)
    infer.add_argument("--model", required=True)
    infer.add_argument("--corpus", required=True)
    infer.add_argument("--output", required=True)

    lake = sub.add_parser("data-lake")
    lake_sub = lake.add_subparsers(dest="lake_cmd", required=True)
    lake_check = lake_sub.add_parser("check")
    lake_check.add_argument("--task", required=True)
    lake_import = lake_sub.add_parser("import")
    lake_import.add_argument("--task", required=True)
    lake_import.add_argument("--runs-root", default="runs")
    lake_import.add_argument("--import-id")
    lake_import.add_argument("--source-object-path")
    lake_import.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    lake_export = lake_sub.add_parser("export")
    lake_export.add_argument("--task", required=True)
    lake_export.add_argument("--local", required=True)
    lake_export.add_argument("--target-uri")
    lake_export.add_argument("--target-path")
    lake_publish = lake_sub.add_parser("publish")
    lake_publish_sub = lake_publish.add_subparsers(dest="publish_cmd", required=True)
    lake_publish_plan = lake_publish_sub.add_parser("plan")
    lake_publish_plan.add_argument("--task", required=True)
    lake_publish_plan.add_argument("--runs-root", default="runs")
    lake_publish_plan.add_argument("--kind", required=True, choices=["decisions", "gold", "predictions", "model_metadata"])
    lake_publish_plan.add_argument("--artifact-id", required=True)
    lake_publish_submit = lake_publish_sub.add_parser("submit")
    lake_publish_submit.add_argument("--task", required=True)
    lake_publish_submit.add_argument("--runs-root", default="runs")
    lake_publish_submit.add_argument("--kind", required=True, choices=["decisions", "gold", "predictions", "model_metadata"])
    lake_publish_submit.add_argument("--artifact-id", required=True)
    lake_publish_submit.add_argument("--confirm", action="store_true")
    lake_publish_submit.add_argument("--idempotency-key", required=True)

    task_cmd = sub.add_parser("task")
    task_sub = task_cmd.add_subparsers(dest="task_cmd", required=True)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--tasks-root", default="tasks")
    task_status = task_sub.add_parser("status")
    task_status.add_argument("--task")
    task_status.add_argument("--task-id")
    task_status.add_argument("--tasks-root", default="tasks")
    task_status.add_argument("--runs-root", default="runs")

    imports = sub.add_parser("import")
    import_sub = imports.add_subparsers(dest="import_cmd", required=True)
    import_list = import_sub.add_parser("list")
    import_list.add_argument("--task")
    import_list.add_argument("--task-id")
    import_list.add_argument("--tasks-root", default="tasks")
    import_list.add_argument("--runs-root", default="runs")
    import_detail = import_sub.add_parser("detail")
    import_detail.add_argument("--task")
    import_detail.add_argument("--task-id")
    import_detail.add_argument("--tasks-root", default="tasks")
    import_detail.add_argument("--runs-root", default="runs")
    import_detail.add_argument("--import-id", required=True)

    smoke = sub.add_parser("smoke")
    smoke.add_argument("--server-url")
    smoke.add_argument("--token")
    smoke.add_argument("--token-env", default="LLS_SMOKE_TOKEN")
    smoke.add_argument("--basic-user")
    smoke.add_argument("--basic-password")
    smoke.add_argument("--basic-password-env", default="LLS_SMOKE_BASIC_PASSWORD")
    smoke.add_argument("--task-id", default="patent_boundary_v0_1")
    smoke.add_argument("--import-id", default="patent_boundary_manual_seed_500_2026_06_27")
    smoke.add_argument("--timeout", type=float, default=10.0)
    smoke.add_argument("--format", choices=["json", "markdown"], default="json")

    panel = sub.add_parser("panel")
    panel.add_argument("--runs-root", default="runs")
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=8765)
    panel.add_argument("--user", default="admin")
    panel.add_argument("--password")
    panel.add_argument("--static-dir")
    panel.add_argument("--tasks-root", default="tasks")

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "schema":
        task = load_task(args.task)
        print(write_output_schema(task, args.output))
    elif args.cmd == "sample":
        task = load_task(args.task)
        print(sample_records(task, args.rows, args.sample_id, args.strategy, args.seed, args.source))
    elif args.cmd == "batch":
        task = load_task(args.task)
        sample_id = Path(args.sample).parent.name
        out = Path(args.output_dir) if args.output_dir else task.runs_dir / "samples" / sample_id / "batches" / f"size_{args.batch_size}"
        print("\n".join(str(p) for p in batch_records(args.sample, out, args.batch_size)))
    elif args.cmd == "annotate":
        task = load_task(args.task)
        print(annotate(task, args.sample, args.run_id, args.provider, args.batch_size, args.skip_existing))
    elif args.cmd == "audit":
        task = load_task(args.task)
        print(audit_run(task, args.run))
    elif args.cmd == "merge":
        task = load_task(args.task)
        print(merge_run(task, args.run))
    elif args.cmd == "gold":
        task = load_task(args.task)
        if args.sample and args.decisions:
            from .gold import build_gold_from_decisions

            print(build_gold_from_decisions(task, args.sample, args.decisions, args.version))
        else:
            if not args.run:
                raise SystemExit("gold build requires --run, or --sample with --decisions")
            print(build_gold(task, args.run, args.version, args.decisions))
    elif args.cmd == "train":
        from .train import train_model

        task = load_task(args.task)
        print(train_model(task, args.gold, args.model_id, args.trainer, _parse_params(args.param)))
    elif args.cmd == "infer":
        from .infer import infer_jsonl

        task = load_task(args.task)
        print(infer_jsonl(task, args.model, args.corpus, args.output))
    elif args.cmd == "data-lake":
        task = load_task(args.task)
        if args.lake_cmd == "check":
            from .data_lake import preview_source

            _print_json(preview_source(task))
        elif args.lake_cmd == "import":
            from .pipeline import import_from_data_lake

            overrides = {
                "source_object_path": args.source_object_path,
            }
            _print_json(
                import_from_data_lake(args.runs_root, task, import_id=args.import_id, overrides=overrides, max_bytes=args.max_bytes),
            )
        elif args.lake_cmd == "export":
            from .data_lake import export_artifact

            _print_json(
                export_artifact(task, args.local, target_uri=args.target_uri, target_path=args.target_path),
            )
        elif args.lake_cmd == "publish":
            if args.publish_cmd == "plan":
                from .data_lake import plan_artifact_publish

                _print_json(
                    plan_artifact_publish(task, args.runs_root, args.kind, args.artifact_id),
                )
            elif args.publish_cmd == "submit":
                from .data_lake import submit_artifact_publish

                _print_json(
                    submit_artifact_publish(
                        task,
                        args.runs_root,
                        args.kind,
                        args.artifact_id,
                        confirm=args.confirm,
                        idempotency_key=args.idempotency_key,
                    ),
                )

    elif args.cmd == "task":
        if args.task_cmd == "list":
            from .pipeline import list_tasks

            _print_json({"tasks": list_tasks(args.tasks_root)})
        elif args.task_cmd == "status":
            from .pipeline import task_profile_status

            task = _load_task_reference(args.task, args.task_id, args.tasks_root)
            _print_json(task_profile_status(args.runs_root, task))

    elif args.cmd == "import":
        from .pipeline import import_detail, list_imports

        task = _load_task_reference(args.task, args.task_id, args.tasks_root)
        if args.import_cmd == "list":
            _print_json({"imports": list_imports(args.runs_root, task.task_id, id_field=task.id_field)})
        elif args.import_cmd == "detail":
            _print_json({"import": import_detail(args.runs_root, task.task_id, args.import_id, id_field=task.id_field)})

    elif args.cmd == "smoke":
        from .smoke import config_from_env, render_summary, run_smoke

        config = config_from_env(
            server_url=args.server_url,
            token=args.token,
            token_env=args.token_env,
            basic_user=args.basic_user,
            basic_password=args.basic_password,
            basic_password_env=args.basic_password_env,
            task_id=args.task_id,
            import_id=args.import_id,
            timeout=args.timeout,
        )
        summary = run_smoke(config)
        print(render_summary(summary, args.format))
        if not summary.get("ok"):
            raise SystemExit(1)

    elif args.cmd == "panel":
        from .panel import serve_panel

        serve_panel(args.runs_root, args.host, args.port, args.user, args.password, args.static_dir, args.tasks_root)


if __name__ == "__main__":
    main()
