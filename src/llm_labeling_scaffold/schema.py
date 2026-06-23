from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import TaskConfig
from .io import write_json


def _field_schema(label: dict[str, Any]) -> dict[str, Any]:
    typ = label.get("type", "string")
    out: dict[str, Any]
    if typ == "categorical":
        out = {"type": "string", "enum": list(label["values"])}
    elif typ == "integer":
        out = {"type": "integer"}
        if "values" in label:
            out["enum"] = list(label["values"])
        if "min" in label:
            out["minimum"] = int(label["min"])
        if "max" in label:
            out["maximum"] = int(label["max"])
    elif typ == "number":
        out = {"type": "number"}
    elif typ == "boolean":
        out = {"type": "boolean"}
    else:
        out = {"type": "string"}
    return out


def build_output_schema(task: TaskConfig) -> dict[str, Any]:
    properties: dict[str, Any] = {
        task.id_field: {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
        "evidence": {"type": "string"},
    }
    required = [task.id_field]
    primary = task.primary_label
    properties[primary["name"]] = _field_schema(primary)
    required.append(primary["name"])
    for label in task.auxiliary_labels:
        properties[label["name"]] = _field_schema(label)
        if label.get("required", True):
            required.append(label["name"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": required,
                    "properties": properties,
                    "additionalProperties": True,
                },
            }
        },
        "additionalProperties": True,
    }


def write_output_schema(task: TaskConfig, output: str | Path | None = None) -> Path:
    path = Path(output) if output else task.runs_dir / "schemas" / "label_output.schema.json"
    write_json(build_output_schema(task), path)
    return path


def validate_row_light(row: dict, item_schema: dict) -> list[str]:
    """Small dependency-free validator for required fields and enum/type checks."""
    errors: list[str] = []
    required = item_schema.get("required", [])
    props = item_schema.get("properties", {})
    for field in required:
        if field not in row:
            errors.append(f"missing required field: {field}")
    for field, spec in props.items():
        if field not in row:
            continue
        value = row[field]
        typ = spec.get("type")
        if typ == "string" and not isinstance(value, str):
            errors.append(f"{field} must be string")
        if typ == "integer" and not isinstance(value, int):
            errors.append(f"{field} must be integer")
        if typ == "number" and not isinstance(value, (int, float)):
            errors.append(f"{field} must be number")
        if typ == "boolean" and not isinstance(value, bool):
            errors.append(f"{field} must be boolean")
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"{field} has illegal value: {value}")
        if "minimum" in spec and isinstance(value, (int, float)) and value < spec["minimum"]:
            errors.append(f"{field} below minimum: {value}")
        if "maximum" in spec and isinstance(value, (int, float)) and value > spec["maximum"]:
            errors.append(f"{field} above maximum: {value}")
    return errors
