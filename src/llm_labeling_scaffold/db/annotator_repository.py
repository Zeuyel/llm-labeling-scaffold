from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import append_audit_event
from .database import create_database_engine, create_session_factory
from .enums import (
    AnnotatorCohortState,
    AnnotatorMappingState,
    AnnotatorVerificationState,
    ArgillaBindingState,
    AuditChannel,
    IdempotencyState,
)
from .models import (
    AnnotatorCohort,
    AnnotatorCohortMember,
    AnnotatorCohortRevision,
    ArgillaAnnotatorMapping,
    ArgillaConnectionBinding,
    Principal,
    Workspace,
)
from .rbac import Permission
from .service import (
    AuthorizationUnavailable,
    DatabaseTransaction,
    ExternalIdentity,
    IdempotencyClaim,
    IdempotencyClaimStatus,
    IdempotencyConflict,
)


class AnnotatorRepositoryError(RuntimeError):
    pass


class AnnotatorRepositoryValidationError(ValueError):
    pass


class AnnotatorResourceNotFound(AnnotatorRepositoryError):
    pass


class AnnotatorNaturalKeyConflict(AnnotatorRepositoryError):
    pass


class AnnotatorOperationInProgress(AnnotatorRepositoryError):
    pass


class AnnotatorNotReady(AnnotatorRepositoryError):
    pass


class AnnotatorVerificationRejected(AnnotatorRepositoryError):
    def __init__(self, reason: str, mapping_id: uuid.UUID | None = None) -> None:
        self.reason = reason
        self.mapping_id = mapping_id
        super().__init__(reason)


AnnotatorVerificationError = AnnotatorVerificationRejected


@dataclass(frozen=True, slots=True)
class CohortMemberInput:
    annotator_mapping_id: uuid.UUID
    default_capacity: int


@dataclass(frozen=True, slots=True)
class WorkspaceBindingRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    connection_config_id: str
    server_version: str
    argilla_workspace_id: uuid.UUID | None
    argilla_workspace_name: str | None
    state: ArgillaBindingState


@dataclass(frozen=True, slots=True)
class AnnotatorMappingRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    connection_binding_id: uuid.UUID
    principal_id: uuid.UUID | None
    principal_issuer: str | None
    principal_subject: str | None
    principal_display_name: str | None
    argilla_user_id: uuid.UUID | None
    username: str
    personal_argilla_workspace_id: uuid.UUID | None
    state: AnnotatorMappingState
    verification_state: AnnotatorVerificationState
    verified_at: datetime | None
    argilla_role: str | None = None
    membership_verified: bool | None = None

    @property
    def recent_verification_at(self) -> datetime | None:
        return self.verified_at


@dataclass(frozen=True, slots=True)
class CohortMemberRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    cohort_revision_id: uuid.UUID
    annotator_mapping_id: uuid.UUID
    default_capacity: int
    mapping: AnnotatorMappingRef


@dataclass(frozen=True, slots=True)
class CohortRevisionRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    cohort_id: uuid.UUID
    revision_number: int
    fingerprint: str
    member_count: int
    connection_binding_id: uuid.UUID | None
    sealed_at: datetime | None
    members: tuple[CohortMemberRef, ...]

    @property
    def immutable(self) -> bool:
        return self.sealed_at is not None


@dataclass(frozen=True, slots=True)
class CohortRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    state: AnnotatorCohortState
    latest_revision: CohortRevisionRef | None


@dataclass(frozen=True, slots=True)
class WorkspaceBindingResult:
    binding: WorkspaceBindingRef
    response_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class AnnotatorMappingResult:
    mapping: AnnotatorMappingRef
    response_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class AnnotatorVerificationResult:
    mapping: AnnotatorMappingRef
    verified: bool
    reason: str | None
    response_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class CohortRevisionResult:
    revision: CohortRevisionRef
    response_status: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class CohortResult:
    cohort: CohortRef
    revision: CohortRevisionRef
    response_status: int
    replayed: bool


