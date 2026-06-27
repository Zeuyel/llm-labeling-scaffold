from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any


DEFAULT_PROFILE = "manual_labeling_cv_v1"
QUALITY_CONTROL_PROFILE = "manual_labeling_quality_control_v1"
PROFILE_ORDER = (DEFAULT_PROFILE, QUALITY_CONTROL_PROFILE)

STATUS_LABELS = {
    "not_started": "未开始",
    "ready": "可执行",
    "done": "已完成",
    "blocked": "受阻",
}

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    QUALITY_CONTROL_PROFILE: {
        "id": QUALITY_CONTROL_PROFILE,
        "name": "人工标注质量控制 v1",
        "description": "面向人工标注的一致性质量控制流程，使用试标校准、重叠样本、一致性检查和复核裁决来支撑主标注。",
        "quality_controls": {
            "annotators_per_item": 1,
            "overlap_rate": 0.2,
            "min_annotators_per_overlap_item": 2,
            "gold_rate": 0.0,
            "adjudication_required": True,
        },
        "stages": [
            {
                "id": "lake_import",
                "title": "数据导入",
                "name": "数据导入",
                "action": "import",
                "artifact_dir": "imports",
                "depends_on": [],
                "description": "从数据湖或本地上传形成可追溯导入资产。",
                "required_inputs": ["任务配置", "源对象清单"],
                "outputs": ["runs/<task_id>/imports/<import_id>/raw.jsonl", "manifest.json"],
                "action_hint": "先确认原始语料和字段映射，再生成本地导入资产。",
            },
            {
                "id": "sample",
                "title": "样本抽取",
                "name": "样本抽取",
                "action": "sample",
                "artifact_dir": "samples",
                "depends_on": ["lake_import"],
                "description": "从导入数据中抽取待标注样本。",
                "required_inputs": ["可用导入数据"],
                "outputs": ["runs/<task_id>/samples/<sample_id>/sample.jsonl", "manifest.json"],
                "action_hint": "创建样本后进入切分批次，生成带重叠样本的质量控制计划。",
            },
            {
                "id": "pilot_calibration",
                "title": "试标/校准",
                "name": "试标/校准",
                "action": "batch",
                "artifact_dir": "samples",
                "depends_on": ["sample"],
                "description": "先生成小规模重叠批次，用于标注员理解规则、发现歧义并校准指南。",
                "required_inputs": ["样本文件", "批次大小", "重叠比例"],
                "outputs": ["runs/<task_id>/samples/<sample_id>/batches/<plan_id>/manifest.json"],
                "action_hint": "使用 overlap_rate 和 min_annotators_per_overlap_item 创建试标批次计划。",
            },
            {
                "id": "consistency_check",
                "title": "一致性检查",
                "name": "一致性检查",
                "action": "agreement_audit",
                "artifact_dir": "agreement_audits",
                "depends_on": ["pilot_calibration"],
                "description": "基于重叠样本检查多人标注覆盖和一致性，确认标注指南是否可进入主标注。",
                "required_inputs": ["试标批次计划", "标注结果产物", "一致性阈值"],
                "outputs": ["runs/<task_id>/agreement_audits/<audit_id>/summary.json"],
                "action_hint": "检查重叠样本提交数和冲突情况，必要时返回校准。",
            },
            {
                "id": "main_annotation",
                "title": "主标注",
                "name": "主标注",
                "action": "argilla_push",
                "artifact_dir": "annotation_jobs",
                "depends_on": ["consistency_check"],
                "description": "将通过校准的批次计划或样本分发给标注员执行主标注。",
                "required_inputs": ["样本或批次计划", "Argilla 工作区"],
                "outputs": ["runs/<task_id>/annotation_jobs/<annotation_id>/manifest.json"],
                "action_hint": "主标注阶段继续保留重叠样本，用于后续一致性审计。",
            },
            {
                "id": "argilla_pull",
                "title": "标注回收",
                "name": "标注回收",
                "action": "argilla_pull",
                "artifact_dir": "decisions",
                "depends_on": ["main_annotation"],
                "description": "从 Argilla 拉取人工标注结果。",
                "required_inputs": ["已有提交的 Argilla 数据集"],
                "outputs": ["runs/<task_id>/decisions/<decision_id>/decisions.jsonl", "manifest.json"],
                "action_hint": "从主标注数据集拉回结果，作为复核裁决输入。",
            },
            {
                "id": "review_adjudication",
                "title": "复核裁决",
                "name": "复核裁决",
                "action": "agreement_audit",
                "artifact_dir": "agreement_audits",
                "depends_on": ["argilla_pull"],
                "description": "复核重叠样本分歧和低置信样本，对冲突项进行裁决后进入训练集构建。",
                "required_inputs": ["标注结果产物", "重叠样本清单", "裁决规则"],
                "outputs": ["runs/<task_id>/agreement_audits/<audit_id>/summary.json", "decisions.jsonl"],
                "action_hint": "审计一致性并记录裁决结果，未通过时暂缓训练集构建。",
            },
            {
                "id": "gold_build",
                "title": "训练集构建",
                "name": "训练集构建",
                "action": "gold",
                "artifact_dir": "gold",
                "depends_on": ["review_adjudication"],
                "description": "生成训练集数据和数据卡。",
                "required_inputs": ["通过复核裁决的标注结果", "样本清单"],
                "outputs": ["runs/<task_id>/gold/gold_<version>.jsonl", "gold_<version>.manifest.json"],
                "action_hint": "从通过质量检查的人工标注结果构建训练集版本。",
            },
            {
                "id": "train",
                "title": "模型训练",
                "name": "模型训练",
                "action": "train",
                "artifact_dir": "models",
                "depends_on": ["gold_build"],
                "description": "在训练集通过质量控制后训练模型版本。",
                "required_inputs": ["通过质量控制的训练集版本", "训练配置"],
                "outputs": ["runs/<task_id>/models/<model_id>/manifest.json", "metrics.json"],
                "action_hint": "选择质量控制通过后的训练集版本训练模型。",
            },
            {
                "id": "batch_infer",
                "title": "批量推理",
                "name": "批量推理",
                "action": "infer",
                "artifact_dir": "inference",
                "depends_on": ["train"],
                "description": "使用质量控制后训练得到的模型对语料执行批量推理。",
                "required_inputs": ["模型版本", "语料文件"],
                "outputs": ["runs/<task_id>/inference/<run_id>/predictions.jsonl"],
                "action_hint": "选择质量控制流程产出的模型版本和语料执行批量推理。",
            },
        ],
    },
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
                "description": "从 R2 数据湖或本地上传形成可追溯导入资产。",
                "required_inputs": ["任务配置", "数据湖登记表", "源对象清单"],
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
                "required_inputs": ["可用导入数据"],
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
                "required_inputs": ["可用样本", "Argilla 工作区"],
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
                "required_inputs": ["已有提交的 Argilla 数据集"],
                "outputs": ["runs/<task_id>/decisions/<decision_id>/decisions.jsonl", "manifest.json"],
                "action_hint": "进入标注分发页，从同一个 Argilla 数据集拉回结果。",
            },
            {
                "id": "agreement_audit",
                "title": "一致性检查",
                "name": "一致性检查",
                "action": "agreement_audit",
                "artifact_dir": "agreement_audits",
                "depends_on": ["argilla_pull"],
                "description": "检查标注一致性并确认可进入训练集构建。",
                "required_inputs": ["标注结果产物", "一致性和裁决规则"],
                "outputs": ["runs/<task_id>/agreement_audits/<audit_id>/summary.json"],
                "action_hint": "检查多人标注一致性、冲突裁决和最低提交数。",
            },
            {
                "id": "gold_build",
                "title": "训练集构建",
                "name": "训练集构建",
                "action": "gold",
                "artifact_dir": "gold",
                "depends_on": ["agreement_audit"],
                "description": "生成训练集数据和数据卡。",
                "required_inputs": ["通过质量检查的标注结果", "样本清单"],
                "outputs": ["runs/<task_id>/gold/gold_<version>.jsonl", "gold_<version>.manifest.json"],
                "action_hint": "进入训练集页，从标注结果构建训练集版本。",
            },
            {
                "id": "train",
                "title": "模型训练",
                "name": "模型训练",
                "action": "train",
                "artifact_dir": "models",
                "depends_on": ["gold_build"],
                "description": "基于训练集数据训练模型版本。",
                "required_inputs": ["训练集版本", "训练配置"],
                "outputs": ["runs/<task_id>/models/<model_id>/manifest.json", "metrics.json"],
                "action_hint": "进入模型页，选择训练集版本训练模型；交叉验证参数由训练器读取。",
            },
            {
                "id": "batch_infer",
                "title": "批量推理",
                "name": "批量推理",
                "action": "infer",
                "artifact_dir": "inference",
                "depends_on": ["train"],
                "description": "使用模型对语料执行批量推理。",
                "required_inputs": ["模型版本", "语料文件"],
                "outputs": ["runs/<task_id>/inference/<run_id>/predictions.jsonl"],
                "action_hint": "进入模型页，选择模型和语料执行批量推理。",
            },
        ],
    }
}


def _profile_meta(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in profile.items() if key != "stages"}


@lru_cache(maxsize=None)
def _cached_profile_definition(profile_id: str) -> dict[str, Any]:
    if profile_id not in BUILTIN_PROFILES:
        raise ValueError(f"未知流程预设: {profile_id}")
    return deepcopy(BUILTIN_PROFILES[profile_id])


@lru_cache(maxsize=1)
def _cached_profile_preset_catalog() -> tuple[dict[str, Any], ...]:
    ordered_ids = [profile_id for profile_id in PROFILE_ORDER if profile_id in BUILTIN_PROFILES]
    ordered_ids.extend(profile_id for profile_id in BUILTIN_PROFILES if profile_id not in ordered_ids)
    return tuple(_profile_meta(_cached_profile_definition(profile_id)) for profile_id in ordered_ids)


def list_profile_presets() -> list[dict[str, Any]]:
    return [deepcopy(item) for item in _cached_profile_preset_catalog()]


def profile_definition(profile_id: str | None = None) -> dict[str, Any]:
    resolved = str(profile_id or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    return deepcopy(_cached_profile_definition(resolved))


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)
