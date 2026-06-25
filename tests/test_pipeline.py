from pathlib import Path

from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold import pipeline


def test_create_task_writes_custom_task_with_auxiliary_labels(tmp_path: Path):
    task = pipeline.create_task(
        tmp_path,
        {
            "task_id": "patent_boundary_demo",
            "id_field": "patent_id",
            "text_fields": ["patent_title", "patent_abstract"],
            "metadata_fields": "firm_name, application_year",
            "primary_label_name": "innovation_boundary_label",
            "primary_label_values": ["new_product_or_application", "unclear_or_insufficient"],
            "auxiliary_labels": [
                {"name": "new_product_application_flag", "type": "integer", "values": ["0", "1"]},
                {"name": "reason", "type": "string"},
                {"name": "confidence", "type": "integer", "min": "0", "max": "100"},
                {"name": "evidence_product_application", "type": "string", "required": False},
            ],
        },
    )

    created = load_task(task["path"])

    assert created.task_id == "patent_boundary_demo"
    assert created.id_field == "patent_id"
    assert created.text_fields == ["patent_title", "patent_abstract"]
    assert created.metadata_fields == ["firm_name", "application_year"]
    assert created.primary_label["name"] == "innovation_boundary_label"
    assert [item["name"] for item in created.auxiliary_labels] == [
        "new_product_application_flag",
        "reason",
        "confidence",
        "evidence_product_application",
    ]
    assert created.auxiliary_labels[0]["values"] == [0, 1]
    assert created.auxiliary_labels[2]["min"] == 0
    assert created.auxiliary_labels[3]["required"] is False


def test_list_tasks_reads_multiple_roots_and_deduplicates(tmp_path: Path):
    root_a = tmp_path / "examples"
    root_b = tmp_path / "tasks"
    pipeline.create_task(
        root_a,
        {
            "task_id": "task_a",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    pipeline.create_task(
        root_b,
        {
            "task_id": "task_a",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )

    tasks = pipeline.list_tasks(f"{root_a},{root_b}")

    assert [task["task_id"] for task in tasks] == ["task_a"]