class AnnotatorControlRepository:
    def __init__(self, session_factory, engine=None) -> None:
        self._session_factory = session_factory
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str | None = None) -> AnnotatorControlRepository:
        engine = create_database_engine(database_url)
        return cls(create_session_factory(engine), engine)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            yield session

    def list_workspace_bindings(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        include_disabled: bool = False,
    ) -> tuple[WorkspaceBindingRef, ...]:
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            statement = select(ArgillaConnectionBinding).where(
                ArgillaConnectionBinding.workspace_id == workspace.id,
            )
            if not include_disabled:
                statement = statement.where(ArgillaConnectionBinding.state == ArgillaBindingState.ACTIVE)
            return tuple(
                _binding_ref(row)
                for row in session.scalars(
                    statement.order_by(ArgillaConnectionBinding.connection_config_id),
                ).all()
            )

    def get_workspace_binding(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        binding_id: uuid.UUID | str,
    ) -> WorkspaceBindingRef | None:
        normalized_id = _uuid(binding_id, "binding_id")
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            row = session.scalar(
                select(ArgillaConnectionBinding).where(
                    ArgillaConnectionBinding.workspace_id == workspace.id,
                    ArgillaConnectionBinding.id == normalized_id,
                ),
            )
            return _binding_ref(row) if row is not None else None

    def ensure_workspace_binding(
        self,
        *,
        workspace_slug: str,
        connection_config_id: str,
        server_version: str,
        actor_identity: ExternalIdentity,
        idempotency_key: str,
        caller_identity: ExternalIdentity | None = None,
        argilla_workspace_id: uuid.UUID | str | None = None,
        argilla_workspace_name: str | None = None,
        channel: AuditChannel = AuditChannel.API,
        request_id: str | None = None,
    ) -> WorkspaceBindingResult:
        config_id = _text(connection_config_id, "connection_config_id")
        version = _text(server_version, "server_version")
        remote_id = _optional_uuid(argilla_workspace_id, "argilla_workspace_id")
        remote_name = _optional_text(argilla_workspace_name, "argilla_workspace_name")
        _paired_remote_values(remote_id, remote_name)
        caller = caller_identity or actor_identity
        payload = {
            "connection_config_id": config_id,
            "server_version": version,
            "argilla_workspace_id": str(remote_id) if remote_id is not None else None,
            "argilla_workspace_name": remote_name,
        }
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.workspace_binding.ensure",
                idempotency_key=idempotency_key,
                request_payload=payload,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                binding = _binding_from_claim(session, claim)
                return WorkspaceBindingResult(binding, claim.response_status or 200, True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AnnotatorOperationInProgress("workspace binding is already in progress")

            workspace = _workspace_from_claim(session, claim, workspace_slug, lock=True)
            binding = session.scalar(
                select(ArgillaConnectionBinding)
                .where(
                    ArgillaConnectionBinding.workspace_id == workspace.id,
                    ArgillaConnectionBinding.connection_config_id == config_id,
                )
                .with_for_update(),
            )
            created = binding is None
            if binding is None:
                binding = ArgillaConnectionBinding(
                    workspace_id=workspace.id,
                    connection_config_id=config_id,
                    server_version=version,
                    argilla_workspace_id=remote_id,
                    argilla_workspace_name_snapshot=remote_name,
                    state=ArgillaBindingState.ACTIVE,
                    idempotency_record_id=claim.record_id,
                )
                try:
                    with session.begin_nested():
                        session.add(binding)
                        session.flush()
                except IntegrityError:
                    binding = session.scalar(
                        select(ArgillaConnectionBinding)
                        .where(
                            ArgillaConnectionBinding.workspace_id == workspace.id,
                            ArgillaConnectionBinding.connection_config_id == config_id,
                        )
                        .with_for_update(),
                    )
                    if binding is None:
                        raise
            if binding.state != ArgillaBindingState.ACTIVE:
                raise AnnotatorNotReady("workspace binding is disabled")
            if binding.argilla_workspace_id is not None and (
                binding.argilla_workspace_id != remote_id
                or binding.argilla_workspace_name_snapshot != remote_name
            ):
                raise AnnotatorNaturalKeyConflict("workspace binding identity differs")
            if binding.argilla_workspace_id is None and remote_id is not None:
                binding.argilla_workspace_id = remote_id
                binding.argilla_workspace_name_snapshot = remote_name
            binding.server_version = version
            session.flush()
            response = _binding_response(binding)
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller,
                response_status=201 if created else 200,
                response_body=response,
                request_id=request_id,
            )
            _audit(
                session,
                claim,
                "annotator.workspace_binding.ensured",
                "argilla_connection_binding",
                binding.id,
                request_id,
                {"connection_config_id": config_id, "reused": not created},
            )
            return WorkspaceBindingResult(
                _binding_ref(binding),
                completed.response_status or (201 if created else 200),
                False,
            )

    bind_workspace = ensure_workspace_binding
    create_binding = ensure_workspace_binding
    update_workspace_binding = ensure_workspace_binding
    list_bindings = list_workspace_bindings
    get_binding = get_workspace_binding

    def list_annotators(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        binding_id: uuid.UUID | str | None = None,
        include_disabled: bool = False,
    ) -> tuple[AnnotatorMappingRef, ...]:
        normalized_binding_id = (
            _optional_uuid(binding_id, "binding_id") if binding_id is not None else None
        )
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            statement = (
                select(ArgillaAnnotatorMapping, Principal)
                .outerjoin(Principal, Principal.id == ArgillaAnnotatorMapping.principal_id)
                .where(ArgillaAnnotatorMapping.workspace_id == workspace.id)
            )
            if normalized_binding_id is not None:
                statement = statement.where(
                    ArgillaAnnotatorMapping.connection_binding_id == normalized_binding_id,
                )
            if not include_disabled:
                statement = statement.where(ArgillaAnnotatorMapping.state == AnnotatorMappingState.ACTIVE)
            return tuple(
                _mapping_ref(mapping, principal)
                for mapping, principal in session.execute(
                    statement.order_by(ArgillaAnnotatorMapping.username_snapshot),
                ).all()
            )

    list_mappings = list_annotators

    def get_annotator(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        mapping_id: uuid.UUID | str,
    ) -> AnnotatorMappingRef | None:
        normalized_id = _uuid(mapping_id, "mapping_id")
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            row = session.execute(
                select(ArgillaAnnotatorMapping, Principal)
                .outerjoin(Principal, Principal.id == ArgillaAnnotatorMapping.principal_id)
                .where(
                    ArgillaAnnotatorMapping.workspace_id == workspace.id,
                    ArgillaAnnotatorMapping.id == normalized_id,
                ),
            ).first()
            return _mapping_ref(*row) if row is not None else None

    get_mapping = get_annotator

    def create_annotator_mapping(
        self,
        *,
        workspace_slug: str,
        binding_id: uuid.UUID | str,
        principal_identity: ExternalIdentity,
        username: str,
        actor_identity: ExternalIdentity,
        idempotency_key: str,
        argilla_user_id: uuid.UUID | str | None = None,
        personal_argilla_workspace_id: uuid.UUID | str | None = None,
        argilla_role: str = "annotator",
        membership_verified: bool = False,
        initial_password: str | None = None,
        caller_identity: ExternalIdentity | None = None,
        channel: AuditChannel = AuditChannel.API,
        request_id: str | None = None,
    ) -> AnnotatorMappingResult:
        del initial_password
        normalized_binding_id = _uuid(binding_id, "binding_id")
        remote_user_id = _optional_uuid(argilla_user_id, "argilla_user_id")
        personal_workspace_id = _optional_uuid(
            personal_argilla_workspace_id,
            "personal_argilla_workspace_id",
        )
        normalized_username = _text(username, "username")
        _require_annotator_role(argilla_role)
        if type(membership_verified) is not bool:
            raise AnnotatorRepositoryValidationError("membership_verified must be a boolean")
        if membership_verified and (
            remote_user_id is None or personal_workspace_id is None
        ):
            raise AnnotatorRepositoryValidationError(
                "verified mapping requires Argilla user and personal workspace UUIDs",
            )
        caller = caller_identity or actor_identity
        payload = {
            "binding_id": str(normalized_binding_id),
            "principal_issuer": principal_identity.issuer,
            "principal_subject": principal_identity.subject,
            "username": normalized_username,
            "argilla_user_id": str(remote_user_id) if remote_user_id is not None else None,
            "personal_argilla_workspace_id": (
                str(personal_workspace_id) if personal_workspace_id is not None else None
            ),
            "membership_verified": membership_verified,
            "argilla_role": "annotator",
        }
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.mapping.create",
                idempotency_key=idempotency_key,
                request_payload=payload,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                mapping = _mapping_from_claim(session, claim)
                return AnnotatorMappingResult(mapping, claim.response_status or 200, True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AnnotatorOperationInProgress("annotator mapping is already in progress")

            workspace = _workspace_from_claim(session, claim, workspace_slug, lock=True)
            binding = session.scalar(
                select(ArgillaConnectionBinding)
                .where(
                    ArgillaConnectionBinding.workspace_id == workspace.id,
                    ArgillaConnectionBinding.id == normalized_binding_id,
                )
                .with_for_update(),
            )
            if binding is None:
                raise AnnotatorResourceNotFound("workspace binding was not found")
            if binding.state != ArgillaBindingState.ACTIVE:
                raise AnnotatorNotReady("workspace binding is disabled")
            principal_ref = transaction.resolve_or_provision(principal_identity)
            principal = session.scalar(select(Principal).where(Principal.id == principal_ref.id))
            if principal is None or not principal.is_active:
                raise AnnotatorResourceNotFound("annotator principal is not active")
            existing = session.scalar(
                select(ArgillaAnnotatorMapping)
                .where(
                    ArgillaAnnotatorMapping.workspace_id == workspace.id,
                    ArgillaAnnotatorMapping.connection_binding_id == binding.id,
                    ArgillaAnnotatorMapping.principal_id == principal.id,
                )
                .with_for_update(),
            )
            if remote_user_id is not None:
                remote_existing = session.scalar(
                    select(ArgillaAnnotatorMapping)
                    .where(
                        ArgillaAnnotatorMapping.workspace_id == workspace.id,
                        ArgillaAnnotatorMapping.connection_binding_id == binding.id,
                        ArgillaAnnotatorMapping.argilla_user_id == remote_user_id,
                    )
                    .with_for_update(),
                )
                if remote_existing is not None and (
                    existing is None or remote_existing.id != existing.id
                ):
                    raise AnnotatorNaturalKeyConflict("Argilla user is already mapped")
            created = existing is None
            if existing is None:
                existing = ArgillaAnnotatorMapping(
                    workspace_id=workspace.id,
                    connection_binding_id=binding.id,
                    principal_id=principal.id,
                    argilla_user_id=remote_user_id,
                    username_snapshot=normalized_username,
                    personal_argilla_workspace_id=personal_workspace_id,
                    state=AnnotatorMappingState.ACTIVE,
                    verification_state=(
                        AnnotatorVerificationState.VERIFIED
                        if membership_verified
                        else AnnotatorVerificationState.UNVERIFIED
                    ),
                    verified_by_principal_id=(
                        claim.actor_principal_id if membership_verified else None
                    ),
                    verified_at=datetime.now(timezone.utc) if membership_verified else None,
                    idempotency_record_id=claim.record_id,
                )
                session.add(existing)
                session.flush()
            else:
                if existing.state != AnnotatorMappingState.ACTIVE:
                    raise AnnotatorNotReady("annotator mapping is disabled")
                if (
                    remote_user_id is not None
                    and existing.argilla_user_id is not None
                    and remote_user_id != existing.argilla_user_id
                ):
                    raise AnnotatorNaturalKeyConflict("Argilla user identity differs")
                if (
                    personal_workspace_id is not None
                    and existing.personal_argilla_workspace_id is not None
                    and personal_workspace_id != existing.personal_argilla_workspace_id
                ):
                    raise AnnotatorNaturalKeyConflict("personal workspace identity differs")
                if (
                    existing.argilla_user_id is not None
                    and existing.username_snapshot != normalized_username
                ):
                    raise AnnotatorNaturalKeyConflict("Argilla username identity differs")
                if existing.argilla_user_id is None and remote_user_id is not None:
                    existing.argilla_user_id = remote_user_id
                if (
                    existing.personal_argilla_workspace_id is None
                    and personal_workspace_id is not None
                ):
                    existing.personal_argilla_workspace_id = personal_workspace_id
                if existing.argilla_user_id is None:
                    existing.username_snapshot = normalized_username
                if membership_verified:
                    existing.verification_state = AnnotatorVerificationState.VERIFIED
                    existing.verified_by_principal_id = claim.actor_principal_id
                    existing.verified_at = datetime.now(timezone.utc)
                session.flush()
            response = _mapping_response(existing)
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller,
                response_status=201 if created else 200,
                response_body=response,
                request_id=request_id,
            )
            _audit(
                session,
                claim,
                "annotator.mapping.created" if created else "annotator.mapping.reused",
                "argilla_annotator_mapping",
                existing.id,
                request_id,
                {
                    "principal_id": str(principal.id),
                    "argilla_user_id": (
                        str(existing.argilla_user_id)
                        if existing.argilla_user_id is not None
                        else None
                    ),
                    "verification_state": existing.verification_state.value,
                },
            )
            return AnnotatorMappingResult(
                _mapping_ref(existing, principal, role="annotator"),
                completed.response_status or (201 if created else 200),
                False,
            )

    bind_annotator = create_annotator_mapping
    create_mapping = create_annotator_mapping
    create_annotator = create_annotator_mapping

    def verify_annotator(
        self,
        *,
        workspace_slug: str,
        mapping_id: uuid.UUID | str,
        actor_identity: ExternalIdentity,
        idempotency_key: str,
        argilla_user_id: uuid.UUID | str | None = None,
        username: str | None = None,
        personal_argilla_workspace_id: uuid.UUID | str | None = None,
        membership_verified: bool = False,
        argilla_role: str = "annotator",
        caller_identity: ExternalIdentity | None = None,
        channel: AuditChannel = AuditChannel.API,
        request_id: str | None = None,
    ) -> AnnotatorVerificationResult:
        normalized_mapping_id = _uuid(mapping_id, "mapping_id")
        remote_user_id = _optional_uuid(argilla_user_id, "argilla_user_id")
        personal_workspace_id = _optional_uuid(
            personal_argilla_workspace_id,
            "personal_argilla_workspace_id",
        )
        normalized_username = _optional_text(username, "username")
        normalized_role = _role_text(argilla_role)
        if type(membership_verified) is not bool:
            raise AnnotatorRepositoryValidationError("membership_verified must be a boolean")
        caller = caller_identity or actor_identity
        payload = {
            "mapping_id": str(normalized_mapping_id),
            "argilla_user_id": str(remote_user_id) if remote_user_id is not None else None,
            "username": normalized_username,
            "personal_argilla_workspace_id": (
                str(personal_workspace_id) if personal_workspace_id is not None else None
            ),
            "membership_verified": membership_verified,
            "argilla_role": normalized_role,
        }
        rejected_reason: str | None = None
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.mapping.verify",
                idempotency_key=idempotency_key,
                request_payload=payload,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                if claim.state == IdempotencyState.FAILED:
                    raise AnnotatorVerificationRejected(
                        _claim_reason(claim),
                        normalized_mapping_id,
                    )
                return AnnotatorVerificationResult(
                    _mapping_from_claim(session, claim),
                    True,
                    None,
                    claim.response_status or 200,
                    True,
                )
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AnnotatorOperationInProgress("annotator verification is already in progress")

            workspace = _workspace_from_claim(session, claim, workspace_slug, lock=True)
            row = session.execute(
                select(ArgillaAnnotatorMapping, Principal)
                .outerjoin(Principal, Principal.id == ArgillaAnnotatorMapping.principal_id)
                .where(
                    ArgillaAnnotatorMapping.workspace_id == workspace.id,
                    ArgillaAnnotatorMapping.id == normalized_mapping_id,
                )
                .with_for_update(),
            ).first()
            if row is None:
                raise AnnotatorResourceNotFound("annotator mapping was not found")
            mapping, principal = row
            binding = session.scalar(
                select(ArgillaConnectionBinding)
                .where(
                    ArgillaConnectionBinding.workspace_id == workspace.id,
                    ArgillaConnectionBinding.id == mapping.connection_binding_id,
                )
                .with_for_update(),
            )
            if mapping.state != AnnotatorMappingState.ACTIVE:
                rejected_reason = "mapping_disabled"
            elif binding is None or binding.state != ArgillaBindingState.ACTIVE:
                rejected_reason = "workspace_binding_unavailable"
            elif normalized_role != "annotator":
                rejected_reason = "role_not_annotator"
            elif mapping.argilla_user_id is not None and (
                remote_user_id is None or mapping.argilla_user_id != remote_user_id
            ):
                rejected_reason = "argilla_user_id_mismatch"
            elif mapping.argilla_user_id is None and remote_user_id is None:
                rejected_reason = "argilla_user_id_missing"
            elif normalized_username is None:
                rejected_reason = "username_missing"
            elif normalized_username != mapping.username_snapshot:
                rejected_reason = "username_mismatch"
            elif mapping.personal_argilla_workspace_id is not None and (
                personal_workspace_id is None
                or mapping.personal_argilla_workspace_id != personal_workspace_id
            ):
                rejected_reason = "personal_workspace_id_mismatch"
            elif personal_workspace_id is None:
                rejected_reason = "personal_workspace_id_missing"
            elif not membership_verified:
                rejected_reason = "workspace_membership_missing"

            if rejected_reason is None and remote_user_id is not None:
                duplicate = session.scalar(
                    select(ArgillaAnnotatorMapping)
                    .where(
                        ArgillaAnnotatorMapping.workspace_id == workspace.id,
                        ArgillaAnnotatorMapping.connection_binding_id
                        == mapping.connection_binding_id,
                        ArgillaAnnotatorMapping.argilla_user_id == remote_user_id,
                        ArgillaAnnotatorMapping.id != mapping.id,
                    )
                    .with_for_update(),
                )
                if duplicate is not None:
                    rejected_reason = "argilla_user_id_already_mapped"

            if rejected_reason is not None:
                mapping.verification_state = AnnotatorVerificationState.REJECTED
                mapping.verified_by_principal_id = None
                mapping.verified_at = None
                session.flush()
                response = {
                    "mapping_id": str(mapping.id),
                    "verification_state": AnnotatorVerificationState.REJECTED.value,
                    "reason": rejected_reason,
                }
                transaction.complete_idempotency(
                    claim,
                    actor_identity=actor_identity,
                    caller_identity=caller,
                    response_status=409,
                    response_body=response,
                    succeeded=False,
                    request_id=request_id,
                )
                _audit(
                    session,
                    claim,
                    "annotator.mapping.verification_rejected",
                    "argilla_annotator_mapping",
                    mapping.id,
                    request_id,
                    {
                        "verification_state": AnnotatorVerificationState.REJECTED.value,
                        "reason": rejected_reason,
                    },
                )
            else:
                if mapping.argilla_user_id is None:
                    mapping.argilla_user_id = remote_user_id
                if mapping.personal_argilla_workspace_id is None:
                    mapping.personal_argilla_workspace_id = personal_workspace_id
                mapping.verification_state = AnnotatorVerificationState.VERIFIED
                mapping.verified_by_principal_id = claim.actor_principal_id
                mapping.verified_at = datetime.now(timezone.utc)
                session.flush()
                response = _mapping_response(mapping)
                completed = transaction.complete_idempotency(
                    claim,
                    actor_identity=actor_identity,
                    caller_identity=caller,
                    response_status=200,
                    response_body=response,
                    request_id=request_id,
                )
                _audit(
                    session,
                    claim,
                    "annotator.mapping.verified",
                    "argilla_annotator_mapping",
                    mapping.id,
                    request_id,
                    {
                        "verification_state": AnnotatorVerificationState.VERIFIED.value,
                        "verified_at": mapping.verified_at.isoformat(),
                    },
                )
                return AnnotatorVerificationResult(
                    _mapping_ref(mapping, principal, role="annotator", membership_verified=True),
                    True,
                    None,
                    completed.response_status or 200,
                    False,
                )
        raise AnnotatorVerificationRejected(rejected_reason or "verification_rejected", normalized_mapping_id)

    verify_mapping = verify_annotator
    update_annotator = verify_annotator
    update_mapping = verify_annotator

    def create_cohort(
        self,
        *,
        workspace_slug: str,
        name: str,
        binding_id: uuid.UUID | str,
        members: Iterable[CohortMemberInput | tuple[uuid.UUID | str, int] | dict[str, Any]],
        actor_identity: ExternalIdentity,
        idempotency_key: str,
        caller_identity: ExternalIdentity | None = None,
        channel: AuditChannel = AuditChannel.API,
        request_id: str | None = None,
    ) -> CohortResult:
        normalized_name = _text(name, "name")
        normalized_binding_id = _uuid(binding_id, "binding_id")
        normalized_members = _member_inputs(members)
        caller = caller_identity or actor_identity
        payload = _cohort_payload(normalized_name, normalized_binding_id, normalized_members)
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.cohort.create",
                idempotency_key=idempotency_key,
                request_payload=payload,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                cohort, revision = _cohort_from_claim(session, claim)
                return CohortResult(cohort, revision, claim.response_status or 200, True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AnnotatorOperationInProgress("cohort creation is already in progress")

            workspace = _workspace_from_claim(session, claim, workspace_slug, lock=True)
            existing = session.scalar(
                select(AnnotatorCohort)
                .where(
                    AnnotatorCohort.workspace_id == workspace.id,
                    AnnotatorCohort.name == normalized_name,
                )
                .with_for_update(),
            )
            fingerprint = _cohort_fingerprint(normalized_binding_id, normalized_members)
            if existing is not None:
                revision = _latest_revision(session, workspace.id, existing.id)
                if revision is None or revision.fingerprint != fingerprint:
                    raise AnnotatorNaturalKeyConflict("cohort name is already in use")
                response = {"cohort_id": str(existing.id), "revision_id": str(revision.id)}
                completed = transaction.complete_idempotency(
                    claim,
                    actor_identity=actor_identity,
                    caller_identity=caller,
                    response_status=200,
                    response_body=response,
                    request_id=request_id,
                )
                _audit(
                    session,
                    claim,
                    "annotator.cohort.reused",
                    "annotator_cohort",
                    existing.id,
                    request_id,
                    {"revision_id": str(revision.id)},
                )
                return CohortResult(
                    _cohort_ref(session, existing),
                    _revision_ref(session, revision),
                    completed.response_status or 200,
                    False,
                )

            cohort = AnnotatorCohort(
                workspace_id=workspace.id,
                name=normalized_name,
                state=AnnotatorCohortState.ACTIVE,
                idempotency_record_id=claim.record_id,
            )
            session.add(cohort)
            session.flush()
            revision_claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.cohort.revision.create",
                idempotency_key=f"{idempotency_key}:revision",
                request_payload={"cohort_id": str(cohort.id), **payload},
                channel=channel,
            )
            revision, reused = self._persist_revision(
                session,
                cohort=cohort,
                binding_id=normalized_binding_id,
                members=normalized_members,
                claim=revision_claim,
            )
            if revision_claim.status == IdempotencyClaimStatus.CLAIMED:
                transaction.complete_idempotency(
                    revision_claim,
                    actor_identity=actor_identity,
                    caller_identity=caller,
                    response_status=201 if not reused else 200,
                    response_body=_revision_response(revision),
                    request_id=request_id,
                )
                _audit(
                    session,
                    revision_claim,
                    "annotator.cohort.revision.created",
                    "annotator_cohort_revision",
                    revision.id,
                    request_id,
                    {"cohort_id": str(cohort.id), "member_count": revision.member_count},
                )
            response = {"cohort_id": str(cohort.id), "revision_id": str(revision.id)}
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller,
                response_status=201,
                response_body=response,
                request_id=request_id,
            )
            _audit(
                session,
                claim,
                "annotator.cohort.created",
                "annotator_cohort",
                cohort.id,
                request_id,
                {"revision_id": str(revision.id), "member_count": revision.member_count},
            )
            return CohortResult(
                _cohort_ref(session, cohort),
                _revision_ref(session, revision),
                completed.response_status or 201,
                False,
            )

    def create_cohort_revision(
        self,
        *,
        workspace_slug: str,
        cohort_id: uuid.UUID | str,
        binding_id: uuid.UUID | str,
        members: Iterable[CohortMemberInput | tuple[uuid.UUID | str, int] | dict[str, Any]],
        actor_identity: ExternalIdentity,
        idempotency_key: str,
        caller_identity: ExternalIdentity | None = None,
        channel: AuditChannel = AuditChannel.API,
        request_id: str | None = None,
    ) -> CohortRevisionResult:
        normalized_cohort_id = _uuid(cohort_id, "cohort_id")
        normalized_binding_id = _uuid(binding_id, "binding_id")
        normalized_members = _member_inputs(members)
        caller = caller_identity or actor_identity
        payload = {
            "cohort_id": str(normalized_cohort_id),
            "binding_id": str(normalized_binding_id),
            "members": _member_payload(normalized_members),
        }
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.cohort.revision.create",
                idempotency_key=idempotency_key,
                request_payload=payload,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                revision = _revision_from_claim(session, claim)
                return CohortRevisionResult(revision, claim.response_status or 200, True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AnnotatorOperationInProgress("cohort revision creation is already in progress")
            workspace = _workspace_from_claim(session, claim, workspace_slug, lock=True)
            cohort = session.scalar(
                select(AnnotatorCohort)
                .where(
                    AnnotatorCohort.workspace_id == workspace.id,
                    AnnotatorCohort.id == normalized_cohort_id,
                )
                .with_for_update(),
            )
            if cohort is None:
                raise AnnotatorResourceNotFound("cohort was not found")
            revision, reused = self._persist_revision(
                session,
                cohort=cohort,
                binding_id=normalized_binding_id,
                members=normalized_members,
                claim=claim,
            )
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller,
                response_status=200 if reused else 201,
                response_body=_revision_response(revision),
                request_id=request_id,
            )
            _audit(
                session,
                claim,
                "annotator.cohort.revision.reused" if reused else "annotator.cohort.revision.created",
                "annotator_cohort_revision",
                revision.id,
                request_id,
                {"cohort_id": str(cohort.id), "reused": reused},
            )
            return CohortRevisionResult(
                _revision_ref(session, revision),
                completed.response_status or (200 if reused else 201),
                False,
            )

    def update_cohort(
        self,
        *,
        workspace_slug: str,
        cohort_id: uuid.UUID | str,
        name: str | None,
        default_capacity: int | None,
        expected_revision: int | None,
        actor_identity: ExternalIdentity,
        idempotency_key: str,
        caller_identity: ExternalIdentity | None = None,
        channel: AuditChannel = AuditChannel.API,
        request_id: str | None = None,
    ) -> CohortResult:
        normalized_cohort_id = _uuid(cohort_id, "cohort_id")
        normalized_name = _optional_text(name, "name")
        if default_capacity is not None and (
            type(default_capacity) is not int or default_capacity <= 0
        ):
            raise AnnotatorRepositoryValidationError(
                "default_capacity must be a positive integer",
            )
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise AnnotatorRepositoryValidationError(
                "expected_revision must be a non-negative integer",
            )
        caller = caller_identity or actor_identity
        payload = {
            "cohort_id": str(normalized_cohort_id),
            "name": normalized_name,
            "default_capacity": default_capacity,
            "expected_revision": expected_revision,
        }
        with self._transaction() as session:
            transaction = DatabaseTransaction(session)
            claim = _claim(
                transaction,
                actor_identity=actor_identity,
                caller_identity=caller,
                workspace_slug=workspace_slug,
                operation="annotator.cohort.update",
                idempotency_key=idempotency_key,
                request_payload=payload,
                channel=channel,
            )
            if claim.status == IdempotencyClaimStatus.REPLAY:
                cohort, revision = _cohort_from_claim(session, claim)
                return CohortResult(cohort, revision, claim.response_status or 200, True)
            if claim.status == IdempotencyClaimStatus.PENDING:
                raise AnnotatorOperationInProgress("cohort update is already in progress")

            workspace = _workspace_from_claim(session, claim, workspace_slug, lock=True)
            cohort = session.scalar(
                select(AnnotatorCohort)
                .where(
                    AnnotatorCohort.workspace_id == workspace.id,
                    AnnotatorCohort.id == normalized_cohort_id,
                )
                .with_for_update(),
            )
            if cohort is None:
                raise AnnotatorResourceNotFound("cohort was not found")
            revision = _latest_revision(session, workspace.id, cohort.id)
            if revision is None:
                raise AnnotatorNotReady("cohort has no revision")
            if expected_revision is not None and revision.revision_number != expected_revision:
                raise AnnotatorNaturalKeyConflict("cohort revision differs")

            target_name = normalized_name or cohort.name
            if target_name != cohort.name:
                duplicate = session.scalar(
                    select(AnnotatorCohort)
                    .where(
                        AnnotatorCohort.workspace_id == workspace.id,
                        AnnotatorCohort.name == target_name,
                        AnnotatorCohort.id != cohort.id,
                    )
                    .with_for_update(),
                )
                if duplicate is not None:
                    raise AnnotatorNaturalKeyConflict("cohort name is already in use")

            binding_id = revision.connection_binding_id
            if binding_id is None:
                raise AnnotatorNotReady("cohort revision has no active binding")
            current_members = session.scalars(
                select(AnnotatorCohortMember)
                .where(
                    AnnotatorCohortMember.workspace_id == workspace.id,
                    AnnotatorCohortMember.cohort_revision_id == revision.id,
                )
                .order_by(AnnotatorCohortMember.annotator_mapping_id),
            ).all()
            members = tuple(
                CohortMemberInput(
                    member.annotator_mapping_id,
                    default_capacity
                    if default_capacity is not None
                    else member.default_capacity,
                )
                for member in current_members
            )
            new_revision, reused = self._persist_revision(
                session,
                cohort=cohort,
                binding_id=binding_id,
                members=members,
                claim=claim,
            )
            name_changed = target_name != cohort.name
            cohort.name = target_name
            session.flush()
            response = {
                "cohort_id": str(cohort.id),
                "revision_id": str(new_revision.id),
            }
            completed = transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller,
                response_status=200 if reused else 201,
                response_body=response,
                request_id=request_id,
            )
            _audit(
                session,
                claim,
                "annotator.cohort.updated",
                "annotator_cohort",
                cohort.id,
                request_id,
                {
                    "revision_id": str(new_revision.id),
                    "reused": reused,
                    "name_changed": name_changed,
                },
            )
            return CohortResult(
                _cohort_ref(session, cohort),
                _revision_ref(session, new_revision),
                completed.response_status or (200 if reused else 201),
                False,
            )

    replace_cohort_members = create_cohort_revision
    update_cohort_members = create_cohort_revision
    replace_members = create_cohort_revision

    def list_cohorts(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        include_archived: bool = False,
    ) -> tuple[CohortRef, ...]:
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            statement = select(AnnotatorCohort).where(AnnotatorCohort.workspace_id == workspace.id)
            if not include_archived:
                statement = statement.where(AnnotatorCohort.state == AnnotatorCohortState.ACTIVE)
            return tuple(
                _cohort_ref(session, cohort)
                for cohort in session.scalars(statement.order_by(AnnotatorCohort.name)).all()
            )

    def get_cohort(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        cohort_id: uuid.UUID | str,
    ) -> CohortRef | None:
        normalized_id = _uuid(cohort_id, "cohort_id")
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            cohort = session.scalar(
                select(AnnotatorCohort).where(
                    AnnotatorCohort.workspace_id == workspace.id,
                    AnnotatorCohort.id == normalized_id,
                ),
            )
            return _cohort_ref(session, cohort) if cohort is not None else None

    def list_cohort_revisions(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        cohort_id: uuid.UUID | str,
    ) -> tuple[CohortRevisionRef, ...]:
        normalized_id = _uuid(cohort_id, "cohort_id")
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            rows = session.scalars(
                select(AnnotatorCohortRevision)
                .where(
                    AnnotatorCohortRevision.workspace_id == workspace.id,
                    AnnotatorCohortRevision.cohort_id == normalized_id,
                )
                .order_by(AnnotatorCohortRevision.revision_number),
            ).all()
            return tuple(_revision_ref(session, row) for row in rows)

    def get_cohort_revision(
        self,
        *,
        workspace_slug: str,
        identity: ExternalIdentity,
        revision_id: uuid.UUID | str,
    ) -> CohortRevisionRef | None:
        normalized_id = _uuid(revision_id, "revision_id")
        with self._transaction() as session:
            workspace = self._authorize(session, identity, workspace_slug)
            revision = session.scalar(
                select(AnnotatorCohortRevision).where(
                    AnnotatorCohortRevision.workspace_id == workspace.id,
                    AnnotatorCohortRevision.id == normalized_id,
                ),
            )
            return _revision_ref(session, revision) if revision is not None else None

    list_revisions = list_cohort_revisions
    get_revision = get_cohort_revision

    def _authorize(
        self,
        session: Session,
        identity: ExternalIdentity,
        workspace_slug: str,
    ) -> Workspace:
        normalized_slug = _text(workspace_slug, "workspace_slug")
        transaction = DatabaseTransaction(session)
        decision = transaction.authorize_workspace(
            identity,
            normalized_slug,
            Permission.WORKSPACE_MANAGE,
        )
        transaction.require(decision)
        if decision.workspace is None:
            raise AnnotatorResourceNotFound("workspace was not found")
        workspace = session.scalar(
            select(Workspace).where(
                Workspace.id == decision.workspace.id,
                Workspace.slug == normalized_slug,
            ),
        )
        if workspace is None:
            raise AuthorizationUnavailable("authorized workspace disappeared")
        return workspace

    def _persist_revision(
        self,
        session: Session,
        *,
        cohort: AnnotatorCohort,
        binding_id: uuid.UUID,
        members: tuple[CohortMemberInput, ...],
        claim: IdempotencyClaim,
    ) -> tuple[AnnotatorCohortRevision, bool]:
        fingerprint = _cohort_fingerprint(binding_id, members)
        existing = session.scalar(
            select(AnnotatorCohortRevision)
            .where(
                AnnotatorCohortRevision.workspace_id == cohort.workspace_id,
                AnnotatorCohortRevision.cohort_id == cohort.id,
                AnnotatorCohortRevision.fingerprint == fingerprint,
            )
            .with_for_update(),
        )
        if existing is not None:
            return existing, True
        if cohort.state != AnnotatorCohortState.ACTIVE:
            raise AnnotatorNotReady("cohort is archived")
        binding = session.scalar(
            select(ArgillaConnectionBinding)
            .where(
                ArgillaConnectionBinding.workspace_id == cohort.workspace_id,
                ArgillaConnectionBinding.id == binding_id,
            )
            .with_for_update(),
        )
        if binding is None:
            raise AnnotatorResourceNotFound("workspace binding was not found")
        if (
            binding.state != ArgillaBindingState.ACTIVE
            or binding.argilla_workspace_id is None
            or binding.argilla_workspace_name_snapshot is None
        ):
            raise AnnotatorNotReady("workspace binding is not ready")
        mapping_ids = [member.annotator_mapping_id for member in members]
        mappings = session.scalars(
            select(ArgillaAnnotatorMapping)
            .where(
                ArgillaAnnotatorMapping.workspace_id == cohort.workspace_id,
                ArgillaAnnotatorMapping.connection_binding_id == binding_id,
                ArgillaAnnotatorMapping.id.in_(mapping_ids),
                ArgillaAnnotatorMapping.state == AnnotatorMappingState.ACTIVE,
                ArgillaAnnotatorMapping.verification_state == AnnotatorVerificationState.VERIFIED,
                ArgillaAnnotatorMapping.argilla_user_id.is_not(None),
            )
            .with_for_update(),
        ).all()
        if len({mapping.id for mapping in mappings}) != len(mapping_ids):
            raise AnnotatorNotReady("cohort members must be verified annotator mappings")
        next_number = (
            session.scalar(
                select(func.coalesce(func.max(AnnotatorCohortRevision.revision_number), 0)).where(
                    AnnotatorCohortRevision.workspace_id == cohort.workspace_id,
                    AnnotatorCohortRevision.cohort_id == cohort.id,
                ),
            )
            + 1
        )
        revision = AnnotatorCohortRevision(
            workspace_id=cohort.workspace_id,
            cohort_id=cohort.id,
            revision_number=next_number,
            fingerprint=fingerprint,
            member_count=len(members),
            actor_principal_id=claim.actor_principal_id,
            caller_principal_id=claim.caller_principal_id,
            channel=claim.channel,
            idempotency_record_id=claim.record_id,
        )
        try:
            with session.begin_nested():
                session.add(revision)
                session.flush()
                session.add_all(
                    [
                        AnnotatorCohortMember(
                            workspace_id=cohort.workspace_id,
                            cohort_revision_id=revision.id,
                            annotator_mapping_id=member.annotator_mapping_id,
                            default_capacity=member.default_capacity,
                        )
                        for member in members
                    ],
                )
                session.flush()
                revision.connection_binding_id = binding.id
                revision.sealed_at = datetime.now(timezone.utc)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(AnnotatorCohortRevision)
                .where(
                    AnnotatorCohortRevision.workspace_id == cohort.workspace_id,
                    AnnotatorCohortRevision.cohort_id == cohort.id,
                    AnnotatorCohortRevision.fingerprint == fingerprint,
                )
                .with_for_update(),
            )
            if existing is None:
                raise
            return existing, True
        return revision, False


