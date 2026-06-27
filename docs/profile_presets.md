# Profile 预设

`profile` 是任务的执行模板，不是备注字段。它把控制台中的“数据导入 -> 样本抽取 -> 标注分发 -> 标注回收 -> 一致性检查 -> 训练集构建 -> 模型训练 -> 批量推理”固化成可重复执行的阶段、默认参数和质量门槛。

## 术语

- `task.yaml`：单个标注任务的业务配置。它定义任务编号、输入字段、标签体系、约束、标注说明、数据湖来源和要使用的 `profile`。
- `profile`：任务绑定的执行模板。面板按它生成阶段计划、默认编号、默认参数和质量控制点。
- `profile preset`：可复用的 profile 名称，例如 `manual_labeling_cv_v1`。任务通常只引用 preset，必要时再写少量覆盖参数。
- `profile registry`：平台可加载的 profile 预设集合。它和数据湖 registry 不是同一个概念；前者描述执行流程，后者登记数据集和 manifest。
- `data lake registry`：数据湖治理登记表，记录数据集编号、层级、领域和 manifest URI。
- `manifest`：资产清单。导入、样本、标注分发、标注结果、训练集、模型和推理结果都要写 manifest，用于记录来源、路径、行数、哈希、时间和状态。
- `import`：任务级导入数据，落到 `runs/<task_id>/imports/<import_id>/raw.jsonl`。来自 R2 数据湖时，它只是执行缓存，权威来源仍是数据湖 manifest。
- `sample`：从 import 或任务输入抽取出的待标注样本，落到 `runs/<task_id>/samples/<sample_id>/sample.jsonl`。
- `annotation_job`：一次推送到 Argilla 的分发记录，记录数据集名、样本来源、行数和分发状态。
- `decision_artifact`：从 Argilla 拉回的人工标注结果，落到 `runs/<task_id>/decisions/<decision_id>/decisions.jsonl`。
- `gold` / `gold_version`：由样本和人工标注结果构建出的训练集版本，落到 `runs/<task_id>/gold/gold_<version>.jsonl`。
- `training_job`：控制台创建的训练请求。正式训练可由远程训练服务器执行，本地 `tfidf_sgd` 只作为基线和调试能力。
- `model_version`：训练输出的模型目录和指标清单。
- `inference_run`：模型对 JSONL 语料执行批量推理后的结果目录。

## 执行契约

面板加载 `task.yaml` 后，先解析 `profile.preset`，再按 preset 展开阶段计划。阶段之间只通过 manifest 和明确的产物路径传递，不靠人工记忆。

默认执行顺序：

1. `import`：上传、粘贴或从数据湖 materialize 任务级 JSONL。
2. `sample`：从导入数据创建样本，写入样本 manifest。
3. `distribute`：把样本推送到 Argilla，写入 `annotation_job` manifest。
4. `collect`：从 Argilla 拉回标注结果，写入 `decision_artifact` manifest。
5. `quality`：检查覆盖率、缺失 ID、重复 ID、schema、标签约束和最低提交数。
6. `gold`：质量门槛通过后构建训练集版本。
7. `train`：用指定训练器和参数创建训练任务或执行基线训练。
8. `inference`：用模型对指定 corpus 执行批量推理。

生产环境中，profile 不能覆盖数据湖权威字段。`lake_registry_uri`、`source_dataset_id`、`source_manifest_uri`、`source_object_path` 仍由 `task.yaml` 的 `data_lake` 配置决定；只有开发排查时才允许用 `LLS_ALLOW_DATA_LAKE_OVERRIDES=1` 临时覆盖。

## `task.yaml` 引用方式

任务文件只需要引用 preset：

```yaml
profile:
  preset: manual_labeling_cv_v1
```

如果确实需要覆盖默认参数，应只覆盖执行参数，不覆盖数据湖权威路径：

```yaml
profile:
  preset: manual_labeling_cv_v1
  overrides:
    sample:
      rows: 500
      sample_id: manual_seed_500_v001
    train:
      model_id: baseline_v001
```

## `manual_labeling_cv_v1` 示例

这个 preset 面向“人工标注 + 基线验证 + 批量推理”的闭环。它把现有流水线动作串成一个可执行计划：先得到任务级导入，再抽样并分发给 Argilla，拉回人工结果后做质量检查，构建 gold，最后训练基线模型并执行推理。

```yaml
manual_labeling_cv_v1:
  version: 1
  stages:
    import:
      enabled: true
      source: data_lake
      import_id: "{data_lake.default_import_id}"
      checks:
        id_field_present: true
        unique_ids: true
        text_fields_non_empty: true
        content_sha256: required

    sample:
      enabled: true
      source_import_id: "{import.import_id}"
      sample_id: manual_seed_v001
      rows: 500
      strategy: head
      seed: 20260617
      checks:
        no_overwrite: true
        write_manifest: true
        source_import_link: required

    distribute:
      enabled: true
      provider: argilla
      annotation_id: "{task_id}_{sample.sample_id}_argilla_v001"
      dataset: "{task_id}_{sample.sample_id}_v001"
      include_labels:
        - labels.primary
        - labels.auxiliary
      checks:
        rows_match_sample: true
        write_manifest: true

    collect:
      enabled: true
      provider: argilla
      decision_id: "{distribute.annotation_id}_decisions_v001"
      min_submitted: "{annotation.min_submitted}"
      checks:
        require_known_ids: true
        require_primary_label: true
        write_manifest: true

    quality:
      enabled: true
      gates:
        missing_ids: 0
        duplicate_ids: 0
        schema_errors: 0
        constraint_errors: 0
        min_submitted: "{annotation.min_submitted}"
        require_all_sample_ids_decided: true
      on_failure: block_gold

    gold:
      enabled: true
      version: v001
      source: decision_artifact
      checks:
        rows_gt_zero: true
        label_counts_required: true
        write_data_card: true

    train:
      enabled: true
      training_job_id: train_baseline_v001
      model_id: baseline_v001
      trainer: tfidf_sgd
      trainer_params:
        seed: 20260617
        test_size: 0.25
        ngram_min: 2
        ngram_max: 4
        max_features: 180000
      checks:
        gold_manifest_required: true
        write_model_manifest: true
        write_metrics: true

    inference:
      enabled: true
      inference_id: baseline_v001_import_predictions
      model: "{train.model_path}"
      corpus: "{import.path}"
      output: "runs/{task_id}/inference/{inference.inference_id}"
      checks:
        model_manifest_required: true
        corpus_jsonl_required: true
        write_predictions: true
```

说明：

- `import.source=data_lake` 表示优先按 `task.yaml` 中的 `data_lake` 配置生成导入；没有 `data_lake` 的任务可由面板上传或粘贴生成 import。
- `sample.strategy=head` 适合数据湖上游已经完成抽样的人工种子集；若要由 scaffold 抽样，可覆盖为 `random` 并设置 `seed`。
- `distribute.include_labels` 要求 Argilla 同步主标签和辅助标签，避免训练集只保留主标签。
- `quality.on_failure=block_gold` 表示质量门槛未过时不能构建 `gold`。
- `train.trainer_params` 对齐当前内置 `tfidf_sgd` 训练器。若后续训练器支持 k-fold CV，可在该训练器自己的参数中扩展，不改变 profile 阶段契约。
- `inference.corpus={import.path}` 表示默认对本次任务导入执行批量推理；正式生产可改为另一个已登记的 JSONL corpus。
