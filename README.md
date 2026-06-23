# llm-labeling-scaffold

A task-agnostic scaffold for building reproducible text-labeling pipelines:

1. define a task schema,
2. sample records,
3. batch records for LLM annotation,
4. audit and merge structured outputs,
5. apply human adjudication patches,
6. build a versioned gold set,
7. train a local deployable classifier,
8. infer labels on the full corpus,
9. write auditable reports.

The repo is intentionally not tied to any paper, data source, or label taxonomy.
Downstream projects should keep their real data and task-specific outputs outside this repo or under `runs/`, which is git-ignored except for `.gitkeep`.

## Quickstart

The default entrypoint is the built-in Docker stack. It builds the Vite frontend into the panel backend image and runs the panel, Argilla, MLflow, PostgreSQL, Elasticsearch, and Redis together.

```bash
./scripts/stack up
```

Open:

- Pipeline control panel: `http://localhost:8765` (`admin` / `changeme`)
- Argilla UI: `http://localhost:6900` (`argilla` / `12345678`)
- MLflow UI/API: `http://localhost:5000`

Useful stack commands:

```bash
./scripts/stack logs
./scripts/stack ps
./scripts/stack restart
./scripts/stack down
```

The panel and API are served from the same `8765` port. Vite's `5173` port is only used if you explicitly run frontend development mode.

For `toy_multiclass_v1`, use the panel flow:

1. 在「采样 / Artifact」创建 sample artifact。
2. 在「标注运行 Run」推送 sample 到 Argilla。
3. 在 Argilla 完成人工标注或复核。
4. 回到 panel 拉取 Argilla responses，生成 decision artifact。
5. 在 run detail 审核 missing / duplicate / conflict / merged pools。
6. 在「Gold 版本」构建 `gold_version`。
7. 在「模型版本」选择 trainer 并训练，训练记录写入 MLflow。

## Local CLI development

The following commands are for local development and debugging, not the normal deployment path:

```bash
python -m llm_labeling_scaffold.cli schema build --task examples/toy_text_classification/task.yaml
python -m llm_labeling_scaffold.cli sample --task examples/toy_text_classification/task.yaml --rows 12 --sample-id toy_seed
python -m llm_labeling_scaffold.cli batch --task examples/toy_text_classification/task.yaml --sample runs/toy_multiclass_v1/samples/toy_seed/sample.jsonl --batch-size 5
python -m llm_labeling_scaffold.cli annotate --task examples/toy_text_classification/task.yaml --provider local_stub --run-id demo --sample runs/toy_multiclass_v1/samples/toy_seed/sample.jsonl --batch-size 5 --skip-existing
python -m llm_labeling_scaffold.cli audit --task examples/toy_text_classification/task.yaml --run runs/toy_multiclass_v1/demo
python -m llm_labeling_scaffold.cli merge --task examples/toy_text_classification/task.yaml --run runs/toy_multiclass_v1/demo
python -m llm_labeling_scaffold.cli gold build --task examples/toy_text_classification/task.yaml --run runs/toy_multiclass_v1/demo --version v001
python -m llm_labeling_scaffold.cli train --task examples/toy_text_classification/task.yaml --gold runs/toy_multiclass_v1/gold/gold_v001.jsonl --model-id baseline_v001 --trainer tfidf_sgd
python -m llm_labeling_scaffold.cli infer --task examples/toy_text_classification/task.yaml --model runs/toy_multiclass_v1/models/baseline_v001/model.joblib --corpus examples/toy_text_classification/raw/sample.jsonl --output runs/toy_multiclass_v1/inference/baseline_v001
```

The `tfidf_sgd` trainer is a built-in baseline plugin, not part of the core labeling flow. Install its optional dependencies before running that trainer:

```bash
pip install -e ".[baseline]"
```

Custom trainers can be referenced as `module:function` and receive `(task, gold_path, model_id, params)`.

Optional Argilla and MLflow integrations are kept outside the core dependencies:

```bash
pip install -e ".[argilla,mlflow]"
export ARGILLA_API_URL=http://localhost:6900
export ARGILLA_API_KEY=argilla.apikey
export ARGILLA_WORKSPACE=argilla
export MLFLOW_TRACKING_URI=http://localhost:5000
```

Argilla is the annotation workspace: push a sample artifact to an Argilla dataset, label or review in Argilla, then pull responses back as a decisions JSONL artifact. MLflow is the model tracking workspace: training jobs can log params, metrics, artifacts, and the resulting MLflow run id into the local model manifest.

### Docker stack details

The panel container mounts the repository's `runs/`, `examples/`, and `configs/` directories, so generated artifacts stay on the host filesystem. Inside Docker, the panel reaches Argilla at `http://argilla:6900` and MLflow at `http://mlflow:5000`.

If you run the panel or CLI on the host instead of Docker, load the local integration environment:

```bash
set -a
. configs/integrations.env.example
set +a
```

## Pipeline control panel

The panel has two parts: a stdlib JSON API (backend) and a Vite + React frontend. In Docker, the frontend is built into the panel image and served from the same `8765` port. The domain model is organized around `task`, `artifact`, `run`, `job`, `decision`, `gold_version`, and `model_version`.

### Backend API (authenticated)

The normal entrypoint is `docker compose up -d`; the direct Python command is only for local development.

- HTTP Basic auth (user/password). If no password is given, one is generated and printed at startup; `LLS_PANEL_PASSWORD` is also honored.
- Read: `GET /api/runs`, `GET /api/run?task=&run=`, `GET /api/rows?task=&run=&kind=merged|missing|duplicate|conflict`, `GET /api/gold?task=`.
- Export: `GET /api/export?task=&run=&kind=` streams a `.jsonl` download.
- Write: `POST /api/adjudicate?task=&run=` appends a human decision to `<run>/adjudication/decisions.jsonl`; `POST /api/import?task=&name=` saves uploaded JSONL to `<task>/imports/<name>/raw.jsonl`; `POST /api/action` starts pipeline jobs including Argilla push/pull, gold build, and training.
- All run/task path segments are validated against `..` and separators. Binds to `127.0.0.1` by default; use `--host` to expose.

The adjudication file is the `--decisions` input for `gold build`, so panel review feeds straight back into the gold set.

### Frontend development

```bash
cd frontend
npm install
npm run dev      # dev server with /api proxied to http://127.0.0.1:8765
npm run build    # outputs frontend/dist, auto-served by the panel backend
```

The UI is split by task and workflow page: task overview, sampling/artifacts, annotation runs, run detail with pools and decisions, jobs, gold versions, and model versions. It uses a restrained shadcn-style layout with a left navigation rail, compact cards, tabs, badges, and dense tables.

## Current MVP

Implemented now:

- task YAML loading,
- JSON Schema generation,
- deterministic sampling and batching,
- `local_stub` annotation provider,
- schema/ID/constraint audit,
- safe merge with missing/duplicate/conflict pools,
- adjudication patch application,
- versioned gold set build,
- modular training registry with a TF-IDF + SGD baseline trainer,
- JSONL full-corpus inference,
- basic Markdown/JSON summaries.

Planned next:

- `codex_exec` provider,
- OpenAI Responses API provider,
- parquet streaming inference,
- configurable aggregation,
- richer active-learning sampling.