def _claim(
    transaction: DatabaseTransaction,
    *,
    actor_identity: ExternalIdentity,
    caller_identity: ExternalIdentity,
    workspace_slug: str,
    operation: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    channel: AuditChannel,
) -> IdempotencyClaim:
    return transaction.claim_idempotency(
        actor_identity=actor_identity,
        caller_identity=caller_identity,
        workspace_slug=workspace_slug,
        required_permission=Permission.WORKSPACE_MANAGE,
        operation=operation,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        channel=channel,
    )


def _workspace_from_claim(
    session: Session,
    claim: IdempotencyClaim,
    workspace_slug: str,
    *,
    lock: bool,
) -> Workspace:
    statement = select(Workspace).where(
        Workspace.id == claim.workspace_id,
        Workspace.slug == _text(workspace_slug, "workspace_slug"),
    )
    if lock:
        statement = statement.with_for_update()
    workspace = session.scalar(statement)
    if workspace is None:
        raise AuthorizationUnavailable("authorized workspace disappeared")
    return workspace


def _audit(
    session: Session,
    claim: IdempotencyClaim,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID,
    request_id: str | None,
    details: dict[str, Any],
) -> None:
    actor = session.scalar(select(Principal).where(Principal.id == claim.actor_principal_id))
    caller = session.scalar(select(Principal).where(Principal.id == claim.caller_principal_id))
    if actor is None or caller is None:
        raise AuthorizationUnavailable("operation principals disappeared")
    append_audit_event(
        session,
        workspace_id=claim.workspace_id,
        event_type=event_type,
        actor=actor,
        caller=caller,
        channel=claim.channel,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        details=details,
    )


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnotatorRepositoryValidationError(f"{field} must not be blank")
    return value.strip()


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AnnotatorRepositoryValidationError(f"{field} must be a UUID") from exc


