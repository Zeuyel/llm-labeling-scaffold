from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_PROFILE = "manual_labeling_cv_v1"

STATUS_LABELS = {
    "not_started": "未开始",
    "ready": "可执行",
    "done": "已完成",
    "blocked": "受阻",
}

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    DEFAULT_PROFILE: {
        "id": DEFAULT_PROFILE,
        "name": "人工标注闭环 v1",
        "description": "数据湖导入、抽样、Argilla 分发与回收、一致性检查、训练集构建、模型训练、批量推理的标准闭环。",
        "quality_controls": {
            "annotators_per_item": 2,
            "overlap_rate": 0.2,
            "adjudication_required": True,
            "cv_folds": 5,
        },
        "stages": [
            {
                "id": "lake_import",
                "title": "数据导入",
                "name": "数据导入",
                "action": "import",
                "artifact_dir": "imports",
                "depends_on": [],
                "description": "从 R2 data lake 或本地上传形成可追溯导入资产。",
                "required_inputs": ["task.yaml", "data_lake registry", "source manifest"],
                "outputs": ["runs/<task_id>/imports/<import_id>/raw.jsonl", "manifest.json"],
                "action_hint": "进入数据导入页，检查数据湖配置并生成本地导入。",
            },
            {
                "id": "sample",
                "title": "样本抽取",
                "name": "样本抽取",
                "action": "sample",
                "artifact_dir": "samples",
                "depends_on": ["lake_import"],
                "description": "从导入数据中抽取待标注样本。",
                "required_inputs": ["active import"],
                "outputs": ["runs/<task_id>/samples/<sample_id>/sample.jsonl", "manifest.json"],
                "action_hint": "进入样本管理页，从导入数据创建样本。",
            },
            {
                "id": "argilla_dispatch",
                "title": "标注分发",
                "name": "标注分发",
                "action": "argilla_push",
                "artifact_dir": "annotation_jobs",
                "depends_on": ["sample"],
                "description": "将样本推送到 Argilla 标注集。",
                "required_inputs": ["active sample", "Argilla workspace"],
                "outputs": ["runs/<task_id>/annotation_jobs/<annotation_id>/manifest.json"],
                "action_hint": "进入标注分发页，测试 Argilla 连接后推送样本。",
            },
            {
                "id": "argilla_pull",
                "title": "标注回收",
                "name": "标注回收",
                "action": "argilla_pull",
                "artifact_dir": "decisions",
                "depends_on": ["argilla_dispatch"],
                "description": "从 Argilla 拉取人工标注结果。",
                "required_inputs": ["Argilla dataset with submitted responses"],
                "outputs": ["runs/<task_id>/decisions/<decision_id>/decisions.jsonl", "manifest.json"],
                "action_hint": "进入标注分发页，从同一个 Argilla 数据集拉回结果。",
            },
            {
                "id": "agreement_audit",
                "title": "一致性检查",
                "name": "一致性检查",
                "action": "audit",
                "artifact_dir": "decisions",
                "depends_on": ["argilla_pull"],
                "description": "检查标注一致性并确认可进入 gold 构建。",
                "required_inputs": ["decision artifact", "overlap/adjudication policy"],
                "outputs": ["agreement summary", "adjudication decisions"],
                "action_hint": "检查多人标注一致性、冲突裁决和最低提交数。",
            },
            {
                "id": "gold_build",
                "title": "训练集构建",
                "name": "训练集构建",
                "action": "gold",
                "artifact_dir": "gold",
                "depends_on": ["agreement_audit"],
                "description": "生成 gold 数据集和数据卡。",
                "required_inputs": ["accepted decisions", "sample manifest"],
                "outputs": ["runs/<task_id>/gold/gold_<version>.jsonl", "gold_<version>.manifest.json"],
                "action_hint": "进入训练集页，从标注结果构建 gold 版本。",
            },
            {
                "id": "train",
                "title": "模型训练",
                "name": "模型训练",
                "action": "train",
                "artifact_dir": "models",
                "depends_on": ["gold_build"],
                "description": "基于 gold 数据训练模型版本。",
                "required_inputs": ["gold version", "trainer config"],
                "outputs": ["runs/<task_id>/models/<model_id>/manifest.json", "metrics.json"],
                "action_hint": "进入模型页，选择 gold 版本训练模型；交叉验证参数由 trainer 读取。",
            },
            {
                "id": "batch_infer",
                "title": "批量推理",
                "name": "批量推理",
                "action": "infer",
                "artifact_dir": "inference",
                "depends_on": ["train"],
                "description": "使用模型对语料执行批量推理。",
                "required_inputs": ["model version", "corpus JSONL"],
                "outputs": ["runs/<task_id>/inference/<run_id>/predictions.jsonl"],
                "action_hint": "进入模型页，选择模型和语料执行批量推理。",
            },
        ],
    }
}


def profile_definition(profile_id: str | None = None) -> dict[str, Any]:
    resolved = str(profile_id or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    if resolved not in BUILTIN_PROFILES:
        raise ValueError(f"未知 profile: {resolved}")
    return deepcopy(BUILTIN_PROFILES[resolved])


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)
