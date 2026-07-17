"""Allocation-only Argilla 2.8 dispatch and collection adapter.

The adapter accepts a sealed allocation snapshot. It never reads a sample path,
reconstructs assignments, or creates a remote workspace/dataset.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping
from uuid import UUID

from .db.allocation_repository import (
    AllocationNaturalKeyConflict,
    AllocationNotReady,
    AllocationReceiptRequest,
    AllocationRepositoryError,
    AllocationRepositoryValidationError,
    ReceiptRejectionReason,
)
from .db.enums import AuditChannel, CollectionDisposition
from .integrations.argilla import (
    _api_url,
    _client,
    _load_argilla,
    _record_metadata_dict,
    _require_argilla_2_8_version,
    _runtime_versions,
    _settings_fingerprint,
    _settings_intent_fingerprint,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_RECORD_FINGERPRINT = "__lls_allocation_record_fingerprint"
_PLAN_FINGERPRINT = "__lls_allocation_plan_fingerprint"
_ITEM_ID = "__lls_allocation_item_id"
_DATASET_ID = "__lls_allocation_dataset_id"
_BATCH_ID = "__lls_allocation_batch_id"
_CONTENT_HASH = "__lls_allocation_content_hash"
_ASSIGNMENT_IDS = "__lls_allocation_assignment_ids"
_SOURCE_RECORD_ID = "__lls_allocation_source_record_id"


class AllocationAdapterError(RuntimeError):
    """Base error for an allocation adapter contract failure."""


class AllocationInputError(ValueError, AllocationAdapterError):
    """Raised when the trusted allocation snapshot is incomplete or invalid."""


class RemoteBindingError(AllocationAdapterError):
    """Raised when a remote resource does not match its trusted binding."""


@dataclass(frozen=True, slots=True)
class ConfirmedAllocationPlan:
    plan_id: UUID
    fingerprint: str
    connection_binding_id: UUID
    lifecycle_state: str = "confirmed"


@dataclass(frozen=True, slots=True)
class AllocationWorkspaceBinding:
    workspace_id: UUID
    name: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationDatasetBinding:
    dataset_group_id: UUID
    workspace_id: UUID
    dataset_id: UUID
    min_submitted: int
    name: str | None = None
    settings_fingerprint: str | None = None
    intent_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationRecordBinding:
    item_id: UUID
    dataset_group_id: UUID
    dataset_id: UUID
    record_id: UUID


@dataclass(frozen=True, slots=True)
class AllocationAssignmentItem:
    item_id: UUID
    dataset_group_id: UUID
    source_record_id: str
    batch_id: str
    content_hash: str
    fields: Mapping[str, Any]
    remote: AllocationRecordBinding


@dataclass(frozen=True, slots=True)
class AllocationAssignment:
    assignment_id: UUID
    item_id: UUID
    respondent_ids: tuple[UUID, ...]
    role: str = "primary"
    required_submissions: int = 1
    pool_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AllocationRuntimeContract:
    schema_version: int = 1
    sdk_version: str = "2.8.0"
    server_version: str = "2.8.0"
    settings_fingerprint: str | None = None
    intent_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ArgillaAllocationSnapshot:
    plan: ConfirmedAllocationPlan
    workspace: AllocationWorkspaceBinding
    datasets: tuple[AllocationDatasetBinding, ...]
    items: tuple[AllocationAssignmentItem, ...]
    assignments: tuple[AllocationAssignment, ...]
    required_question_names: tuple[str, ...] = ()
    contract: AllocationRuntimeContract | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    plan_id: UUID
    plan_fingerprint: str
    records_logged: int
    records_existing: int
    batches: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": str(self.plan_id),
            "plan_fingerprint": self.plan_fingerprint,
            "records_logged": self.records_logged,
            "records_existing": self.records_existing,
            "batches": [{"batch": batch, "records": count} for batch, count in self.batches],
        }


@dataclass(frozen=True, slots=True)
class AcceptedReceipt:
    item_id: UUID
    assignment_id: UUID
    response_id: UUID
    receipt_id: UUID | None
    replayed: bool
    labels_hash: str


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    reason: str
    reason_codes: tuple[str, ...]
    dataset_id: str | None
    record_id: str | None
    item_id: UUID | None
    response_id: UUID | None
    respondent_id: UUID | None
    response_status: str
    question_names: tuple[str, ...] = ()
    observed_statuses: tuple[str, ...] = ()
    labels_hash: str | None = None
    receipt_id: UUID | None = None
    receipt_replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "reason_codes": list(self.reason_codes),
            "dataset_id": self.dataset_id,
            "record_id": self.record_id,
            "item_id": str(self.item_id) if self.item_id else None,
            "response_id": str(self.response_id) if self.response_id else None,
            "respondent_id": str(self.respondent_id) if self.respondent_id else None,
            "response_status": self.response_status,
            "question_names": list(self.question_names),
            "observed_statuses": list(self.observed_statuses),
            "labels_hash": self.labels_hash,
            "receipt_id": str(self.receipt_id) if self.receipt_id else None,
            "receipt_replayed": self.receipt_replayed,
        }


@dataclass(frozen=True, slots=True)
class CollectResult:
    plan_id: UUID
    accepted: tuple[AcceptedReceipt, ...]
    quarantine: tuple[QuarantineEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": str(self.plan_id),
            "accepted": [
                {
                    "item_id": str(item.item_id),
                    "assignment_id": str(item.assignment_id),
                    "response_id": str(item.response_id),
                    "receipt_id": str(item.receipt_id) if item.receipt_id else None,
                    "replayed": item.replayed,
                    "labels_hash": item.labels_hash,
                }
                for item in self.accepted
            ],
            "quarantine": [item.to_dict() for item in self.quarantine],
        }


def _uuid(value: Any, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AllocationInputError(f"{field} must be a UUID") from exc


def _optional_uuid(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return {"type": type(value).__name__}


def _stable_hash(value: Any) -> str:
    payload = json.dumps(_canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _response_status(value: Any) -> str:
    return _enum_value(value) or "unknown"


def _response_time(value: Any) -> datetime | None:
    for name in ("submitted_at", "updated_at", "inserted_at", "created_at"):
        candidate = getattr(value, name, None)
        if candidate is None:
            continue
        if isinstance(candidate, datetime):
            return candidate.astimezone(timezone.utc) if candidate.tzinfo else candidate.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _response_id(value: Any) -> tuple[UUID | None, bool]:
    found = False
    for name in ("id", "response_id", "uuid"):
        candidate = getattr(value, name, None)
        if candidate in (None, ""):
            continue
        found = True
        parsed = _optional_uuid(candidate)
        return parsed, parsed is not None
    return None, not found


def _response_user_id(value: Any) -> UUID | None:
    return _optional_uuid(getattr(value, "user_id", None))


def _response_groups(record: Any) -> list[dict[str, Any]]:
    responses = getattr(record, "responses", None)
    if responses is None:
        return []
    try:
        iterator = iter(responses)
    except TypeError as exc:
        raise AllocationAdapterError("Argilla 2.8 Record.responses must be a public iterable") from exc

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for response in iterator:
        user_id = _response_user_id(response)
        raw_user_id = getattr(response, "user_id", None)
        response_id, response_id_valid = _response_id(response)
        response_key = str(response_id) if response_id else _stable_hash({"user": str(raw_user_id), "status": _response_status(getattr(response, "status", None))})
        user_key = str(user_id) if user_id else _stable_hash({"user": str(raw_user_id)})
        group = groups.setdefault(
            (response_key, user_key),
            {
                "response_ids": set(),
                "response_id_missing": False,
                "response_id_invalid": False,
                "respondent_id": user_id,
                "statuses": set(),
                "observed_statuses": set(),
                "values": {},
                "duplicate_questions": set(),
                "question_names": set(),
                "times": [],
            },
        )
        if response_id:
            group["response_ids"].add(response_id)
        elif response_id_valid:
            group["response_id_missing"] = True
        else:
            group["response_id_invalid"] = True
        question_name = str(getattr(response, "question_name", "") or "").strip()
        if not question_name:
            group["schema_error"] = True
        if question_name in group["values"]:
            group["duplicate_questions"].add(question_name)
        group["question_names"].add(question_name)
        group["values"][question_name] = getattr(response, "value", None)
        status = _response_status(getattr(response, "status", None))
        group["statuses"].add(status if status in {"submitted", "draft", "discarded"} else "unknown")
        group["observed_statuses"].add(status)
        timestamp = _response_time(response)
        if timestamp is not None:
            group["times"].append(timestamp)

    output: list[dict[str, Any]] = []
    for group in groups.values():
        statuses = sorted(group["statuses"])
        output.append(
            {
                **group,
                "status": statuses[0] if len(statuses) == 1 else "mixed",
                "observed_statuses": tuple(sorted(group["observed_statuses"])),
                "question_names": tuple(sorted(group["question_names"])),
                "response_id": next(iter(group["response_ids"])) if len(group["response_ids"]) == 1 else None,
                "timestamp": max(group["times"], default=_EPOCH),
            }
        )
    return output


def _metadata(value: Any) -> dict[str, Any]:
    try:
        return _record_metadata_dict(value)
    except Exception as exc:
        raise RemoteBindingError("remote record metadata is not readable") from exc


def _records(dataset: Any) -> tuple[Any, ...]:
    collection = getattr(dataset, "records", None)
    if collection is None:
        raise RemoteBindingError("remote dataset does not expose public records")
    try:
        return tuple(collection)
    except TypeError as exc:
        raise RemoteBindingError("remote dataset records are not iterable") from exc


def _remote_record_id(record: Any) -> str:
    value = getattr(record, "id", None)
    return str(value or "").strip()


def _record_fingerprint(
    snapshot: ArgillaAllocationSnapshot,
    item: AllocationAssignmentItem,
    assignments: tuple[AllocationAssignment, ...],
) -> str:
    return _stable_hash(
        {
            "schema_version": 1,
            "plan_id": str(snapshot.plan.plan_id),
            "plan_fingerprint": snapshot.plan.fingerprint,
            "dataset_group_id": str(item.dataset_group_id),
            "dataset_id": str(item.remote.dataset_id),
            "record_id": str(item.remote.record_id),
            "item_id": str(item.item_id),
            "source_record_id": item.source_record_id,
            "batch_id": item.batch_id,
            "content_hash": item.content_hash,
            "fields": item.fields,
            "assignment_ids": sorted(str(assignment.assignment_id) for assignment in assignments),
        }
    )


def _expected_metadata(
    snapshot: ArgillaAllocationSnapshot,
    item: AllocationAssignmentItem,
    assignments: tuple[AllocationAssignment, ...],
) -> dict[str, Any]:
    return {
        _RECORD_FINGERPRINT: _record_fingerprint(snapshot, item, assignments),
        _PLAN_FINGERPRINT: snapshot.plan.fingerprint,
        _ITEM_ID: str(item.item_id),
        _DATASET_ID: str(item.remote.dataset_id),
        _BATCH_ID: item.batch_id,
        _CONTENT_HASH: item.content_hash,
        _ASSIGNMENT_IDS: sorted(str(assignment.assignment_id) for assignment in assignments),
        _SOURCE_RECORD_ID: item.source_record_id,
    }


def _validate_snapshot(snapshot: ArgillaAllocationSnapshot) -> tuple[
    dict[UUID, AllocationDatasetBinding],
    dict[UUID, AllocationAssignmentItem],
    dict[UUID, tuple[AllocationAssignment, ...]],
]:
    if not isinstance(snapshot, ArgillaAllocationSnapshot):
        raise AllocationInputError("dispatch and collect require an ArgillaAllocationSnapshot")
    plan_id = _uuid(snapshot.plan.plan_id, "plan_id")
    _uuid(snapshot.plan.connection_binding_id, "connection_binding_id")
    fingerprint = str(snapshot.plan.fingerprint or "").strip().lower()
    if not _SHA256_RE.fullmatch(fingerprint):
        raise AllocationInputError("plan fingerprint must be a lowercase SHA-256 digest")
    if _enum_value(snapshot.plan.lifecycle_state) != "confirmed":
        raise AllocationInputError("Argilla dispatch/collect require a confirmed allocation plan")
    workspace_id = _uuid(snapshot.workspace.workspace_id, "workspace_id")
    datasets: dict[UUID, AllocationDatasetBinding] = {}
    remote_dataset_ids: set[UUID] = set()
    for dataset in snapshot.datasets:
        group_id = _uuid(dataset.dataset_group_id, "dataset_group_id")
        dataset_workspace_id = _uuid(dataset.workspace_id, "dataset.workspace_id")
        dataset_id = _uuid(dataset.dataset_id, "dataset_id")
        if dataset_workspace_id != workspace_id:
            raise AllocationInputError("dataset workspace binding does not match the allocation workspace")
        if group_id in datasets or dataset_id in remote_dataset_ids:
            raise AllocationInputError("allocation dataset bindings must be unique")
        if int(dataset.min_submitted) < 1:
            raise AllocationInputError("dataset min_submitted must be positive")
        if dataset.settings_fingerprint and not _SHA256_RE.fullmatch(dataset.settings_fingerprint):
            raise AllocationInputError("settings fingerprint must be a lowercase SHA-256 digest")
        if dataset.intent_fingerprint and not _SHA256_RE.fullmatch(dataset.intent_fingerprint):
            raise AllocationInputError("intent fingerprint must be a lowercase SHA-256 digest")
        datasets[group_id] = dataset
        remote_dataset_ids.add(dataset_id)
    if not datasets:
        raise AllocationInputError("confirmed allocation snapshot must contain dataset bindings")

    items: dict[UUID, AllocationAssignmentItem] = {}
    remote_records: set[tuple[UUID, UUID]] = set()
    for item in snapshot.items:
        item_id = _uuid(item.item_id, "item_id")
        group_id = _uuid(item.dataset_group_id, "item.dataset_group_id")
        if group_id not in datasets or item_id in items:
            raise AllocationInputError("assignment item is not uniquely bound to a dataset group")
        if not str(item.source_record_id).strip() or not str(item.batch_id).strip():
            raise AllocationInputError("assignment item source_record_id and batch_id are required")
        if not _SHA256_RE.fullmatch(str(item.content_hash).strip().lower()):
            raise AllocationInputError("assignment item content_hash must be a lowercase SHA-256 digest")
        if not isinstance(item.fields, Mapping):
            raise AllocationInputError("assignment item fields must be a mapping")
        try:
            json.dumps(_canonical_value(item.fields), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise AllocationInputError("assignment item fields must be JSON-compatible") from exc
        remote = item.remote
        if _uuid(remote.item_id, "remote.item_id") != item_id:
            raise AllocationInputError("remote record binding item_id does not match the assignment item")
        if _uuid(remote.dataset_group_id, "remote.dataset_group_id") != group_id:
            raise AllocationInputError("remote record binding dataset_group_id does not match the assignment item")
        dataset_id = _uuid(remote.dataset_id, "remote.dataset_id")
        if dataset_id != _uuid(datasets[group_id].dataset_id, "dataset_id"):
            raise AllocationInputError("remote record binding dataset_id does not match its dataset group")
        record_id = _uuid(remote.record_id, "remote.record_id")
        if (dataset_id, record_id) in remote_records:
            raise AllocationInputError("remote record bindings must be unique")
        items[item_id] = item
        remote_records.add((dataset_id, record_id))
    if not items:
        raise AllocationInputError("confirmed allocation snapshot must contain assignment items")

    assignments_by_item: dict[UUID, list[AllocationAssignment]] = defaultdict(list)
    assignment_ids: set[UUID] = set()
    for assignment in snapshot.assignments:
        assignment_id = _uuid(assignment.assignment_id, "assignment_id")
        item_id = _uuid(assignment.item_id, "assignment.item_id")
        if assignment_id in assignment_ids or item_id not in items:
            raise AllocationInputError("assignment must reference a unique known assignment item")
        if not assignment.respondent_ids or any(_optional_uuid(value) is None for value in assignment.respondent_ids):
            raise AllocationInputError("assignment must contain valid trusted respondent UUIDs")
        if int(assignment.required_submissions) < 1:
            raise AllocationInputError("assignment required_submissions must be positive")
        assignment_ids.add(assignment_id)
        assignments_by_item[item_id].append(assignment)
    if any(item_id not in assignments_by_item for item_id in items):
        raise AllocationInputError("every assignment item must have at least one assignment")
    questions = [str(value).strip() for value in snapshot.required_question_names]
    if len(questions) != len(set(questions)) or any(not value for value in questions):
        raise AllocationInputError("required_question_names must be unique and non-empty")
    if snapshot.contract is not None and snapshot.contract.schema_version != 1:
        raise AllocationInputError("unsupported Argilla allocation contract schema")
    return datasets, items, {key: tuple(value) for key, value in assignments_by_item.items()}


class ArgillaAllocationAdapter:
    """Dispatch and collect records using only a confirmed allocation snapshot."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        api_url: str | None = None,
        sdk_module: Any | None = None,
        runtime_versions: Mapping[str, str] | None = None,
        record_factory: Callable[..., Any] | None = None,
    ) -> None:
        sdk = sdk_module
        if client is None:
            sdk = sdk or _load_argilla()
            client = _client(_api_url(api_url))
        if runtime_versions is None:
            if sdk is None:
                raise AllocationAdapterError("runtime_versions is required when injecting a fake client")
            runtime_versions = _runtime_versions(sdk, _api_url(api_url))
        self.client = client
        self.runtime_versions = {
            "sdk": _require_argilla_2_8_version("Argilla SDK", runtime_versions.get("sdk")),
            "server": _require_argilla_2_8_version("Argilla server", runtime_versions.get("server")),
        }
        if record_factory is None:
            sdk = sdk or _load_argilla()
            record_factory = getattr(sdk, "Record", None)
        if not callable(record_factory):
            raise AllocationAdapterError("Argilla Record factory is unavailable")
        self._record_factory = record_factory

    def _validate_contract(self, snapshot: ArgillaAllocationSnapshot) -> None:
        contract = snapshot.contract
        if contract is None:
            return
        if _require_argilla_2_8_version("Argilla contract SDK", contract.sdk_version) != self.runtime_versions["sdk"]:
            raise RemoteBindingError("Argilla SDK version does not match the allocation contract")
        if _require_argilla_2_8_version("Argilla contract server", contract.server_version) != self.runtime_versions["server"]:
            raise RemoteBindingError("Argilla server version does not match the allocation contract")

    @staticmethod
    def _lookup(manager: Any, resource_id: UUID, *, fetch: bool = False) -> Any | None:
        try:
            resource = manager(id=resource_id)
        except TypeError:
            resource = manager(id=str(resource_id))
        if resource is None:
            return None
        getter = getattr(resource, "get", None)
        if fetch and callable(getter):
            fetched = getter()
            if fetched is not None:
                resource = fetched
        return resource

    def _workspace(self, binding: AllocationWorkspaceBinding) -> Any:
        resource = self._lookup(getattr(self.client, "workspaces"), _uuid(binding.workspace_id, "workspace_id"))
        if resource is None or _optional_uuid(getattr(resource, "id", None)) != _uuid(binding.workspace_id, "workspace_id"):
            raise RemoteBindingError("Argilla workspace UUID does not match the trusted binding")
        if binding.name is not None and str(getattr(resource, "name", "")) != binding.name:
            raise RemoteBindingError("Argilla workspace name does not match the trusted binding")
        return resource

    def _dataset(self, binding: AllocationDatasetBinding, workspace: Any) -> Any:
        dataset = self._lookup(
            getattr(self.client, "datasets"),
            _uuid(binding.dataset_id, "dataset_id"),
            fetch=True,
        )
        if dataset is None or _optional_uuid(getattr(dataset, "id", None)) != _uuid(binding.dataset_id, "dataset_id"):
            raise RemoteBindingError("Argilla dataset UUID does not match the trusted binding")
        if binding.name is not None and str(getattr(dataset, "name", "")) != binding.name:
            raise RemoteBindingError("Argilla dataset name does not match the trusted binding")
        dataset_workspace = getattr(dataset, "workspace", None)
        dataset_workspace_id = _optional_uuid(getattr(dataset_workspace, "id", dataset_workspace))
        if dataset_workspace_id is None:
            dataset_workspace_id = _optional_uuid(getattr(dataset, "workspace_id", None))
        workspace_id = _uuid(getattr(workspace, "id", None), "remote workspace id")
        if dataset_workspace_id != workspace_id:
            raise RemoteBindingError("Argilla dataset workspace does not match the trusted binding")
        distribution = getattr(dataset, "distribution", None)
        min_submitted = getattr(distribution, "min_submitted", None)
        if min_submitted is None:
            min_submitted = getattr(dataset, "min_submitted", None)
        try:
            min_submitted = int(min_submitted)
        except (TypeError, ValueError) as exc:
            raise RemoteBindingError("Argilla dataset distribution.min_submitted is unavailable") from exc
        if min_submitted != int(binding.min_submitted):
            raise RemoteBindingError("Argilla dataset min_submitted does not match the trusted binding")
        if binding.settings_fingerprint:
            try:
                actual = _settings_fingerprint(dataset.settings)
            except Exception as exc:
                raise RemoteBindingError("Argilla dataset settings fingerprint is unavailable") from exc
            if actual != binding.settings_fingerprint:
                raise RemoteBindingError("Argilla dataset settings do not match the trusted binding")
        if binding.intent_fingerprint:
            try:
                actual = _settings_intent_fingerprint(dataset.settings)
            except Exception as exc:
                raise RemoteBindingError("Argilla dataset contract intent is unavailable") from exc
            if actual != binding.intent_fingerprint:
                raise RemoteBindingError("Argilla dataset contract intent does not match the trusted binding")
        return dataset

    def dispatch(self, snapshot: ArgillaAllocationSnapshot) -> DispatchResult:
        datasets, items, assignments_by_item = _validate_snapshot(snapshot)
        self._validate_contract(snapshot)
        workspace = self._workspace(snapshot.workspace)
        records_logged = 0
        records_existing = 0
        batches: list[tuple[str, int]] = []
        for group_id, binding in sorted(datasets.items(), key=lambda pair: str(pair[0])):
            dataset = self._dataset(binding, workspace)
            remote_records = _records(dataset)
            by_id: dict[str, Any] = {}
            for record in remote_records:
                remote_id = _remote_record_id(record)
                try:
                    normalized_id = str(_uuid(remote_id, "remote record id"))
                except AllocationInputError as exc:
                    raise RemoteBindingError("Argilla dataset contains a record without a UUID") from exc
                if normalized_id in by_id:
                    raise RemoteBindingError("Argilla dataset contains duplicate record UUIDs")
                by_id[normalized_id] = record
            expected_items = [item for item in items.values() if _uuid(item.dataset_group_id, "dataset_group_id") == group_id]
            expected_ids = {str(_uuid(item.remote.record_id, "remote.record_id")) for item in expected_items}
            if set(by_id) - expected_ids:
                raise RemoteBindingError("Argilla dataset contains records outside the allocation binding")
            missing_by_batch: dict[str, list[Any]] = defaultdict(list)
            for item in sorted(expected_items, key=lambda value: (str(value.batch_id), str(value.remote.record_id))):
                item_id = _uuid(item.item_id, "item_id")
                item_assignments = assignments_by_item[item_id]
                expected = _expected_metadata(snapshot, item, item_assignments)
                remote_id = str(_uuid(item.remote.record_id, "remote.record_id"))
                existing = by_id.get(remote_id)
                if existing is not None:
                    remote_metadata = _metadata(existing)
                    if any(remote_metadata.get(key) != value for key, value in expected.items()):
                        raise RemoteBindingError("existing Argilla record metadata does not match the allocation binding")
                    if dict(getattr(existing, "fields", {}) or {}) != dict(item.fields):
                        raise RemoteBindingError("existing Argilla record fields do not match the allocation binding")
                    records_existing += 1
                    continue
                created = self._record_factory(id=remote_id, fields=dict(item.fields), metadata=expected)
                missing_by_batch[str(item.batch_id)].append(created)
            logger = getattr(getattr(dataset, "records", None), "log", None)
            if not callable(logger) and missing_by_batch:
                raise RemoteBindingError("Argilla dataset records collection is not writable")
            for batch_id in sorted(missing_by_batch):
                batch_records = missing_by_batch[batch_id]
                logger(batch_records)
                records_logged += len(batch_records)
                batches.append((f"{binding.dataset_id}:{batch_id}", len(batch_records)))
        return DispatchResult(
            plan_id=_uuid(snapshot.plan.plan_id, "plan_id"),
            plan_fingerprint=snapshot.plan.fingerprint,
            records_logged=records_logged,
            records_existing=records_existing,
            batches=tuple(batches),
        )

    @staticmethod
    def _labels_hash(values: Mapping[str, Any]) -> str:
        return _stable_hash({"labels": values})

    @staticmethod
    def _reason(reason_codes: list[str]) -> str:
        return reason_codes[0] if reason_codes else "manual_review"

    def _record_receipt(
        self,
        repository: Any,
        request: AllocationReceiptRequest,
        *,
        actor_identity: Any,
        caller_identity: Any,
        workspace_slug: str,
        channel: AuditChannel,
        request_id: str | None,
    ) -> Any:
        return repository.record_receipt(
            request,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            idempotency_key=f"argilla-collect:{request.connection_binding_id}:{request.argilla_response_id}",
            channel=channel,
            request_id=request_id,
        )

    def _quarantine(
        self,
        *,
        snapshot: ArgillaAllocationSnapshot,
        dataset_id: str | None,
        record_id: str | None,
        item: AllocationAssignmentItem | None,
        group: dict[str, Any] | None,
        reason_codes: list[str],
        repository: Any,
        actor_identity: Any,
        caller_identity: Any,
        workspace_slug: str,
        channel: AuditChannel,
        request_id: str | None,
    ) -> QuarantineEntry:
        group = group or {
            "response_id": None,
            "respondent_id": None,
            "status": "unknown",
            "values": {},
            "question_names": (),
            "observed_statuses": (),
            "timestamp": _EPOCH,
        }
        response_id = group.get("response_id")
        respondent_id = group.get("respondent_id")
        labels_hash = self._labels_hash(group.get("values") or {})
        receipt_id: UUID | None = None
        receipt_replayed = False
        if item is not None and response_id is not None and respondent_id is not None:
            remote_dataset_id = _uuid(item.remote.dataset_id, "remote.dataset_id")
            remote_record_id = _uuid(item.remote.record_id, "remote.record_id")
            request_value = AllocationReceiptRequest(
                plan_id=_uuid(snapshot.plan.plan_id, "plan_id"),
                item_id=_uuid(item.item_id, "item_id"),
                connection_binding_id=_uuid(snapshot.plan.connection_binding_id, "connection_binding_id"),
                argilla_dataset_id=remote_dataset_id,
                argilla_record_id=remote_record_id,
                argilla_response_id=response_id,
                argilla_respondent_id=respondent_id,
                response_status=str(group.get("status") or "unknown"),
                disposition=CollectionDisposition.QUARANTINED,
                labels_hash=labels_hash,
                pulled_at=group.get("timestamp") or _EPOCH,
                rejection_reason=ReceiptRejectionReason(self._reason(reason_codes)),
            )
            try:
                result = self._record_receipt(
                    repository,
                    request_value,
                    actor_identity=actor_identity,
                    caller_identity=caller_identity,
                    workspace_slug=workspace_slug,
                    channel=channel,
                    request_id=request_id,
                )
            except AllocationNaturalKeyConflict:
                result = None
            except AllocationRepositoryValidationError:
                result = None
            else:
                receipt_id = _optional_uuid(getattr(result, "receipt_id", None))
                receipt_replayed = bool(getattr(result, "replayed", False))
        return QuarantineEntry(
            reason=self._reason(reason_codes),
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            dataset_id=dataset_id,
            record_id=record_id,
            item_id=_optional_uuid(getattr(item, "item_id", None)),
            response_id=response_id,
            respondent_id=respondent_id,
            response_status=str(group.get("status") or "unknown"),
            question_names=tuple(group.get("question_names") or ()),
            observed_statuses=tuple(group.get("observed_statuses") or ()),
            labels_hash=labels_hash,
            receipt_id=receipt_id,
            receipt_replayed=receipt_replayed,
        )

    def _accepted(
        self,
        *,
        snapshot: ArgillaAllocationSnapshot,
        item: AllocationAssignmentItem,
        assignment: AllocationAssignment,
        group: dict[str, Any],
        repository: Any,
        actor_identity: Any,
        caller_identity: Any,
        workspace_slug: str,
        channel: AuditChannel,
        request_id: str | None,
    ) -> AcceptedReceipt:
        response_id = group["response_id"]
        respondent_id = group["respondent_id"]
        labels_hash = self._labels_hash(group["values"])
        request_value = AllocationReceiptRequest(
            plan_id=_uuid(snapshot.plan.plan_id, "plan_id"),
            item_id=_uuid(item.item_id, "item_id"),
            assignment_id=_uuid(assignment.assignment_id, "assignment_id"),
            connection_binding_id=_uuid(snapshot.plan.connection_binding_id, "connection_binding_id"),
            argilla_dataset_id=_uuid(item.remote.dataset_id, "remote.dataset_id"),
            argilla_record_id=_uuid(item.remote.record_id, "remote.record_id"),
            argilla_response_id=response_id,
            argilla_respondent_id=respondent_id,
            response_status="submitted",
            disposition=CollectionDisposition.ACCEPTED,
            labels_hash=labels_hash,
            pulled_at=group.get("timestamp") or _EPOCH,
        )
        try:
            result = self._record_receipt(
                repository,
                request_value,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                channel=channel,
                request_id=request_id,
            )
        except AllocationNaturalKeyConflict as exc:
            raise AllocationAdapterError("accepted response conflicts with an existing receipt") from exc
        except AllocationNotReady as exc:
            raise AllocationAdapterError("allocation repository is not ready for collection") from exc
        except AllocationRepositoryError as exc:
            raise AllocationAdapterError("allocation repository rejected the accepted receipt") from exc
        return AcceptedReceipt(
            item_id=_uuid(item.item_id, "item_id"),
            assignment_id=_uuid(assignment.assignment_id, "assignment_id"),
            response_id=response_id,
            receipt_id=_optional_uuid(getattr(result, "receipt_id", None)),
            replayed=bool(getattr(result, "replayed", False)),
            labels_hash=labels_hash,
        )

    def collect(
        self,
        snapshot: ArgillaAllocationSnapshot,
        *,
        repository: Any,
        actor_identity: Any,
        caller_identity: Any,
        workspace_slug: str,
        channel: AuditChannel = AuditChannel.WORKER,
        request_id: str | None = None,
    ) -> CollectResult:
        datasets, items, assignments_by_item = _validate_snapshot(snapshot)
        self._validate_contract(snapshot)
        workspace = self._workspace(snapshot.workspace)
        accepted: list[AcceptedReceipt] = []
        quarantine: list[QuarantineEntry] = []
        required_questions = set(snapshot.required_question_names)
        for group_id, binding in sorted(datasets.items(), key=lambda pair: str(pair[0])):
            try:
                dataset = self._dataset(binding, workspace)
            except RemoteBindingError:
                quarantine.append(
                    self._quarantine(
                        snapshot=snapshot,
                        dataset_id=str(_uuid(binding.dataset_id, "dataset_id")),
                        record_id=None,
                        item=None,
                        group=None,
                        reason_codes=["remote_binding_mismatch"],
                        repository=repository,
                        actor_identity=actor_identity,
                        caller_identity=caller_identity,
                        workspace_slug=workspace_slug,
                        channel=channel,
                        request_id=request_id,
                    )
                )
                continue
            expected_items = {
                str(_uuid(item.remote.record_id, "remote.record_id")): item
                for item in items.values()
                if _uuid(item.dataset_group_id, "dataset_group_id") == group_id
            }
            for record in _records(dataset):
                observed_record_id = _remote_record_id(record)
                item = expected_items.get(str(_optional_uuid(observed_record_id) or observed_record_id))
                groups = _response_groups(record)
                if item is None:
                    if not groups:
                        groups = [None]
                    for group in groups:
                        quarantine.append(
                            self._quarantine(
                                snapshot=snapshot,
                                dataset_id=str(_uuid(binding.dataset_id, "dataset_id")),
                                record_id=observed_record_id or None,
                                item=None,
                                group=group,
                                reason_codes=["unknown_record"],
                                repository=repository,
                                actor_identity=actor_identity,
                                caller_identity=caller_identity,
                                workspace_slug=workspace_slug,
                                channel=channel,
                                request_id=request_id,
                            )
                        )
                    continue
                expected_metadata = _expected_metadata(snapshot, item, assignments_by_item[_uuid(item.item_id, "item_id")])
                remote_metadata = _metadata(record)
                metadata_matches = all(remote_metadata.get(key) == value for key, value in expected_metadata.items())
                if not metadata_matches:
                    if not groups:
                        groups = [None]
                    for group in groups:
                        quarantine.append(
                            self._quarantine(
                                snapshot=snapshot,
                                dataset_id=str(_uuid(binding.dataset_id, "dataset_id")),
                                record_id=observed_record_id,
                                item=item,
                                group=group,
                                reason_codes=["remote_binding_mismatch"],
                                repository=repository,
                                actor_identity=actor_identity,
                                caller_identity=caller_identity,
                                workspace_slug=workspace_slug,
                                channel=channel,
                                request_id=request_id,
                            )
                        )
                    continue
                for group in groups:
                    if group["status"] != "submitted":
                        reason_codes = ["invalid_response"]
                    else:
                        reason_codes = []
                    if group.get("respondent_id") is None:
                        reason_codes.append("untrusted_respondent")
                    if group.get("response_id") is None:
                        reason_codes.append("invalid_response")
                    if group.get("schema_error") or group.get("duplicate_questions"):
                        reason_codes.append("schema_mismatch")
                    if required_questions - set(group.get("values") or {}):
                        reason_codes.append("schema_mismatch")
                    if required_questions and not set(group.get("values") or {}).issubset(required_questions):
                        reason_codes.append("schema_mismatch")
                    assignments = assignments_by_item[_uuid(item.item_id, "item_id")]
                    matching = [
                        assignment
                        for assignment in assignments
                        if group.get("respondent_id") is not None
                        and group["respondent_id"] in {_uuid(value, "respondent_id") for value in assignment.respondent_ids}
                    ]
                    if len(matching) != 1:
                        reason_codes.append("untrusted_respondent")
                    if reason_codes:
                        quarantine.append(
                            self._quarantine(
                                snapshot=snapshot,
                                dataset_id=str(_uuid(binding.dataset_id, "dataset_id")),
                                record_id=observed_record_id,
                                item=item,
                                group=group,
                                reason_codes=reason_codes,
                                repository=repository,
                                actor_identity=actor_identity,
                                caller_identity=caller_identity,
                                workspace_slug=workspace_slug,
                                channel=channel,
                                request_id=request_id,
                            )
                        )
                        continue
                    accepted.append(
                        self._accepted(
                            snapshot=snapshot,
                            item=item,
                            assignment=matching[0],
                            group=group,
                            repository=repository,
                            actor_identity=actor_identity,
                            caller_identity=caller_identity,
                            workspace_slug=workspace_slug,
                            channel=channel,
                            request_id=request_id,
                        )
                    )
        return CollectResult(
            plan_id=_uuid(snapshot.plan.plan_id, "plan_id"),
            accepted=tuple(accepted),
            quarantine=tuple(quarantine),
        )
