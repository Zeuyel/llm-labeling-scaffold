from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterator

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..allocation import (
    AllocationPlan as PlannerAllocationPlan,
    AllocationRequest,
    AllocationStrategy as PlannerAllocationStrategy,
    AnnotatorSpec,
    AssignmentPhase as PlannerAssignmentPhase,
    AssignmentRole as PlannerAssignmentRole,
    CalibrationRule,
    ManifestKind as PlannerManifestKind,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    plan_allocation,
    preview_allocation,
    validate_allocation_request,
)
from .audit import append_audit_event
from .database import create_database_engine, create_session_factory
from .enums import (
    AllocationAssignmentRole,
    AllocationDatasetState,
    AllocationManifestKind,
    AllocationPhase,
    AllocationPlanLifecycle,
    AllocationStrategy,
    AllocationWorkspaceMode,
    AnnotatorCohortState,
    AnnotatorMappingState,
    AnnotatorVerificationState,
    ArgillaBindingState,
    AuditChannel,
    CollectionDisposition,
)
from .models import (
    AllocationAssignment,
    AllocationAssignmentItem,
    AllocationCollectionReceipt,
    AllocationDatasetGroup,
    AllocationDatasetGroupState,
    AllocationPlan,
    AllocationPlanState,
    AllocationRecordBinding,
    AllocationWorkspaceGroup,
    AnnotatorCohort,
    AnnotatorCohortMember,
    AnnotatorCohortRevision,
    ArgillaAnnotatorMapping,
    ArgillaConnectionBinding,
    AuditEvent,
    Principal,
    Task,
    TaskRevision,
)
from .rbac import Permission
from .service import (
    DatabaseTransaction,
    ExternalIdentity,
    IdempotencyClaim,
    IdempotencyClaimStatus,
    IdempotencyConflict,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REJECTION_REASON_CODES = frozenset(
    {
        "duplicate_response",
        "invalid_payload",
        "invalid_response",
        "manual_review",
        "policy_violation",
        "remote_binding_mismatch",
        "remote_deleted",
        "schema_mismatch",
        "unknown_record",
        "untrusted_respondent",
    }
)


class AllocationRepositoryError(RuntimeError):
    pass


class AllocationRepositoryValidationError(ValueError):
    pass


class AllocationResourceNotFound(AllocationRepositoryError):
    pass


class AllocationOperationInProgress(AllocationRepositoryError):
    pass


class AllocationNotReady(AllocationRepositoryError):
    pass


class AllocationNaturalKeyConflict(AllocationRepositoryError):
    pass


class ReceiptRejectionReason(str, Enum):
    DUPLICATE_RESPONSE = "duplicate_response"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_RESPONSE = "invalid_response"
    MANUAL_REVIEW = "manual_review"
    POLICY_VIOLATION = "policy_violation"
    REMOTE_BINDING_MISMATCH = "remote_binding_mismatch"
    REMOTE_DELETED = "remote_deleted"
    SCHEMA_MISMATCH = "schema_mismatch"
    UNKNOWN_RECORD = "unknown_record"
    UNTRUSTED_RESPONDENT = "untrusted_respondent"


@dataclass(frozen=True, slots=True)
class AllocationScope:
    workspace_slug: str
    task_key: str
    cohort_revision_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AllocationPlanResult:
    plan_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    task_revision_id: uuid.UUID
    cohort_revision_id: uuid.UUID
    fingerprint: str
    input_fingerprint: str
    lifecycle_state: AllocationPlanLifecycle
    response_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class AllocationConfirmationResult:
    plan_id: uuid.UUID
    lifecycle_state: AllocationPlanLifecycle
    confirmed_at: datetime | None
    response_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class AllocationReceiptRequest:
    plan_id: uuid.UUID
    item_id: uuid.UUID
    connection_binding_id: uuid.UUID
    argilla_dataset_id: uuid.UUID
    argilla_record_id: uuid.UUID
    argilla_response_id: uuid.UUID
    argilla_respondent_id: uuid.UUID
    response_status: str
    disposition: CollectionDisposition
    labels_hash: str
    pulled_at: datetime
    assignment_id: uuid.UUID | None = None
    rejection_reason: ReceiptRejectionReason | str | None = None


@dataclass(frozen=True, slots=True)
class AllocationProgress:
    plan_id: uuid.UUID
    accepted_submissions: int
    required_submissions: int
    completed: bool

    @property
    def ratio(self) -> float:
        if self.required_submissions == 0:
            return 1.0
        return self.accepted_submissions / self.required_submissions


@dataclass(frozen=True, slots=True)
class AllocationReceiptResult:
    receipt_id: uuid.UUID
    plan_id: uuid.UUID
    disposition: CollectionDisposition
    response_status: int
    replayed: bool
    progress: AllocationProgress


@dataclass(frozen=True, slots=True)
class _AllocationContext:
    task: Task
    task_revision: TaskRevision
    binding: ArgillaConnectionBinding
    cohort: AnnotatorCohort
    cohort_revision: AnnotatorCohortRevision
    members: tuple[tuple[AnnotatorCohortMember, ArgillaAnnotatorMapping], ...]
    actor: Principal
    caller: Principal


class TrustedAllocationRepository:
    """Persist planner output only after rebuilding it from trusted database state."""

    def __init__(
        self,
        session_factory,
        engine: Engine | None = None,
        *,
        planner: Callable[[AllocationRequest], PlannerAllocationPlan] = plan_allocation,
        previewer: Callable = preview_allocation,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._planner = planner
        self._previewer = previewer

    @classmethod
    def from_url(cls, database_url: str | None = None) -> TrustedAllocationRepository:
        engine = create_database_engine(database_url)
        return cls(create_session_factory(engine), engine)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            yield session

    def preview(
        self,
        request: AllocationRequest,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        cohort_revision_id: uuid.UUID,
    ):
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            context = self._load_context(
                session,
                transaction,
                request=request,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                cohort_revision_id=cohort_revision_id,
                require_current_revision=True,
            )
            normalized = self._normalize_request(request, context)
            return self._previewer(normalized)

    def preview_allocation(self, *args, **kwargs):
        return self.preview(*args, **kwargs)

    def create(
        self,
        request: AllocationRequest,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        cohort_revision_id: uuid.UUID,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> AllocationPlanResult:
        _require_write_channel(channel)
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            context = self._load_context(
                session,
                transaction,
                request=request,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                cohort_revision_id=cohort_revision_id,
                require_current_revision=True,
            )
            normalized = self._normalize_request(request, context)
            plan = self._planner(normalized)
            claim = transaction.claim_idempotency(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                required_permission=Permission.ANNOTATION_REVIEW,
                operation="allocation.plan.create",
                idempotency_key=idempotency_key,
                request_payload=_plan_request_payload(normalized, context),
                task_key=task_key,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                return _plan_result_from_claim(claim, replayed=True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AllocationOperationInProgress("allocation plan creation is already in progress")

            existing = session.scalar(
                select(AllocationPlan).where(
                    AllocationPlan.workspace_id == context.task.workspace_id,
                    AllocationPlan.fingerprint == plan.fingerprint,
                )
            )
            reused_existing = existing is not None
            if existing is None:
                try:
                    with session.begin_nested():
                        existing = _persist_plan(session, context, normalized, plan, claim, channel)
                except IntegrityError:
                    existing = session.scalar(
                        select(AllocationPlan).where(
                            AllocationPlan.workspace_id == context.task.workspace_id,
                            AllocationPlan.fingerprint == plan.fingerprint,
                        )
                    )
                    if existing is None:
                        raise
                    reused_existing = True

            state = _plan_state(session, existing.id)
            response_body = _plan_response(existing, state)
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                response_status=201,
                response_body=response_body,
            )
            _append_plan_audit(
                session,
                context,
                existing,
                channel=channel,
                request_id=request_id,
                replayed=reused_existing,
            )
            return _plan_result_from_claim(completed, replayed=reused_existing)

    def confirm(
        self,
        plan_id: uuid.UUID,
        *,
        plan_fingerprint: str,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> AllocationConfirmationResult:
        _require_write_channel(channel)
        plan_id = _require_uuid(plan_id, "plan_id")
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            plan, task = _load_plan_and_task(session, plan_id)
            self._authorize_plan(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task=task,
            )
            if plan.fingerprint != plan_fingerprint:
                raise AllocationRepositoryValidationError("plan fingerprint does not match the stored plan")
            claim = transaction.claim_idempotency(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                required_permission=Permission.ANNOTATION_REVIEW,
                operation="allocation.plan.confirm",
                idempotency_key=idempotency_key,
                request_payload={"plan_id": str(plan_id), "plan_fingerprint": plan_fingerprint},
                task_key=task.task_key,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                return _confirmation_result_from_claim(claim, replayed=True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AllocationOperationInProgress("allocation plan confirmation is already in progress")

            state = session.scalar(
                select(AllocationPlanState)
                .where(
                    AllocationPlanState.workspace_id == plan.workspace_id,
                    AllocationPlanState.plan_id == plan.id,
                )
                .with_for_update()
            )
            if state is None:
                raise AllocationResourceNotFound("allocation plan state was not found")
            transitioned = False
            if state.lifecycle_state == AllocationPlanLifecycle.DRAFT:
                _require_plan_ready(session, plan)
                now = datetime.now(timezone.utc)
                state.lifecycle_state = AllocationPlanLifecycle.CONFIRMED
                state.confirmed_at = now
                state.confirmed_by_principal_id = claim.actor_principal_id
                state.confirmed_via_caller_principal_id = claim.caller_principal_id
                state.confirmation_idempotency_record_id = claim.record_id
                session.flush()
                transitioned = True
            response_body = _confirmation_response(state)
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                response_status=200,
                response_body=response_body,
            )
            actor = session.get(Principal, claim.actor_principal_id)
            caller = session.get(Principal, claim.caller_principal_id)
            if actor is None or caller is None:
                raise AllocationResourceNotFound("confirmation principal disappeared")
            append_audit_event(
                session,
                workspace_id=plan.workspace_id,
                event_type=(
                    "allocation.plan.confirmed"
                    if transitioned
                    else "allocation.plan.confirmation_replayed"
                ),
                actor=actor,
                caller=caller,
                channel=channel,
                resource_type="allocation_plan",
                resource_id=plan.id,
                request_id=request_id,
                details={"plan_fingerprint": plan.fingerprint},
            )
            return _confirmation_result_from_claim(completed, replayed=False)

    def record_receipt(
        self,
        request: AllocationReceiptRequest,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> AllocationReceiptResult:
        _require_write_channel(channel)
        _validate_receipt_request(request)
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            plan, task = _load_plan_and_task(session, request.plan_id)
            context = self._load_plan_context(
                session,
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task=task,
                plan=plan,
                require_current_revision=False,
            )
            _validate_receipt_binding(session, context, plan, request)
            claim = transaction.claim_idempotency(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                required_permission=Permission.ANNOTATION_REVIEW,
                operation="allocation.receipt.record",
                idempotency_key=idempotency_key,
                request_payload=_receipt_payload(request),
                task_key=task.task_key,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                return _receipt_result_from_claim(claim, replayed=True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AllocationOperationInProgress("allocation receipt recording is already in progress")

            existing = session.scalar(
                select(AllocationCollectionReceipt).where(
                    AllocationCollectionReceipt.connection_binding_id == request.connection_binding_id,
                    AllocationCollectionReceipt.argilla_response_id == request.argilla_response_id,
                )
            )
            reused_receipt = existing is not None
            if existing is None:
                try:
                    with session.begin_nested():
                        existing = _insert_receipt(session, context, plan, request, claim, channel)
                except IntegrityError:
                    existing = session.scalar(
                        select(AllocationCollectionReceipt).where(
                            AllocationCollectionReceipt.connection_binding_id == request.connection_binding_id,
                            AllocationCollectionReceipt.argilla_response_id == request.argilla_response_id,
                        )
                    )
                    if existing is None or not _receipt_matches(existing, request):
                        raise AllocationNaturalKeyConflict("remote response is already recorded")
                    reused_receipt = True
            elif not _receipt_matches(existing, request):
                raise AllocationNaturalKeyConflict("remote response is already recorded for another item")

            progress = _allocation_progress(session, plan.id)
            response_body = _receipt_response(existing, progress)
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                response_status=201,
                response_body=response_body,
            )
            actor = session.get(Principal, claim.actor_principal_id)
            caller = session.get(Principal, claim.caller_principal_id)
            if actor is None or caller is None:
                raise AllocationResourceNotFound("receipt principal disappeared")
            append_audit_event(
                session,
                workspace_id=plan.workspace_id,
                event_type="allocation.receipt.recorded",
                actor=actor,
                caller=caller,
                channel=channel,
                resource_type="allocation_collection_receipt",
                resource_id=existing.id,
                request_id=request_id,
                details={
                    "plan_id": str(plan.id),
                    "item_id": str(existing.item_id),
                    "disposition": existing.disposition.value,
                    "replayed": reused_receipt,
                },
            )
            return _receipt_result_from_claim(completed, replayed=reused_receipt)

    def progress(
        self,
        plan_id: uuid.UUID,
        *,
        actor_identity: ExternalIdentity,
        workspace_slug: str,
    ) -> AllocationProgress:
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            plan, task = _load_plan_and_task(session, _require_uuid(plan_id, "plan_id"))
            decision = transaction.authorize_task(
                actor_identity,
                workspace_slug,
                task.task_key,
                Permission.ANNOTATION_REVIEW,
            )
            transaction.require(decision)
            if decision.workspace.id != plan.workspace_id:
                raise AllocationResourceNotFound("allocation plan was not found")
            return _allocation_progress(session, plan.id)

    def list_plans(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str | None = None,
        limit: int = 100,
        after_plan_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        limit = _page_limit(limit)
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            task_ids, workspace_id = self._authorized_plan_scope(
                session,
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
            )
            if not task_ids:
                return {"workspace": workspace_slug, "plans": [], "next_cursor": None}
            statement = (
                select(AllocationPlan, AllocationPlanState)
                .join(AllocationPlanState, AllocationPlanState.plan_id == AllocationPlan.id)
                .where(AllocationPlan.workspace_id == workspace_id)
                .order_by(AllocationPlan.id.desc())
                .limit(limit + 1)
            )
            statement = statement.where(AllocationPlan.task_id.in_(task_ids))
            if after_plan_id is not None:
                statement = statement.where(AllocationPlan.id < _require_uuid(after_plan_id, "after_plan_id"))
            rows = session.execute(statement).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "workspace": workspace_slug,
                "plans": _plan_summaries(session, rows),
                "next_cursor": str(rows[-1][0].id) if has_more and rows else None,
            }

    def get_plan(
        self,
        plan_id: uuid.UUID | str,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
    ) -> dict[str, Any]:
        plan_id = _require_uuid(plan_id, "plan_id")
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            plan, task = _load_plan_and_task(session, plan_id)
            self._authorize_plan(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task=task,
            )
            return _plan_detail(session, plan, _plan_state(session, plan.id))

    def list_assignments(
        self,
        plan_id: uuid.UUID | str,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        limit: int = 100,
        after_assignment_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        plan_id = _require_uuid(plan_id, "plan_id")
        limit = _page_limit(limit)
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            plan, task = _load_plan_and_task(session, plan_id)
            self._authorize_plan(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task=task,
            )
            statement = (
                select(
                    AllocationAssignment,
                    AllocationAssignmentItem,
                    AllocationDatasetGroup,
                    AllocationWorkspaceGroup,
                    AllocationRecordBinding,
                )
                .join(
                    AllocationAssignmentItem,
                    and_(
                        AllocationAssignmentItem.workspace_id == AllocationAssignment.workspace_id,
                        AllocationAssignmentItem.plan_id == AllocationAssignment.plan_id,
                        AllocationAssignmentItem.id == AllocationAssignment.item_id,
                    ),
                )
                .join(
                    AllocationDatasetGroup,
                    and_(
                        AllocationDatasetGroup.workspace_id == AllocationAssignment.workspace_id,
                        AllocationDatasetGroup.plan_id == AllocationAssignment.plan_id,
                        AllocationDatasetGroup.id == AllocationAssignmentItem.dataset_group_id,
                    ),
                )
                .join(
                    AllocationWorkspaceGroup,
                    and_(
                        AllocationWorkspaceGroup.workspace_id == AllocationDatasetGroup.workspace_id,
                        AllocationWorkspaceGroup.plan_id == AllocationDatasetGroup.plan_id,
                        AllocationWorkspaceGroup.id == AllocationDatasetGroup.workspace_group_id,
                    ),
                )
                .outerjoin(AllocationRecordBinding, AllocationRecordBinding.item_id == AllocationAssignmentItem.id)
                .where(
                    AllocationAssignment.workspace_id == plan.workspace_id,
                    AllocationAssignment.plan_id == plan.id,
                )
                .order_by(AllocationAssignment.id.desc())
                .limit(limit + 1)
            )
            if after_assignment_id is not None:
                statement = statement.where(
                    AllocationAssignment.id < _require_uuid(after_assignment_id, "after_assignment_id")
                )
            rows = session.execute(statement).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "workspace": workspace_slug,
                "plan_id": str(plan.id),
                "assignments": _assignment_details(session, rows),
                "next_cursor": str(rows[-1][0].id) if has_more and rows else None,
            }

    def get_assignment(
        self,
        assignment_id: uuid.UUID | str,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        plan_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        assignment_id = _require_uuid(assignment_id, "assignment_id")
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            statement = (
                select(
                    AllocationAssignment,
                    AllocationAssignmentItem,
                    AllocationDatasetGroup,
                    AllocationWorkspaceGroup,
                    AllocationRecordBinding,
                )
                .join(
                    AllocationAssignmentItem,
                    and_(
                        AllocationAssignmentItem.workspace_id == AllocationAssignment.workspace_id,
                        AllocationAssignmentItem.plan_id == AllocationAssignment.plan_id,
                        AllocationAssignmentItem.id == AllocationAssignment.item_id,
                    ),
                )
                .join(
                    AllocationDatasetGroup,
                    and_(
                        AllocationDatasetGroup.workspace_id == AllocationAssignment.workspace_id,
                        AllocationDatasetGroup.plan_id == AllocationAssignment.plan_id,
                        AllocationDatasetGroup.id == AllocationAssignmentItem.dataset_group_id,
                    ),
                )
                .join(
                    AllocationWorkspaceGroup,
                    and_(
                        AllocationWorkspaceGroup.workspace_id == AllocationDatasetGroup.workspace_id,
                        AllocationWorkspaceGroup.plan_id == AllocationDatasetGroup.plan_id,
                        AllocationWorkspaceGroup.id == AllocationDatasetGroup.workspace_group_id,
                    ),
                )
                .outerjoin(AllocationRecordBinding, AllocationRecordBinding.item_id == AllocationAssignmentItem.id)
                .where(AllocationAssignment.id == assignment_id)
            )
            if plan_id is not None:
                statement = statement.where(
                    AllocationAssignment.plan_id == _require_uuid(plan_id, "plan_id")
                )
            row = session.execute(statement).first()
            if row is None:
                raise AllocationResourceNotFound("allocation assignment was not found")
            assignment, _, _, _, _ = row
            plan, task = _load_plan_and_task(session, assignment.plan_id)
            self._authorize_plan(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task=task,
            )
            return _assignment_details(session, [row])[0]

    def _authorized_plan_scope(
        self,
        session: Session,
        transaction: DatabaseTransaction,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str | None,
    ) -> tuple[tuple[uuid.UUID, ...], uuid.UUID]:
        if task_key is not None:
            decision = transaction.authorize_task(
                actor_identity,
                workspace_slug,
                task_key,
                Permission.ANNOTATION_REVIEW,
            )
            transaction.require(decision)
            if decision.task is None or decision.workspace is None or decision.principal is None:
                raise AllocationResourceNotFound("allocation plan was not found")
            transaction._require_caller(caller_identity, decision.principal.id, Permission.ANNOTATION_REVIEW)
            return (decision.task.id,), decision.workspace.id

        task_accesses = []
        after_task_key = None
        workspace = None
        while True:
            page = transaction.list_authorized_tasks(
                actor_identity,
                workspace_slug,
                limit=100,
                after_task_key=after_task_key,
                include_archived=True,
            )
            if page.workspace is None:
                break
            workspace = page.workspace
            task_accesses.extend(page.items)
            if page.next_cursor is None:
                break
            after_task_key = page.next_cursor
        if workspace is None:
            raise AllocationResourceNotFound("allocation workspace was not found")
        actor = session.scalar(
            select(Principal).where(
                Principal.issuer == actor_identity.issuer,
                Principal.subject == actor_identity.subject,
            )
        )
        if actor is None:
            raise AllocationResourceNotFound("actor principal was not found")
        transaction._require_caller(caller_identity, actor.id, Permission.ANNOTATION_REVIEW)
        task_ids = tuple(
            access.task.id
            for access in task_accesses
            if Permission.ANNOTATION_REVIEW in access.capabilities
        )
        return task_ids, workspace.id

    def _load_context(
        self,
        session: Session,
        transaction: DatabaseTransaction,
        *,
        request: AllocationRequest,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        cohort_revision_id: uuid.UUID,
        require_current_revision: bool,
    ) -> _AllocationContext:
        if not isinstance(request, AllocationRequest):
            raise AllocationRepositoryValidationError("request must be an AllocationRequest")
        if not isinstance(request.task_revision, TaskRevisionRef):
            raise AllocationRepositoryValidationError("task_revision must be a TaskRevisionRef")
        decision = transaction.authorize_task(
            actor_identity,
            workspace_slug,
            task_key,
            Permission.ANNOTATION_REVIEW,
        )
        transaction.require(decision)
        if decision.task is None or decision.workspace is None or decision.principal is None:
            raise AllocationResourceNotFound("authorized task was not found")
        actor = session.get(Principal, decision.principal.id)
        if actor is None:
            raise AllocationResourceNotFound("actor principal was not found")
        caller = transaction._require_caller(
            caller_identity,
            actor.id,
            Permission.ANNOTATION_REVIEW,
        )
        task = session.get(Task, decision.task.id)
        if task is None:
            raise AllocationResourceNotFound("task was not found")
        return self._load_context_for_task(
            session,
            task=task,
            actor=actor,
            caller=caller,
            request=request,
            cohort_revision_id=cohort_revision_id,
            require_current_revision=require_current_revision,
        )

    def _load_plan_context(
        self,
        session: Session,
        transaction: DatabaseTransaction,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task: Task,
        plan: AllocationPlan,
        require_current_revision: bool,
    ) -> _AllocationContext:
        self._authorize_plan(
            transaction,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            task=task,
        )
        actor = session.scalar(select(Principal).where(Principal.issuer == actor_identity.issuer, Principal.subject == actor_identity.subject))
        caller = session.scalar(select(Principal).where(Principal.issuer == caller_identity.issuer, Principal.subject == caller_identity.subject))
        if actor is None or caller is None:
            raise AllocationResourceNotFound("receipt principal was not found")
        revision = session.get(AnnotatorCohortRevision, plan.cohort_revision_id)
        if revision is None:
            raise AllocationResourceNotFound("cohort revision was not found")
        request = _request_from_plan_context(plan, revision)
        return self._load_context_for_task(
            session,
            task=task,
            actor=actor,
            caller=caller,
            request=request,
            cohort_revision_id=revision.id,
            require_current_revision=require_current_revision,
        )

    def _authorize_plan(
        self,
        transaction: DatabaseTransaction,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task: Task,
    ) -> None:
        decision = transaction.authorize_task(
            actor_identity,
            workspace_slug,
            task.task_key,
            Permission.ANNOTATION_REVIEW,
        )
        transaction.require(decision)
        if decision.workspace is None or decision.workspace.id != task.workspace_id:
            raise AllocationResourceNotFound("allocation plan was not found")
        actor = decision.principal
        if actor is None:
            raise AllocationResourceNotFound("actor principal was not found")
        caller = transaction._require_caller(
            caller_identity,
            actor.id,
            Permission.ANNOTATION_REVIEW,
        )

    def _normalize_request(
        self,
        request: AllocationRequest,
        context: _AllocationContext,
    ) -> AllocationRequest:
        report = validate_allocation_request(request)
        if not report.ok:
            raise AllocationRepositoryValidationError(
                "; ".join(f"{issue.code} at {issue.field}" for issue in report.blocking_errors)
            )
        revision_id = _require_uuid(request.task_revision.revision_id, "task_revision.revision_id")
        if request.task_revision.task_id.strip() != context.task.task_key:
            raise AllocationRepositoryValidationError("task revision task_id does not match the authorized task")
        if revision_id != context.task_revision.id or request.task_revision.revision_hash != context.task_revision.content_hash:
            raise AllocationRepositoryValidationError("task revision is stale or belongs to another task")

        expected = {
            str(mapping.id): (str(context.cohort.id), member.default_capacity)
            for member, mapping in context.members
        }
        provided = {
            annotator.annotator_id.strip(): (annotator.cohort_id.strip(), annotator.capacity)
            for annotator in request.annotators
        }
        if provided != expected:
            raise AllocationRepositoryValidationError(
                "annotator set and capacity must exactly match the sealed cohort revision"
            )
        records = tuple(
            RecordSpec(
                record_id=record.record_id.strip(),
                content_hash=record.content_hash,
                batch_id=record.batch_id.strip(),
            )
            for record in request.records
        )
        annotators = tuple(
            AnnotatorSpec(
                annotator_id=annotator_id,
                cohort_id=cohort_id,
                capacity=capacity,
            )
            for annotator_id, (cohort_id, capacity) in sorted(expected.items())
        )
        return AllocationRequest(
            strategy=request.strategy,
            task_revision=TaskRevisionRef(
                task_id=context.task.task_key,
                revision_id=str(context.task_revision.id),
                revision_hash=context.task_revision.content_hash,
            ),
            source_manifest=SourceManifestRef(
                manifest_id=request.source_manifest.manifest_id.strip(),
                manifest_hash=request.source_manifest.manifest_hash,
                kind=request.source_manifest.kind,
            ),
            records=records,
            annotators=annotators,
            seed=request.seed,
            algorithm_version=request.algorithm_version.strip(),
            overlap_rules=tuple(
                OverlapRule(
                    record_ids=tuple(rule.record_ids),
                    count=rule.count,
                    rate=rule.rate,
                    required_submissions=rule.required_submissions,
                )
                for rule in request.overlap_rules
            ),
            calibration=(
                CalibrationRule(
                    record_ids=tuple(request.calibration.record_ids),
                    count=request.calibration.count,
                    rate=request.calibration.rate,
                    expected_annotator_ids=(
                        tuple(request.calibration.expected_annotator_ids)
                        if request.calibration.expected_annotator_ids is not None
                        else None
                    ),
                    exclude_from_production=request.calibration.exclude_from_production,
                )
                if request.calibration is not None
                else None
            ),
        )

    def _load_context_for_task(
        self,
        session: Session,
        *,
        task: Task,
        actor: Principal,
        caller: Principal,
        request: AllocationRequest,
        cohort_revision_id: uuid.UUID,
        require_current_revision: bool,
    ) -> _AllocationContext:
        revision_id = _require_uuid(request.task_revision.revision_id, "task_revision.revision_id")
        task_revision = session.scalar(
            select(TaskRevision).where(
                TaskRevision.workspace_id == task.workspace_id,
                TaskRevision.task_id == task.id,
                TaskRevision.id == revision_id,
            )
        )
        if task_revision is None:
            raise AllocationResourceNotFound("task revision was not found")
        if require_current_revision and task.current_revision_id != task_revision.id:
            raise AllocationRepositoryValidationError(
                "formal allocation must use the task current revision"
            )
        cohort_revision = session.scalar(
            select(AnnotatorCohortRevision).where(
                AnnotatorCohortRevision.workspace_id == task.workspace_id,
                AnnotatorCohortRevision.id == _require_uuid(cohort_revision_id, "cohort_revision_id"),
            )
        )
        if cohort_revision is None or cohort_revision.sealed_at is None or cohort_revision.connection_binding_id is None:
            raise AllocationNotReady("cohort revision must be sealed before allocation")
        cohort = session.scalar(
            select(AnnotatorCohort).where(
                AnnotatorCohort.workspace_id == task.workspace_id,
                AnnotatorCohort.id == cohort_revision.cohort_id,
                AnnotatorCohort.state == AnnotatorCohortState.ACTIVE,
            )
        )
        if cohort is None:
            raise AllocationResourceNotFound("active annotator cohort was not found")
        binding = session.scalar(
            select(ArgillaConnectionBinding).where(
                ArgillaConnectionBinding.workspace_id == task.workspace_id,
                ArgillaConnectionBinding.id == cohort_revision.connection_binding_id,
                ArgillaConnectionBinding.state == ArgillaBindingState.ACTIVE,
            )
        )
        if binding is None or binding.argilla_workspace_id is None or binding.argilla_workspace_name_snapshot is None:
            raise AllocationNotReady("Argilla connection binding is not ready")
        rows = session.execute(
            select(AnnotatorCohortMember, ArgillaAnnotatorMapping)
            .join(
                ArgillaAnnotatorMapping,
                and_(
                    ArgillaAnnotatorMapping.workspace_id == AnnotatorCohortMember.workspace_id,
                    ArgillaAnnotatorMapping.id == AnnotatorCohortMember.annotator_mapping_id,
                ),
            )
            .where(
                AnnotatorCohortMember.workspace_id == task.workspace_id,
                AnnotatorCohortMember.cohort_revision_id == cohort_revision.id,
            )
            .order_by(AnnotatorCohortMember.annotator_mapping_id)
        ).all()
        if len(rows) != cohort_revision.member_count or not rows:
            raise AllocationNotReady("cohort revision member snapshot is incomplete")
        if any(
            mapping.connection_binding_id != binding.id
            or mapping.state != AnnotatorMappingState.ACTIVE
            or mapping.verification_state != AnnotatorVerificationState.VERIFIED
            or mapping.argilla_user_id is None
            for _, mapping in rows
        ):
            raise AllocationNotReady("cohort contains an inactive or unverified annotator mapping")
        return _AllocationContext(
            task=task,
            task_revision=task_revision,
            binding=binding,
            cohort=cohort,
            cohort_revision=cohort_revision,
            members=tuple(rows),
            actor=actor,
            caller=caller,
        )


def _persist_plan(
    session: Session,
    context: _AllocationContext,
    request: AllocationRequest,
    plan: PlannerAllocationPlan,
    claim: IdempotencyClaim,
    channel: AuditChannel,
) -> AllocationPlan:
    records_by_id = {record.record_id: record for record in request.records}
    capacity_snapshot = [
        {
            "annotator_mapping_id": str(mapping.id),
            "capacity": member.default_capacity,
        }
        for member, mapping in context.members
    ]
    qc_snapshot = {
        "strategy": plan.strategy.value,
        "overlap_rules": [
            {
                "record_ids": list(rule.record_ids),
                "count": rule.count,
                "rate": rule.rate,
                "required_submissions": rule.required_submissions,
            }
            for rule in request.overlap_rules
        ],
        "calibration": _calibration_snapshot(request.calibration),
        "gate_ids": [gate.gate_id for gate in plan.gates],
    }
    row = AllocationPlan(
        workspace_id=context.task.workspace_id,
        connection_binding_id=context.binding.id,
        task_id=context.task.id,
        task_revision_id=context.task_revision.id,
        task_revision_hash=context.task_revision.content_hash,
        source_manifest_id=plan.source_manifest.manifest_id,
        source_manifest_kind=AllocationManifestKind(plan.source_manifest.kind.value),
        source_manifest_hash=plan.source_manifest.manifest_hash,
        cohort_revision_id=context.cohort_revision.id,
        strategy=AllocationStrategy(plan.strategy.value),
        schema_version=plan.schema_version,
        seed=Decimal(plan.seed),
        algorithm_version=plan.algorithm_version,
        input_fingerprint=plan.input_fingerprint,
        fingerprint=plan.fingerprint,
        capacity_snapshot=capacity_snapshot,
        qc_snapshot=qc_snapshot,
        actor_principal_id=claim.actor_principal_id,
        caller_principal_id=claim.caller_principal_id,
        channel=channel,
        idempotency_record_id=claim.record_id,
    )
    session.add(row)
    session.flush()

    workspace_rows: dict[str, AllocationWorkspaceGroup] = {}
    for group in plan.workspace_groups:
        workspace_row = AllocationWorkspaceGroup(
            workspace_id=row.workspace_id,
            plan_id=row.id,
            planner_workspace_group_id=group.workspace_group_id,
            phase=AllocationPhase(group.phase.value),
            mode=AllocationWorkspaceMode(group.mode.value),
            assignee_mapping_id=_optional_uuid(group.assignee_id),
            member_mapping_ids=list(group.member_annotator_ids),
        )
        session.add(workspace_row)
        session.flush()
        workspace_rows[group.workspace_group_id] = workspace_row

    dataset_rows: dict[str, AllocationDatasetGroup] = {}
    for group in plan.dataset_groups:
        workspace_row = workspace_rows.get(group.workspace_group_id)
        if workspace_row is None:
            raise AllocationRepositoryValidationError("planner returned an unknown workspace group")
        dataset_row = AllocationDatasetGroup(
            workspace_id=row.workspace_id,
            plan_id=row.id,
            workspace_group_id=workspace_row.id,
            planner_dataset_group_id=group.dataset_group_id,
            phase=AllocationPhase(group.phase.value),
            min_submitted=group.min_submitted,
            assignee_mapping_id=_optional_uuid(group.assignee_id),
            pool_id=_optional_uuid(group.pool_id),
        )
        session.add(dataset_row)
        session.flush()
        dataset_rows[group.dataset_group_id] = dataset_row

    item_rows: dict[tuple[str, str], AllocationAssignmentItem] = {}
    for group in plan.dataset_groups:
        dataset_row = dataset_rows[group.dataset_group_id]
        for record_id in group.record_ids:
            record = records_by_id.get(record_id)
            if record is None:
                raise AllocationRepositoryValidationError("planner returned an unknown record")
            item = AllocationAssignmentItem(
                workspace_id=row.workspace_id,
                plan_id=row.id,
                dataset_group_id=dataset_row.id,
                record_id=record.record_id,
                external_id=record.record_id,
                batch_id=record.batch_id,
                content_hash=record.content_hash,
            )
            session.add(item)
            session.flush()
            item_rows[(group.dataset_group_id, record_id)] = item

    for assignment in plan.assignments:
        item = item_rows.get((assignment.dataset_group_id, assignment.record_id))
        if item is None:
            raise AllocationRepositoryValidationError("planner returned an assignment without a materialized item")
        session.add(
            AllocationAssignment(
                workspace_id=row.workspace_id,
                plan_id=row.id,
                planner_assignment_id=assignment.assignment_id,
                item_id=item.id,
                phase=AllocationPhase(assignment.phase.value),
                role=AllocationAssignmentRole(assignment.role.value),
                assignee_mapping_id=_optional_uuid(assignment.assignee_id),
                pool_id=_optional_uuid(assignment.pool_id),
                required_submissions=assignment.required_submissions,
                gate_id=assignment.gate_id,
            )
        )
    session.flush()
    return row


def _load_plan_and_task(session: Session, plan_id: uuid.UUID) -> tuple[AllocationPlan, Task]:
    row = session.execute(
        select(AllocationPlan, Task)
        .join(
            Task,
            and_(Task.id == AllocationPlan.task_id, Task.workspace_id == AllocationPlan.workspace_id),
        )
        .where(AllocationPlan.id == plan_id)
    ).first()
    if row is None:
        raise AllocationResourceNotFound("allocation plan was not found")
    return row


def _plan_state(session: Session, plan_id: uuid.UUID) -> AllocationPlanState:
    state = session.scalar(select(AllocationPlanState).where(AllocationPlanState.plan_id == plan_id))
    if state is None:
        raise AllocationResourceNotFound("allocation plan state was not found")
    return state


def _require_plan_ready(session: Session, plan: AllocationPlan) -> None:
    dataset_states = session.scalars(
        select(AllocationDatasetGroupState).where(
            AllocationDatasetGroupState.workspace_id == plan.workspace_id,
            AllocationDatasetGroupState.plan_id == plan.id,
        )
    ).all()
    if not dataset_states or any(
        state.materialization_state != AllocationDatasetState.READY
        or state.argilla_workspace_id is None
        or state.argilla_dataset_id is None
        for state in dataset_states
    ):
        raise AllocationNotReady("every dataset group must be READY and bound before confirmation")
    unbound = session.scalar(
        select(func.count())
        .select_from(AllocationAssignmentItem)
        .outerjoin(
            AllocationRecordBinding,
            AllocationRecordBinding.item_id == AllocationAssignmentItem.id,
        )
        .where(
            AllocationAssignmentItem.workspace_id == plan.workspace_id,
            AllocationAssignmentItem.plan_id == plan.id,
            AllocationRecordBinding.argilla_record_id.is_(None),
        )
    )
    if unbound:
        raise AllocationNotReady("every assignment item must have a remote record binding before confirmation")


def _validate_receipt_binding(
    session: Session,
    context: _AllocationContext,
    plan: AllocationPlan,
    request: AllocationReceiptRequest,
) -> None:
    state = _plan_state(session, plan.id)
    if state.lifecycle_state != AllocationPlanLifecycle.CONFIRMED:
        raise AllocationNotReady("receipts require a confirmed allocation plan")
    if request.connection_binding_id != plan.connection_binding_id:
        raise AllocationRepositoryValidationError("receipt connection binding does not match the plan")
    item = session.scalar(
        select(AllocationAssignmentItem).where(
            AllocationAssignmentItem.workspace_id == plan.workspace_id,
            AllocationAssignmentItem.plan_id == plan.id,
            AllocationAssignmentItem.id == request.item_id,
        )
    )
    if item is None:
        raise AllocationResourceNotFound("assignment item was not found")
    dataset_state = session.scalar(
        select(AllocationDatasetGroupState).where(
            AllocationDatasetGroupState.workspace_id == plan.workspace_id,
            AllocationDatasetGroupState.plan_id == plan.id,
            AllocationDatasetGroupState.dataset_group_id == item.dataset_group_id,
        )
    )
    if dataset_state is None or dataset_state.materialization_state != AllocationDatasetState.READY:
        raise AllocationNotReady("assignment dataset is not READY")
    binding = session.scalar(
        select(AllocationRecordBinding).where(AllocationRecordBinding.item_id == item.id)
    )
    if (
        binding is None
        or binding.argilla_dataset_id != request.argilla_dataset_id
        or binding.argilla_record_id != request.argilla_record_id
        or dataset_state.argilla_dataset_id != request.argilla_dataset_id
    ):
        raise AllocationRepositoryValidationError("receipt remote dataset or record does not match the binding")
    if request.disposition == CollectionDisposition.ACCEPTED:
        assignment = session.scalar(
            select(AllocationAssignment).where(
                AllocationAssignment.workspace_id == plan.workspace_id,
                AllocationAssignment.plan_id == plan.id,
                AllocationAssignment.id == request.assignment_id,
                AllocationAssignment.item_id == item.id,
            )
        )
        if assignment is None:
            raise AllocationRepositoryValidationError("accepted receipt assignment does not match the item")
        if assignment.assignee_mapping_id is not None:
            mapping = session.scalar(
                select(ArgillaAnnotatorMapping).where(
                    ArgillaAnnotatorMapping.id == assignment.assignee_mapping_id,
                    ArgillaAnnotatorMapping.workspace_id == plan.workspace_id,
                    ArgillaAnnotatorMapping.state == AnnotatorMappingState.ACTIVE,
                    ArgillaAnnotatorMapping.verification_state == AnnotatorVerificationState.VERIFIED,
                    ArgillaAnnotatorMapping.argilla_user_id == request.argilla_respondent_id,
                )
            )
            if mapping is None:
                raise AllocationRepositoryValidationError("direct receipt respondent is not the assignee")
        else:
            member = session.scalar(
                select(AnnotatorCohortMember)
                .join(
                    ArgillaAnnotatorMapping,
                    and_(
                        ArgillaAnnotatorMapping.workspace_id == AnnotatorCohortMember.workspace_id,
                        ArgillaAnnotatorMapping.id == AnnotatorCohortMember.annotator_mapping_id,
                    ),
                )
                .where(
                    AnnotatorCohortMember.workspace_id == plan.workspace_id,
                    AnnotatorCohortMember.cohort_revision_id == plan.cohort_revision_id,
                    ArgillaAnnotatorMapping.state == AnnotatorMappingState.ACTIVE,
                    ArgillaAnnotatorMapping.verification_state == AnnotatorVerificationState.VERIFIED,
                    ArgillaAnnotatorMapping.argilla_user_id == request.argilla_respondent_id,
                )
            )
            if member is None or assignment.pool_id is None:
                raise AllocationRepositoryValidationError("pooled receipt respondent is not in the cohort")


def _insert_receipt(
    session: Session,
    context: _AllocationContext,
    plan: AllocationPlan,
    request: AllocationReceiptRequest,
    claim: IdempotencyClaim,
    channel: AuditChannel,
) -> AllocationCollectionReceipt:
    receipt = AllocationCollectionReceipt(
        workspace_id=plan.workspace_id,
        plan_id=plan.id,
        item_id=request.item_id,
        assignment_id=request.assignment_id,
        connection_binding_id=request.connection_binding_id,
        argilla_dataset_id=request.argilla_dataset_id,
        argilla_record_id=request.argilla_record_id,
        argilla_response_id=request.argilla_response_id,
        argilla_respondent_id=request.argilla_respondent_id,
        response_status=request.response_status.strip(),
        disposition=request.disposition,
        rejection_reason=(
            request.rejection_reason.value
            if isinstance(request.rejection_reason, ReceiptRejectionReason)
            else request.rejection_reason
        ),
        labels_hash=request.labels_hash,
        pulled_at=request.pulled_at,
        actor_principal_id=claim.actor_principal_id,
        caller_principal_id=claim.caller_principal_id,
        channel=channel,
        idempotency_record_id=claim.record_id,
    )
    session.add(receipt)
    session.flush()
    return receipt


def _allocation_progress(session: Session, plan_id: uuid.UUID) -> AllocationProgress:
    required = session.scalar(
        select(func.coalesce(func.sum(AllocationAssignment.required_submissions), 0)).where(
            AllocationAssignment.plan_id == plan_id,
        )
    )
    accepted = session.scalar(
        select(func.count()).where(
            AllocationCollectionReceipt.plan_id == plan_id,
            AllocationCollectionReceipt.disposition == CollectionDisposition.ACCEPTED,
        )
    )
    required_count = int(required or 0)
    accepted_count = int(accepted or 0)
    return AllocationProgress(
        plan_id=plan_id,
        accepted_submissions=accepted_count,
        required_submissions=required_count,
        completed=accepted_count >= required_count,
    )


def _page_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 100:
        raise AllocationRepositoryValidationError("limit must be between 1 and 100")
    return value


def _plan_summaries(
    session: Session,
    rows: list[tuple[AllocationPlan, AllocationPlanState]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    plans = [plan for plan, _ in rows]
    plan_ids = [plan.id for plan in plans]
    required_by_plan = {
        plan_id: int(required or 0)
        for plan_id, required in session.execute(
            select(
                AllocationAssignment.plan_id,
                func.coalesce(func.sum(AllocationAssignment.required_submissions), 0),
            )
            .where(AllocationAssignment.plan_id.in_(plan_ids))
            .group_by(AllocationAssignment.plan_id)
        ).all()
    }
    accepted_by_plan = {
        plan_id: int(accepted or 0)
        for plan_id, accepted in session.execute(
            select(
                AllocationCollectionReceipt.plan_id,
                func.count(),
            )
            .where(
                AllocationCollectionReceipt.plan_id.in_(plan_ids),
                AllocationCollectionReceipt.disposition == CollectionDisposition.ACCEPTED,
            )
            .group_by(AllocationCollectionReceipt.plan_id)
        ).all()
    }
    assignment_count_by_plan = {
        plan_id: int(count or 0)
        for plan_id, count in session.execute(
            select(AllocationAssignment.plan_id, func.count())
            .where(AllocationAssignment.plan_id.in_(plan_ids))
            .group_by(AllocationAssignment.plan_id)
        ).all()
    }
    audit_by_plan = _audit_events_by_resource(
        session,
        plans[0].workspace_id,
        "allocation_plan",
        plan_ids,
    )
    progress_by_plan = {
        plan_id: AllocationProgress(
            plan_id=plan_id,
            accepted_submissions=accepted_by_plan.get(plan_id, 0),
            required_submissions=required_by_plan.get(plan_id, 0),
            completed=accepted_by_plan.get(plan_id, 0) >= required_by_plan.get(plan_id, 0),
        )
        for plan_id in plan_ids
    }
    return [
        _plan_summary(
            session,
            plan,
            state,
            progress=progress_by_plan[plan.id],
            assignment_count=assignment_count_by_plan.get(plan.id, 0),
            audit_events=audit_by_plan.get(str(plan.id), []),
        )
        for plan, state in rows
    ]


def _plan_summary(
    session: Session,
    plan: AllocationPlan,
    state: AllocationPlanState,
    *,
    progress: AllocationProgress | None = None,
    assignment_count: int | None = None,
    audit_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if progress is None:
        progress = _allocation_progress(session, plan.id)
    if assignment_count is None:
        assignment_count = int(
            session.scalar(
                select(func.count()).where(
                    AllocationAssignment.workspace_id == plan.workspace_id,
                    AllocationAssignment.plan_id == plan.id,
                )
            )
            or 0
        )
    if audit_events is None:
        audit_events = _audit_events(session, plan.workspace_id, "allocation_plan", plan.id)
    return {
        "kind": "allocation_plan_v1",
        "plan_id": str(plan.id),
        "workspace_id": str(plan.workspace_id),
        "task_id": str(plan.task_id),
        "task_revision_id": str(plan.task_revision_id),
        "cohort_revision_id": str(plan.cohort_revision_id),
        "source_manifest": {
            "manifest_id": plan.source_manifest_id,
            "kind": plan.source_manifest_kind.value,
            "manifest_hash": plan.source_manifest_hash,
        },
        "strategy": plan.strategy.value,
        "schema_version": plan.schema_version,
        "algorithm_version": plan.algorithm_version,
        "fingerprint": plan.fingerprint,
        "input_fingerprint": plan.input_fingerprint,
        "lifecycle_state": state.lifecycle_state.value,
        "assignment_count": assignment_count,
        "progress": _progress_payload(progress),
        "created_at": _datetime_value(plan.created_at),
        "audit_events": audit_events,
    }


def _plan_detail(session: Session, plan: AllocationPlan, state: AllocationPlanState) -> dict[str, Any]:
    payload = _plan_summary(session, plan, state)
    workspace_groups = session.scalars(
        select(AllocationWorkspaceGroup)
        .where(
            AllocationWorkspaceGroup.workspace_id == plan.workspace_id,
            AllocationWorkspaceGroup.plan_id == plan.id,
        )
        .order_by(AllocationWorkspaceGroup.planner_workspace_group_id)
    ).all()
    dataset_rows = session.execute(
        select(AllocationDatasetGroup, AllocationDatasetGroupState)
        .join(
            AllocationDatasetGroupState,
            and_(
                AllocationDatasetGroupState.workspace_id == AllocationDatasetGroup.workspace_id,
                AllocationDatasetGroupState.plan_id == AllocationDatasetGroup.plan_id,
                AllocationDatasetGroupState.dataset_group_id == AllocationDatasetGroup.id,
            ),
        )
        .where(
            AllocationDatasetGroup.workspace_id == plan.workspace_id,
            AllocationDatasetGroup.plan_id == plan.id,
        )
        .order_by(AllocationDatasetGroup.planner_dataset_group_id)
    ).all()
    payload.update(
        {
            "connection_binding_id": str(plan.connection_binding_id),
            "task_revision_hash": plan.task_revision_hash,
            "capacity_snapshot": list(plan.capacity_snapshot),
            "qc_snapshot": dict(plan.qc_snapshot),
            "workspace_groups": [
                {
                    "group_id": str(group.id),
                    "planner_group_id": group.planner_workspace_group_id,
                    "phase": group.phase.value,
                    "mode": group.mode.value,
                    "assignee_mapping_id": (
                        str(group.assignee_mapping_id) if group.assignee_mapping_id is not None else None
                    ),
                    "member_mapping_ids": list(group.member_mapping_ids),
                }
                for group in workspace_groups
            ],
            "dataset_groups": [_dataset_group_payload(group, state_row) for group, state_row in dataset_rows],
            "audit": {
                "actor_principal_id": str(plan.actor_principal_id),
                "caller_principal_id": str(plan.caller_principal_id),
                "channel": plan.channel.value,
                "events": payload["audit_events"],
            },
        }
    )
    return payload


def _dataset_group_payload(group: AllocationDatasetGroup, state: AllocationDatasetGroupState) -> dict[str, Any]:
    return {
        "group_id": str(group.id),
        "planner_group_id": group.planner_dataset_group_id,
        "workspace_group_id": str(group.workspace_group_id),
        "phase": group.phase.value,
        "min_submitted": group.min_submitted,
        "assignee_mapping_id": str(group.assignee_mapping_id) if group.assignee_mapping_id is not None else None,
        "pool_id": str(group.pool_id) if group.pool_id is not None else None,
        "materialization_state": state.materialization_state.value,
        "argilla_workspace_id": str(state.argilla_workspace_id) if state.argilla_workspace_id is not None else None,
        "argilla_dataset_id": str(state.argilla_dataset_id) if state.argilla_dataset_id is not None else None,
        "materialized_at": _datetime_value(state.materialized_at),
        "updated_at": _datetime_value(state.updated_at),
    }


def _assignment_detail(
    session: Session,
    assignment: AllocationAssignment,
    item: AllocationAssignmentItem,
    dataset: AllocationDatasetGroup,
    workspace_group: AllocationWorkspaceGroup,
    record_binding: AllocationRecordBinding | None,
    *,
    receipts: list[AllocationCollectionReceipt] | None = None,
    audit_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if receipts is None:
        receipts = session.scalars(
            select(AllocationCollectionReceipt)
            .where(
                AllocationCollectionReceipt.workspace_id == assignment.workspace_id,
                AllocationCollectionReceipt.plan_id == assignment.plan_id,
                AllocationCollectionReceipt.item_id == item.id,
                or_(
                    AllocationCollectionReceipt.assignment_id == assignment.id,
                    AllocationCollectionReceipt.assignment_id.is_(None),
                ),
            )
            .order_by(AllocationCollectionReceipt.created_at, AllocationCollectionReceipt.id)
        ).all()
    if audit_events is None:
        audit_events = _audit_events(
            session,
            assignment.workspace_id,
            "allocation_assignment",
            assignment.id,
        )
    return {
        "kind": "allocation_assignment_v1",
        "assignment_id": str(assignment.id),
        "planner_assignment_id": assignment.planner_assignment_id,
        "plan_id": str(assignment.plan_id),
        "item_id": str(item.id),
        "record_id": item.record_id,
        "external_id": item.external_id,
        "batch_id": item.batch_id,
        "content_hash": item.content_hash,
        "dataset_group_id": str(dataset.id),
        "planner_dataset_group_id": dataset.planner_dataset_group_id,
        "workspace_group_id": str(workspace_group.id),
        "planner_workspace_group_id": workspace_group.planner_workspace_group_id,
        "phase": assignment.phase.value,
        "role": assignment.role.value,
        "assignee_mapping_id": (
            str(assignment.assignee_mapping_id) if assignment.assignee_mapping_id is not None else None
        ),
        "pool_id": str(assignment.pool_id) if assignment.pool_id is not None else None,
        "required_submissions": assignment.required_submissions,
        "gate_id": assignment.gate_id,
        "remote_binding": (
            {
                "argilla_dataset_id": str(record_binding.argilla_dataset_id),
                "argilla_record_id": str(record_binding.argilla_record_id),
                "bound_at": _datetime_value(record_binding.bound_at),
            }
            if record_binding is not None
            and record_binding.argilla_dataset_id is not None
            and record_binding.argilla_record_id is not None
            else None
        ),
        "receipts": [_receipt_detail(receipt) for receipt in receipts],
        "audit_events": audit_events,
    }


def _assignment_details(
    session: Session,
    rows: list[tuple[
        AllocationAssignment,
        AllocationAssignmentItem,
        AllocationDatasetGroup,
        AllocationWorkspaceGroup,
        AllocationRecordBinding | None,
    ]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    assignments = [row[0] for row in rows]
    items = [row[1] for row in rows]
    assignment_ids = [assignment.id for assignment in assignments]
    item_ids = [item.id for item in items]
    first_assignment = assignments[0]
    receipts = session.scalars(
        select(AllocationCollectionReceipt)
        .where(
            AllocationCollectionReceipt.workspace_id == first_assignment.workspace_id,
            AllocationCollectionReceipt.plan_id == first_assignment.plan_id,
            AllocationCollectionReceipt.item_id.in_(item_ids),
            or_(
                AllocationCollectionReceipt.assignment_id.in_(assignment_ids),
                AllocationCollectionReceipt.assignment_id.is_(None),
            ),
        )
        .order_by(AllocationCollectionReceipt.created_at, AllocationCollectionReceipt.id)
    ).all()
    receipts_by_assignment: dict[uuid.UUID, list[AllocationCollectionReceipt]] = {}
    receipts_by_item: dict[uuid.UUID, list[AllocationCollectionReceipt]] = {}
    for receipt in receipts:
        if receipt.assignment_id is None:
            receipts_by_item.setdefault(receipt.item_id, []).append(receipt)
        else:
            receipts_by_assignment.setdefault(receipt.assignment_id, []).append(receipt)
    audit_by_assignment = _audit_events_by_resource(
        session,
        first_assignment.workspace_id,
        "allocation_assignment",
        assignment_ids,
    )
    result = []
    for assignment, item, dataset, workspace_group, record_binding in rows:
        result.append(
            _assignment_detail(
                session,
                assignment,
                item,
                dataset,
                workspace_group,
                record_binding,
                receipts=(
                    receipts_by_assignment.get(assignment.id, [])
                    + receipts_by_item.get(item.id, [])
                ),
                audit_events=audit_by_assignment.get(str(assignment.id), []),
            )
        )
    return result


def _receipt_detail(receipt: AllocationCollectionReceipt) -> dict[str, Any]:
    return {
        "receipt_id": str(receipt.id),
        "assignment_id": str(receipt.assignment_id) if receipt.assignment_id is not None else None,
        "argilla_response_id": str(receipt.argilla_response_id),
        "argilla_respondent_id": str(receipt.argilla_respondent_id),
        "response_status": receipt.response_status,
        "disposition": receipt.disposition.value,
        "rejection_reason": receipt.rejection_reason,
        "pulled_at": _datetime_value(receipt.pulled_at),
        "created_at": _datetime_value(receipt.created_at),
    }


def _progress_payload(progress: AllocationProgress) -> dict[str, Any]:
    return {
        "accepted_submissions": progress.accepted_submissions,
        "required_submissions": progress.required_submissions,
        "completed": progress.completed,
        "ratio": progress.ratio,
    }


def _audit_events(
    session: Session,
    workspace_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
) -> list[dict[str, Any]]:
    return _audit_events_by_resource(session, workspace_id, resource_type, [resource_id]).get(
        str(resource_id),
        [],
    )


def _audit_events_by_resource(
    session: Session,
    workspace_id: uuid.UUID,
    resource_type: str,
    resource_ids: list[uuid.UUID],
) -> dict[str, list[dict[str, Any]]]:
    if not resource_ids:
        return {}
    events = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.resource_type == resource_type,
            AuditEvent.resource_id.in_([str(resource_id) for resource_id in resource_ids]),
        )
        .order_by(AuditEvent.occurred_at, AuditEvent.id)
    ).all()
    result: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        result.setdefault(event.resource_id or "", []).append(
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "actor_principal_id": str(event.actor_principal_id) if event.actor_principal_id else None,
                "caller_principal_id": str(event.caller_principal_id) if event.caller_principal_id else None,
                "channel": event.channel.value,
                "request_id": event.request_id,
                "details": dict(event.details),
                "occurred_at": _datetime_value(event.occurred_at),
            }
        )
    return result


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _plan_request_payload(request: AllocationRequest, context: _AllocationContext) -> dict[str, Any]:
    return {
        "scope": {
            "workspace_id": str(context.task.workspace_id),
            "task_id": str(context.task.id),
            "task_revision_id": str(context.task_revision.id),
            "cohort_revision_id": str(context.cohort_revision.id),
        },
        "request": _canonical_request(request),
    }


def _canonical_request(request: AllocationRequest) -> dict[str, Any]:
    return {
        "strategy": request.strategy.value,
        "task_revision": {
            "task_id": request.task_revision.task_id,
            "revision_id": request.task_revision.revision_id,
            "revision_hash": request.task_revision.revision_hash,
        },
        "source_manifest": {
            "manifest_id": request.source_manifest.manifest_id,
            "manifest_hash": request.source_manifest.manifest_hash,
            "kind": request.source_manifest.kind.value,
        },
        "records": [
            {
                "record_id": record.record_id,
                "content_hash": record.content_hash,
                "batch_id": record.batch_id,
            }
            for record in request.records
        ],
        "annotators": [
            {
                "annotator_id": annotator.annotator_id,
                "cohort_id": annotator.cohort_id,
                "capacity": annotator.capacity,
            }
            for annotator in request.annotators
        ],
        "seed": request.seed,
        "algorithm_version": request.algorithm_version,
        "overlap_rules": [_overlap_snapshot(rule) for rule in request.overlap_rules],
        "calibration": _calibration_snapshot(request.calibration),
    }


def _receipt_payload(request: AllocationReceiptRequest) -> dict[str, Any]:
    return {
        "plan_id": str(request.plan_id),
        "item_id": str(request.item_id),
        "assignment_id": str(request.assignment_id) if request.assignment_id is not None else None,
        "connection_binding_id": str(request.connection_binding_id),
        "argilla_dataset_id": str(request.argilla_dataset_id),
        "argilla_record_id": str(request.argilla_record_id),
        "argilla_response_id": str(request.argilla_response_id),
        "argilla_respondent_id": str(request.argilla_respondent_id),
        "response_status": request.response_status.strip(),
        "disposition": request.disposition.value,
        "labels_hash": request.labels_hash,
        "pulled_at": request.pulled_at.isoformat(),
        "rejection_reason": (
            request.rejection_reason.value
            if isinstance(request.rejection_reason, ReceiptRejectionReason)
            else request.rejection_reason
        ),
    }


def _plan_response(plan: AllocationPlan, state: AllocationPlanState) -> dict[str, Any]:
    return {
        "kind": "allocation_plan_v1",
        "plan_id": str(plan.id),
        "workspace_id": str(plan.workspace_id),
        "task_id": str(plan.task_id),
        "task_revision_id": str(plan.task_revision_id),
        "cohort_revision_id": str(plan.cohort_revision_id),
        "fingerprint": plan.fingerprint,
        "input_fingerprint": plan.input_fingerprint,
        "lifecycle_state": state.lifecycle_state.value,
    }


def _confirmation_response(state: AllocationPlanState) -> dict[str, Any]:
    return {
        "kind": "allocation_confirmation_v1",
        "plan_id": str(state.plan_id),
        "lifecycle_state": state.lifecycle_state.value,
        "confirmed_at": state.confirmed_at.isoformat() if state.confirmed_at else None,
    }


def _receipt_response(
    receipt: AllocationCollectionReceipt,
    progress: AllocationProgress,
) -> dict[str, Any]:
    return {
        "kind": "allocation_receipt_v1",
        "receipt_id": str(receipt.id),
        "plan_id": str(receipt.plan_id),
        "disposition": receipt.disposition.value,
        "progress": {
            "accepted_submissions": progress.accepted_submissions,
            "required_submissions": progress.required_submissions,
            "completed": progress.completed,
        },
    }


def _plan_result_from_claim(claim: IdempotencyClaim, *, replayed: bool) -> AllocationPlanResult:
    body = _response_body(claim, "allocation_plan_v1", 201)
    return AllocationPlanResult(
        plan_id=_require_uuid(body["plan_id"], "plan_id"),
        workspace_id=_require_uuid(body["workspace_id"], "workspace_id"),
        task_id=_require_uuid(body["task_id"], "task_id"),
        task_revision_id=_require_uuid(body["task_revision_id"], "task_revision_id"),
        cohort_revision_id=_require_uuid(body["cohort_revision_id"], "cohort_revision_id"),
        fingerprint=body["fingerprint"],
        input_fingerprint=body["input_fingerprint"],
        lifecycle_state=AllocationPlanLifecycle(body["lifecycle_state"]),
        response_status=claim.response_status or 201,
        replayed=replayed,
    )


def _confirmation_result_from_claim(
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> AllocationConfirmationResult:
    body = _response_body(claim, "allocation_confirmation_v1", 200)
    return AllocationConfirmationResult(
        plan_id=_require_uuid(body["plan_id"], "plan_id"),
        lifecycle_state=AllocationPlanLifecycle(body["lifecycle_state"]),
        confirmed_at=(
            datetime.fromisoformat(body["confirmed_at"]) if body.get("confirmed_at") is not None else None
        ),
        response_status=claim.response_status or 200,
        replayed=replayed,
    )


def _receipt_result_from_claim(
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> AllocationReceiptResult:
    body = _response_body(claim, "allocation_receipt_v1", 201)
    progress_body = body["progress"]
    progress = AllocationProgress(
        plan_id=_require_uuid(body["plan_id"], "plan_id"),
        accepted_submissions=int(progress_body["accepted_submissions"]),
        required_submissions=int(progress_body["required_submissions"]),
        completed=bool(progress_body["completed"]),
    )
    return AllocationReceiptResult(
        receipt_id=_require_uuid(body["receipt_id"], "receipt_id"),
        plan_id=progress.plan_id,
        disposition=CollectionDisposition(body["disposition"]),
        response_status=claim.response_status or 201,
        replayed=replayed,
        progress=progress,
    )


def _response_body(claim: IdempotencyClaim, kind: str, status: int) -> dict[str, Any]:
    if claim.state.value != "succeeded" or claim.response_status != status or not isinstance(claim.response_body, dict):
        raise IdempotencyConflict()
    if claim.response_body.get("kind") != kind:
        raise IdempotencyConflict()
    return claim.response_body


def _receipt_matches(receipt: AllocationCollectionReceipt, request: AllocationReceiptRequest) -> bool:
    reason = (
        request.rejection_reason.value
        if isinstance(request.rejection_reason, ReceiptRejectionReason)
        else request.rejection_reason
    )
    return (
        receipt.plan_id == request.plan_id
        and receipt.item_id == request.item_id
        and receipt.assignment_id == request.assignment_id
        and receipt.connection_binding_id == request.connection_binding_id
        and receipt.argilla_dataset_id == request.argilla_dataset_id
        and receipt.argilla_record_id == request.argilla_record_id
        and receipt.argilla_response_id == request.argilla_response_id
        and receipt.argilla_respondent_id == request.argilla_respondent_id
        and receipt.response_status == request.response_status.strip()
        and receipt.disposition == request.disposition
        and receipt.rejection_reason == reason
        and receipt.labels_hash == request.labels_hash
        and receipt.pulled_at == request.pulled_at
    )


def _validate_receipt_request(request: AllocationReceiptRequest) -> None:
    if not isinstance(request, AllocationReceiptRequest):
        raise AllocationRepositoryValidationError("request must be an AllocationReceiptRequest")
    for field in (
        "plan_id",
        "item_id",
        "connection_binding_id",
        "argilla_dataset_id",
        "argilla_record_id",
        "argilla_response_id",
        "argilla_respondent_id",
    ):
        _require_uuid(getattr(request, field), field)
    if not isinstance(request.response_status, str) or not request.response_status.strip():
        raise AllocationRepositoryValidationError("response_status must not be blank")
    if not isinstance(request.disposition, CollectionDisposition):
        raise AllocationRepositoryValidationError("disposition must be a CollectionDisposition")
    if not isinstance(request.labels_hash, str) or not _SHA256_RE.fullmatch(request.labels_hash):
        raise AllocationRepositoryValidationError("labels_hash must be a lowercase SHA-256 digest")
    if not isinstance(request.pulled_at, datetime) or request.pulled_at.tzinfo is None:
        raise AllocationRepositoryValidationError("pulled_at must be timezone-aware")
    if request.assignment_id is not None:
        _require_uuid(request.assignment_id, "assignment_id")
    reason = (
        request.rejection_reason.value
        if isinstance(request.rejection_reason, ReceiptRejectionReason)
        else request.rejection_reason
    )
    if request.disposition == CollectionDisposition.ACCEPTED:
        if request.assignment_id is None or reason is not None:
            raise AllocationRepositoryValidationError("accepted receipt requires assignment_id and no rejection reason")
    elif request.assignment_id is not None or reason not in _REJECTION_REASON_CODES:
        raise AllocationRepositoryValidationError("quarantined receipt requires an allowed rejection reason")


def _request_from_plan_context(plan: AllocationPlan, revision: AnnotatorCohortRevision) -> AllocationRequest:
    return AllocationRequest(
        strategy=PlannerAllocationStrategy(plan.strategy.value),
        task_revision=TaskRevisionRef(
            task_id=str(plan.task_id),
            revision_id=str(plan.task_revision_id),
            revision_hash=plan.task_revision_hash,
        ),
        source_manifest=SourceManifestRef(
            manifest_id=plan.source_manifest_id,
            manifest_hash=plan.source_manifest_hash,
            kind=PlannerManifestKind(plan.source_manifest_kind.value),
        ),
        records=(),
        annotators=(),
        seed=int(plan.seed),
        algorithm_version=plan.algorithm_version,
    )


def _calibration_snapshot(rule: CalibrationRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "record_ids": list(rule.record_ids),
        "count": rule.count,
        "rate": rule.rate,
        "expected_annotator_ids": (
            list(rule.expected_annotator_ids) if rule.expected_annotator_ids is not None else None
        ),
        "exclude_from_production": rule.exclude_from_production,
    }


def _overlap_snapshot(rule: OverlapRule) -> dict[str, Any]:
    return {
        "record_ids": list(rule.record_ids),
        "count": rule.count,
        "rate": rule.rate,
        "required_submissions": rule.required_submissions,
    }


def _append_plan_audit(
    session: Session,
    context: _AllocationContext,
    plan: AllocationPlan,
    *,
    channel: AuditChannel,
    request_id: str | None,
    replayed: bool,
) -> None:
    append_audit_event(
        session,
        workspace_id=plan.workspace_id,
        event_type="allocation.plan.reused" if replayed else "allocation.plan.created",
        actor=context.actor,
        caller=context.caller,
        channel=channel,
        resource_type="allocation_plan",
        resource_id=plan.id,
        request_id=request_id,
        details={"fingerprint": plan.fingerprint, "input_fingerprint": plan.input_fingerprint},
    )


def _require_write_channel(channel: AuditChannel) -> None:
    if not isinstance(channel, AuditChannel) or channel == AuditChannel.SYSTEM:
        raise AllocationRepositoryValidationError("allocation writes require a non-system audit channel")


def _require_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise AllocationRepositoryValidationError(f"{field} must be a UUID") from exc
    raise AllocationRepositoryValidationError(f"{field} must be a UUID")


def _optional_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    return _require_uuid(value, "planner identifier")