def _optional_uuid(value: uuid.UUID | str | None, field: str) -> uuid.UUID | None:
    return None if value is None else _uuid(value, field)


def _paired_remote_values(
    remote_id: uuid.UUID | None,
    remote_name: str | None,
) -> None:
    if (remote_id is None) != (remote_name is None):
        raise AnnotatorRepositoryValidationError(
            "Argilla workspace UUID and name must be supplied together",
        )


def _role_text(value: str) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _require_annotator_role(value: str) -> None:
    if _role_text(value) != "annotator":
        raise AnnotatorRepositoryValidationError("Argilla role must be annotator")


def _member_inputs(
    members: Iterable[CohortMemberInput | tuple[uuid.UUID | str, int] | dict[str, Any]],
) -> tuple[CohortMemberInput, ...]:
    normalized: list[CohortMemberInput] = []
    seen: set[uuid.UUID] = set()
    for raw in members:
        if isinstance(raw, CohortMemberInput):
            mapping_id = _uuid(raw.annotator_mapping_id, "annotator_mapping_id")
            capacity = raw.default_capacity
        elif isinstance(raw, tuple) and len(raw) == 2:
            mapping_id = _uuid(raw[0], "annotator_mapping_id")
            capacity = raw[1]
        elif isinstance(raw, dict):
            mapping_id = _uuid(
                raw.get("annotator_mapping_id", raw.get("mapping_id")),
                "annotator_mapping_id",
            )
            capacity = raw.get("default_capacity", raw.get("capacity"))
        else:
            raise AnnotatorRepositoryValidationError("invalid cohort member")
        if type(capacity) is not int or capacity < 0:
            raise AnnotatorRepositoryValidationError(
                "default_capacity must be a non-negative integer",
            )
        if mapping_id in seen:
            raise AnnotatorRepositoryValidationError("cohort members must be unique")
        seen.add(mapping_id)
        normalized.append(CohortMemberInput(mapping_id, capacity))
    if not normalized:
        raise AnnotatorRepositoryValidationError("cohort must contain at least one member")
    return tuple(sorted(normalized, key=lambda item: str(item.annotator_mapping_id)))


