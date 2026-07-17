from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import pytest

from llm_labeling_scaffold.db.enums import CollectionDisposition
from llm_labeling_scaffold.allocation_runtime import (
    AllocationAssignment,
    AllocationAssignmentItem,
    AllocationDatasetBinding,
    AllocationInputError,
    AllocationRecordBinding,
    AllocationRuntimeContract,
    AllocationWorkspaceBinding,
    ArgillaAllocationAdapter,
    ArgillaAllocationSnapshot,
    ConfirmedAllocationPlan,
    RemoteBindingError,
)


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
DATASET_ID = UUID("20000000-0000-0000-0000-000000000001")
GROUP_ID = UUID("30000000-0000-0000-0000-000000000001")
PLAN_ID = UUID("40000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("50000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("60000000-0000-0000-0000-000000000001")
ASSIGNMENT_ID = UUID("70000000-0000-0000-0000-000000000001")
RECORD_ID = UUID("80000000-0000-0000-0000-000000000001")
ANNOTATOR_ID = UUID("90000000-0000-0000-0000-000000000001")
OTHER_USER_ID = UUID("90000000-0000-0000-0000-000000000002")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeRecord:
    def __init__(self, *, id, fields, metadata=None, responses=None):
        self.id = id
        self.fields = fields
        self.metadata = metadata or {}
        self.responses = list(responses or [])


class FakeRecords:
    def __init__(self, records=None):
        self.records = list(records or [])
        self.log_calls = []

    def __iter__(self):
        return iter(self.records)

    def log(self, records):
        batch = list(records)
        self.log_calls.append(batch)
        self.records.extend(batch)


class FakeDataset:
    def __init__(self, *, id, workspace, name="dataset", records=None, min_submitted=1):
        self.id = id
        self.workspace = workspace
        self.name = name
        self.distribution = SimpleNamespace(min_submitted=min_submitted)
        self.records = FakeRecords(records)

    def get(self):
        return self


class FakeResources:
    def __init__(self, resources):
        self.resources = {str(resource.id): resource for resource in resources}

    def __call__(self, *, id):
        return self.resources.get(str(id))


class FakeClient:
    def __init__(self, workspace, datasets):
        self.workspaces = FakeResources([workspace])
        self.datasets = FakeResources(datasets)


@dataclass(frozen=True)
class FakeReceipt:
    receipt_id: UUID
    replayed: bool


class FakeRepository:
    def __init__(self):
        self.requests = []
        self.receipts = {}

    def record_receipt(self, request, **kwargs):
        key = (request.connection_binding_id, request.argilla_response_id)
        existing = self.receipts.get(key)
        if existing is not None:
            return FakeReceipt(existing, True)
        self.requests.append((request, kwargs))
        receipt_id = uuid5(NAMESPACE_URL, f"receipt:{key[0]}:{key[1]}")
        self.receipts[key] = receipt_id
        return FakeReceipt(receipt_id, False)


def _snapshot(*, lifecycle_state="confirmed"):
    workspace = AllocationWorkspaceBinding(WORKSPACE_ID, "workspace")
    dataset = AllocationDatasetBinding(GROUP_ID, WORKSPACE_ID, DATASET_ID, 1, "dataset")
    item = AllocationAssignmentItem(
        item_id=ITEM_ID,
        dataset_group_id=GROUP_ID,
        source_record_id="source-1",
        batch_id="batch-1",
        content_hash=_hash("source-1"),
        fields={"text": "allocation-only"},
        remote=AllocationRecordBinding(ITEM_ID, GROUP_ID, DATASET_ID, RECORD_ID),
    )
    assignment = AllocationAssignment(ASSIGNMENT_ID, ITEM_ID, (ANNOTATOR_ID,), role="primary")
    return ArgillaAllocationSnapshot(
        plan=ConfirmedAllocationPlan(PLAN_ID, _hash("plan"), CONNECTION_ID, lifecycle_state),
        workspace=workspace,
        datasets=(dataset,),
        items=(item,),
        assignments=(assignment,),
        required_question_names=("label", "reason"),
        contract=AllocationRuntimeContract(),
    )


def _adapter(dataset):
    workspace = dataset.workspace
    return ArgillaAllocationAdapter(
        client=FakeClient(workspace, [dataset]),
        runtime_versions={"sdk": "2.8.0", "server": "2.8.0"},
        record_factory=FakeRecord,
    )


def _response(response_id, user_id, question_name, value, status="submitted"):
    return SimpleNamespace(
        id=response_id,
        user_id=user_id,
        question_name=question_name,
        value=value,
        status=status,
        submitted_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )


def test_dispatch_requires_confirmed_plan_and_never_accepts_sample_input():
    workspace = SimpleNamespace(id=WORKSPACE_ID, name="workspace")
    dataset = FakeDataset(id=DATASET_ID, workspace=workspace, name="dataset")
    adapter = _adapter(dataset)

    with pytest.raises(AllocationInputError, match="confirmed"):
        adapter.dispatch(_snapshot(lifecycle_state="draft"))


def test_dispatch_logs_by_batch_and_full_retry_is_idempotent():
    workspace = SimpleNamespace(id=WORKSPACE_ID, name="workspace")
    dataset = FakeDataset(id=DATASET_ID, workspace=workspace, name="dataset")
    adapter = _adapter(dataset)
    snapshot = _snapshot()

    first = adapter.dispatch(snapshot)
    second = adapter.dispatch(snapshot)

    assert first.records_logged == 1
    assert first.records_existing == 0
    assert second.records_logged == 0
    assert second.records_existing == 1
    assert len(dataset.records.log_calls) == 1
    record = dataset.records.records[0]
    assert record.id == str(RECORD_ID)
    assert record.metadata["__lls_allocation_plan_fingerprint"] == snapshot.plan.fingerprint
    assert record.metadata["__lls_allocation_batch_id"] == "batch-1"


def test_dispatch_rejects_existing_record_with_drift():
    workspace = SimpleNamespace(id=WORKSPACE_ID, name="workspace")
    dataset = FakeDataset(id=DATASET_ID, workspace=workspace, name="dataset")
    adapter = _adapter(dataset)
    adapter.dispatch(_snapshot())
    dataset.records.records[0].fields = {"text": "changed"}

    with pytest.raises(RemoteBindingError, match="fields"):
        adapter.dispatch(_snapshot())


def test_collect_accepts_submitted_response_and_repository_replay_is_stable():
    workspace = SimpleNamespace(id=WORKSPACE_ID, name="workspace")
    dataset = FakeDataset(id=DATASET_ID, workspace=workspace, name="dataset")
    adapter = _adapter(dataset)
    snapshot = _snapshot()
    adapter.dispatch(snapshot)
    response_id = UUID("a0000000-0000-0000-0000-000000000001")
    dataset.records.records[0].responses = [
        _response(response_id, ANNOTATOR_ID, "label", "yes"),
        _response(response_id, ANNOTATOR_ID, "reason", "evidence"),
    ]
    repository = FakeRepository()

    first = adapter.collect(
        snapshot,
        repository=repository,
        actor_identity="actor",
        caller_identity="caller",
        workspace_slug="workspace",
    )
    second = adapter.collect(
        snapshot,
        repository=repository,
        actor_identity="actor",
        caller_identity="caller",
        workspace_slug="workspace",
    )

    assert len(first.accepted) == 1
    assert first.accepted[0].replayed is False
    assert len(second.accepted) == 1
    assert second.accepted[0].replayed is True
    assert len(repository.requests) == 1
    request, kwargs = repository.requests[0]
    assert request.disposition == CollectionDisposition.ACCEPTED
    assert request.response_status == "submitted"
    assert request.labels_hash == _hash(json.dumps({"labels": {"label": "yes", "reason": "evidence"}}, sort_keys=True, separators=(",", ":")))
    assert "yes" not in first.to_dict().__repr__()
    assert kwargs["idempotency_key"].endswith(str(response_id))


def test_collect_quarantines_draft_wrong_user_and_unknown_record_without_raw_dto():
    workspace = SimpleNamespace(id=WORKSPACE_ID, name="workspace")
    dataset = FakeDataset(id=DATASET_ID, workspace=workspace, name="dataset")
    adapter = _adapter(dataset)
    snapshot = _snapshot()
    adapter.dispatch(snapshot)
    draft_id = UUID("a0000000-0000-0000-0000-000000000002")
    wrong_user_id = UUID("a0000000-0000-0000-0000-000000000003")
    dataset.records.records[0].responses = [
        _response(draft_id, ANNOTATOR_ID, "label", "secret-draft", status="draft"),
        _response(draft_id, ANNOTATOR_ID, "reason", "secret-reason", status="draft"),
        _response(wrong_user_id, OTHER_USER_ID, "label", "wrong-user"),
        _response(wrong_user_id, OTHER_USER_ID, "reason", "wrong-user-reason"),
    ]
    unknown = FakeRecord(
        id=UUID("b0000000-0000-0000-0000-000000000001"),
        fields={"text": "unexpected"},
        metadata={},
        responses=[_response(UUID("c0000000-0000-0000-0000-000000000001"), ANNOTATOR_ID, "label", "unknown")],
    )
    dataset.records.records.append(unknown)
    repository = FakeRepository()

    result = adapter.collect(
        snapshot,
        repository=repository,
        actor_identity="actor",
        caller_identity="caller",
        workspace_slug="workspace",
    )

    assert len(result.accepted) == 0
    assert {entry.reason for entry in result.quarantine} >= {"invalid_response", "untrusted_respondent", "unknown_record"}
    assert len(repository.requests) == 2
    assert all(request.disposition == CollectionDisposition.QUARANTINED for request, _ in repository.requests)
    serialized = json.dumps(result.to_dict(), sort_keys=True)
    assert "secret-draft" not in serialized
    assert "wrong-user-reason" not in serialized


def test_collect_quarantines_remote_dataset_binding_mismatch():
    expected_workspace = SimpleNamespace(id=WORKSPACE_ID, name="workspace")
    actual_workspace = SimpleNamespace(id=uuid4(), name="workspace")
    dataset = FakeDataset(id=DATASET_ID, workspace=actual_workspace, name="dataset")
    adapter = ArgillaAllocationAdapter(
        client=FakeClient(expected_workspace, [dataset]),
        runtime_versions={"sdk": "2.8.0", "server": "2.8.0"},
        record_factory=FakeRecord,
    )
    repository = FakeRepository()

    result = adapter.collect(
        _snapshot(),
        repository=repository,
        actor_identity="actor",
        caller_identity="caller",
        workspace_slug="workspace",
    )

    assert len(result.accepted) == 0
    assert result.quarantine[0].reason == "remote_binding_mismatch"
    assert repository.requests == []
