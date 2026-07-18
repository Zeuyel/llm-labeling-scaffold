from __future__ import annotations

import pytest

from llm_labeling_scaffold.db import AuditChannel, AuthorizationDenied

from tests.test_allocation_repository import _request, repository


def test_panel_allocation_repository_detail_queries_are_materialized_and_audited(repository):
    data = repository
    created = data["repo"].create(
        _request(data),
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
        cohort_revision_id=data["cohort_revision_id"],
        idempotency_key="panel-query-create",
        channel=AuditChannel.API,
    )

    listed = data["repo"].list_plans(
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
    )
    assert listed["plans"][0]["plan_id"] == str(created.plan_id)
    assert listed["plans"][0]["audit_events"]

    detail = data["repo"].get_plan(
        created.plan_id,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
    )
    assert detail["plan_id"] == str(created.plan_id)
    assert detail["workspace_groups"]
    assert detail["dataset_groups"]
    assert detail["audit"]["events"]

    assignments = data["repo"].list_assignments(
        created.plan_id,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
    )
    assert assignments["assignments"]
    assignment = assignments["assignments"][0]
    assignment_detail = data["repo"].get_assignment(
        assignment["assignment_id"],
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
    )
    assert assignment_detail["assignment_id"] == assignment["assignment_id"]
    assert "labels_hash" not in assignment_detail


def test_panel_allocation_repository_cross_workspace_detail_is_not_visible(repository):
    data = repository
    created = data["repo"].create(
        _request(data),
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
        cohort_revision_id=data["cohort_revision_id"],
        idempotency_key="panel-query-cross-workspace",
        channel=AuditChannel.API,
    )

    with pytest.raises(AuthorizationDenied):
        data["repo"].get_plan(
            created.plan_id,
            actor_identity=data["identity"],
            caller_identity=data["identity"],
            workspace_slug="another-workspace",
        )