def _member_payload(members: tuple[CohortMemberInput, ...]) -> list[dict[str, Any]]:
    return [
        {
            "annotator_mapping_id": str(member.annotator_mapping_id),
            "default_capacity": member.default_capacity,
        }
        for member in members
    ]


def _cohort_payload(
    name: str,
    binding_id: uuid.UUID,
    members: tuple[CohortMemberInput, ...],
) -> dict[str, Any]:
    return {
        "name": name,
        "binding_id": str(binding_id),
        "members": _member_payload(members),
    }


def _cohort_fingerprint(
    binding_id: uuid.UUID,
    members: tuple[CohortMemberInput, ...],
) -> str:
    payload = {
        "binding_id": str(binding_id),
        "members": _member_payload(members),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()


def _binding_ref(binding: ArgillaConnectionBinding) -> WorkspaceBindingRef:
    return WorkspaceBindingRef(
        id=binding.id,
        workspace_id=binding.workspace_id,
        connection_config_id=binding.connection_config_id,
        server_version=binding.server_version,
        argilla_workspace_id=binding.argilla_workspace_id,
        argilla_workspace_name=binding.argilla_workspace_name_snapshot,
        state=binding.state,
    )


def _mapping_ref(
    mapping: ArgillaAnnotatorMapping,
    principal: Principal | None,
    *,
    role: str | None = None,
    membership_verified: bool | None = None,
) -> AnnotatorMappingRef:
    return AnnotatorMappingRef(
        id=mapping.id,
        workspace_id=mapping.workspace_id,
        connection_binding_id=mapping.connection_binding_id,
        principal_id=mapping.principal_id,
        principal_issuer=principal.issuer if principal is not None else None,
        principal_subject=principal.subject if principal is not None else None,
        principal_display_name=principal.display_name if principal is not None else None,
        argilla_user_id=mapping.argilla_user_id,
        username=mapping.username_snapshot,
        personal_argilla_workspace_id=mapping.personal_argilla_workspace_id,
        state=mapping.state,
        verification_state=mapping.verification_state,
        verified_at=mapping.verified_at,
        argilla_role=role,
        membership_verified=membership_verified,
    )


def _cohort_ref(session: Session, cohort: AnnotatorCohort) -> CohortRef:
    revision = _latest_revision(session, cohort.workspace_id, cohort.id)
    return CohortRef(
        id=cohort.id,
        workspace_id=cohort.workspace_id,
        name=cohort.name,
        state=cohort.state,
        latest_revision=_revision_ref(session, revision) if revision is not None else None,
    )


def _latest_revision(
    session: Session,
    workspace_id: uuid.UUID,
    cohort_id: uuid.UUID,
) -> AnnotatorCohortRevision | None:
    return session.scalar(
        select(AnnotatorCohortRevision)
        .where(
            AnnotatorCohortRevision.workspace_id == workspace_id,
            AnnotatorCohortRevision.cohort_id == cohort_id,
        )
        .order_by(AnnotatorCohortRevision.revision_number.desc())
        .limit(1),
    )


def _revision_ref(
    session: Session,
    revision: AnnotatorCohortRevision,
) -> CohortRevisionRef:
    rows = session.execute(
        select(AnnotatorCohortMember, ArgillaAnnotatorMapping, Principal)
        .join(
            ArgillaAnnotatorMapping,
            (ArgillaAnnotatorMapping.workspace_id == AnnotatorCohortMember.workspace_id)
            & (ArgillaAnnotatorMapping.id == AnnotatorCohortMember.annotator_mapping_id),
        )
        .outerjoin(Principal, Principal.id == ArgillaAnnotatorMapping.principal_id)
        .where(
            AnnotatorCohortMember.workspace_id == revision.workspace_id,
            AnnotatorCohortMember.cohort_revision_id == revision.id,
        )
        .order_by(AnnotatorCohortMember.annotator_mapping_id),
    ).all()
    members = tuple(
        CohortMemberRef(
            id=member.id,
            workspace_id=member.workspace_id,
            cohort_revision_id=member.cohort_revision_id,
            annotator_mapping_id=member.annotator_mapping_id,
            default_capacity=member.default_capacity,
            mapping=_mapping_ref(mapping, principal),
        )
        for member, mapping, principal in rows
    )
    return CohortRevisionRef(
        id=revision.id,
        workspace_id=revision.workspace_id,
        cohort_id=revision.cohort_id,
        revision_number=revision.revision_number,
        fingerprint=revision.fingerprint,
        member_count=revision.member_count,
        connection_binding_id=revision.connection_binding_id,
        sealed_at=revision.sealed_at,
        members=members,
    )


def _binding_response(binding: ArgillaConnectionBinding) -> dict[str, Any]:
    return {
        "binding_id": str(binding.id),
        "workspace_id": str(binding.workspace_id),
        "connection_config_id": binding.connection_config_id,
        "server_version": binding.server_version,
        "argilla_workspace_id": (
            str(binding.argilla_workspace_id)
            if binding.argilla_workspace_id is not None
            else None
        ),
        "argilla_workspace_name": binding.argilla_workspace_name_snapshot,
        "state": binding.state.value,
    }


def _mapping_response(mapping: ArgillaAnnotatorMapping) -> dict[str, Any]:
    return {
        "mapping_id": str(mapping.id),
        "workspace_id": str(mapping.workspace_id),
        "binding_id": str(mapping.connection_binding_id),
        "principal_id": str(mapping.principal_id) if mapping.principal_id is not None else None,
        "argilla_user_id": (
            str(mapping.argilla_user_id) if mapping.argilla_user_id is not None else None
        ),
        "username": mapping.username_snapshot,
        "personal_argilla_workspace_id": (
            str(mapping.personal_argilla_workspace_id)
            if mapping.personal_argilla_workspace_id is not None
            else None
        ),
        "state": mapping.state.value,
        "verification_state": mapping.verification_state.value,
        "verified_at": (
            mapping.verified_at.isoformat() if mapping.verified_at is not None else None
        ),
    }


def _revision_response(revision: AnnotatorCohortRevision) -> dict[str, Any]:
    return {
        "revision_id": str(revision.id),
        "workspace_id": str(revision.workspace_id),
        "cohort_id": str(revision.cohort_id),
        "revision_number": revision.revision_number,
        "fingerprint": revision.fingerprint,
        "member_count": revision.member_count,
        "binding_id": (
            str(revision.connection_binding_id)
            if revision.connection_binding_id is not None
            else None
        ),
        "sealed_at": (
            revision.sealed_at.isoformat() if revision.sealed_at is not None else None
        ),
    }


def _response_uuid(body: dict[str, Any] | None, key: str) -> uuid.UUID:
    if not isinstance(body, dict) or key not in body:
        raise IdempotencyConflict()
    return _uuid(body[key], key)


def _binding_from_claim(
    session: Session,
    claim: IdempotencyClaim,
) -> WorkspaceBindingRef:
    binding_id = _response_uuid(claim.response_body, "binding_id")
    binding = session.scalar(
        select(ArgillaConnectionBinding).where(
            ArgillaConnectionBinding.workspace_id == claim.workspace_id,
            ArgillaConnectionBinding.id == binding_id,
        ),
    )
    if binding is None:
        raise IdempotencyConflict()
    return _binding_ref(binding)


def _mapping_from_claim(
    session: Session,
    claim: IdempotencyClaim,
) -> AnnotatorMappingRef:
    mapping_id = _response_uuid(claim.response_body, "mapping_id")
    row = session.execute(
        select(ArgillaAnnotatorMapping, Principal)
        .outerjoin(Principal, Principal.id == ArgillaAnnotatorMapping.principal_id)
        .where(
            ArgillaAnnotatorMapping.workspace_id == claim.workspace_id,
            ArgillaAnnotatorMapping.id == mapping_id,
        ),
    ).first()
    if row is None:
        raise IdempotencyConflict()
    return _mapping_ref(*row)


def _revision_from_claim(
    session: Session,
    claim: IdempotencyClaim,
) -> CohortRevisionRef:
    revision_id = _response_uuid(claim.response_body, "revision_id")
    revision = session.scalar(
        select(AnnotatorCohortRevision).where(
            AnnotatorCohortRevision.workspace_id == claim.workspace_id,
            AnnotatorCohortRevision.id == revision_id,
        ),
    )
    if revision is None:
        raise IdempotencyConflict()
    return _revision_ref(session, revision)


def _cohort_from_claim(
    session: Session,
    claim: IdempotencyClaim,
) -> tuple[CohortRef, CohortRevisionRef]:
    cohort_id = _response_uuid(claim.response_body, "cohort_id")
    revision_id = _response_uuid(claim.response_body, "revision_id")
    cohort = session.scalar(
        select(AnnotatorCohort).where(
            AnnotatorCohort.workspace_id == claim.workspace_id,
            AnnotatorCohort.id == cohort_id,
        ),
    )
    revision = session.scalar(
        select(AnnotatorCohortRevision).where(
            AnnotatorCohortRevision.workspace_id == claim.workspace_id,
            AnnotatorCohortRevision.id == revision_id,
            AnnotatorCohortRevision.cohort_id == cohort_id,
        ),
    )
    if cohort is None or revision is None:
        raise IdempotencyConflict()
    return _cohort_ref(session, cohort), _revision_ref(session, revision)


def _claim_reason(claim: IdempotencyClaim) -> str:
    body = claim.response_body
    reason = body.get("reason") if isinstance(body, dict) else None
    return reason if isinstance(reason, str) and reason else "verification_rejected"


AnnotatorRepository = AnnotatorControlRepository
AnnotatorCohortRepository = AnnotatorControlRepository
ArgillaBindingRef = WorkspaceBindingRef
ArgillaAnnotatorMappingRef = AnnotatorMappingRef
AnnotatorCohortMemberInput = CohortMemberInput
AnnotatorCohortMemberRef = CohortMemberRef
AnnotatorCohortRevisionRef = CohortRevisionRef
