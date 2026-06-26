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
        out = Path(args.output_dir) if args.output_dir else task.runs_dir / "samples" / Path(args.sample).parent.name
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

    elif args.cmd == "panel":
        from .panel import serve_panel

        serve_panel(args.runs_root, args.host, args.port, args.user, args.password, args.static_dir, args.tasks_root)


if __name__ == "__main__":
    main()
