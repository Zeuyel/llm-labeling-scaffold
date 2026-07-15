from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any
from uuid import UUID

from .argilla import _api_url, _load_argilla, _runtime_versions


_ARGILLA_ANNOTATOR_ROLE = "annotator"
_ARGILLA_OPERATOR_ROLES = {"admin", "owner"}
_ARGILLA_WORKSPACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ARGILLA_USERNAME_PREFIX = "lls_u_"
_ARGILLA_WORKSPACE_PREFIX = "lls_personal_"


class ArgillaProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ArgillaUserDTO:
    uuid: str
    username: str
    role: str
    first_name: str | None
    last_name: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "uuid": self.uuid,
            "username": self.username,
            "role": self.role,
            "first_name": self.first_name,
            "last_name": self.last_name,
        }


@dataclass(frozen=True, slots=True)
class ArgillaWorkspaceDTO:
    uuid: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"uuid": self.uuid, "name": self.name}


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningAction:
    user: str
    workspace: str
    membership: str

    def to_dict(self) -> dict[str, str]:
        return {
            "user": self.user,
            "workspace": self.workspace,
            "membership": self.membership,
        }


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningDTO:
    user: ArgillaUserDTO
    workspace: ArgillaWorkspaceDTO
    verification_state: str
    verified_at: str
    action: ArgillaProvisioningAction

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": self.user.to_dict(),
            "workspace": self.workspace.to_dict(),
            "verification_state": self.verification_state,
            "verified_at": self.verified_at,
            "action": self.action.to_dict(),
        }


class _EphemeralPassword:
    __slots__ = ("_value",)

    def __init__(self, value: str | None) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "_EphemeralPassword(<redacted>)"

    def take(self) -> str | None:
        value = self._value
        self._value = None
        return value

    def clear(self) -> None:
        self._value = None


def _principal_digest(issuer: str, subject: str) -> str:
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError("principal issuer 不能为空")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("principal subject 不能为空")
    payload = json.dumps([issuer, subject], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_argilla_username(issuer: str, subject: str) -> str:
    return f"{_ARGILLA_USERNAME_PREFIX}{_principal_digest(issuer, subject)[:32]}"


def derive_personal_workspace_name(issuer: str, subject: str) -> str:
    name = f"{_ARGILLA_WORKSPACE_PREFIX}{_principal_digest(issuer, subject)[:32]}"
    if not _ARGILLA_WORKSPACE_NAME_PATTERN.fullmatch(name):
        raise ValueError("派生的 Argilla personal workspace name 包含非法字符")
    return name


def _role_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ArgillaProvisioningError("invalid_remote_uuid", f"Argilla {label} 缺少有效 UUID") from exc


def _expected_uuid(value: UUID | str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} 必须是有效 UUID") from exc


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _verified_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clear_sdk_password(resource: Any) -> bool:
    if resource is None:
        return True
    try:
        resource.password = None
        return getattr(resource, "password", None) is None
    except Exception:
        return False


def _new_client_from_env(rg: Any, api_url: str) -> Any:
    api_key = os.environ.get("ARGILLA_API_KEY")
    if not api_key:
        raise ArgillaProvisioningError(
            "missing_owner_api_key",
            "owner API key 必须通过 ARGILLA_API_KEY secret/env 提供",
        )

    client = None
    failed = False
    try:
        client = rg.Argilla(api_url=api_url, api_key=api_key)
    except Exception:
        failed = True
    finally:
        api_key = None

    if failed or client is None:
        raise ArgillaProvisioningError(
            "client_initialization_failed",
            "无法使用 ARGILLA_API_KEY 初始化 Argilla 2.8 owner client",
        ) from None
    return client


class ArgillaAdminAdapter:
    def __init__(self, *, api_url: str | None = None) -> None:
        self._rg = _load_argilla()
        resolved_api_url = _api_url(api_url)
        _runtime_versions(self._rg, resolved_api_url)
        self._client = _new_client_from_env(self._rg, resolved_api_url)

        me = self._read_operator()
        self._operator_role = _role_value(getattr(me, "role", None))
        self._operator_uuid = _canonical_uuid(getattr(me, "id", None), "operator user")
        if self._operator_role not in _ARGILLA_OPERATOR_ROLES:
            raise ArgillaProvisioningError(
                "insufficient_operator_role",
                "Argilla provisioning client.me role 必须是 admin 或 owner",
            )

    def __repr__(self) -> str:
        return f"ArgillaAdminAdapter(operator_role={self._operator_role!r})"

    def ensure_annotator(
        self,
        *,
        principal_issuer: str,
        principal_subject: str,
        password: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        expected_user_uuid: UUID | str | None = None,
        expected_workspace_uuid: UUID | str | None = None,
    ) -> ArgillaProvisioningDTO:
        secret = _EphemeralPassword(password)
        password = None
        try:
            username = derive_argilla_username(principal_issuer, principal_subject)
            workspace_name = derive_personal_workspace_name(principal_issuer, principal_subject)
            expected_user_id = _expected_uuid(expected_user_uuid, "expected_user_uuid")
            expected_workspace_id = _expected_uuid(expected_workspace_uuid, "expected_workspace_uuid")

            user, user_action = self._ensure_user(
                username=username,
                first_name=first_name,
                last_name=last_name,
                expected_uuid=expected_user_id,
                secret=secret,
            )
            user_dto = self._user_dto(user, username=username, expected_uuid=expected_user_id)
            workspace, workspace_action = self._ensure_workspace(
                name=workspace_name,
                expected_uuid=expected_workspace_id,
            )
            workspace_dto = self._workspace_dto(
                workspace,
                name=workspace_name,
                expected_uuid=expected_workspace_id,
            )
            membership_action = self._ensure_membership(
                workspace=workspace,
                user=user,
                user_dto=user_dto,
            )
            return ArgillaProvisioningDTO(
                user=user_dto,
                workspace=workspace_dto,
                verification_state="verified",
                verified_at=_verified_at(),
                action=ArgillaProvisioningAction(
                    user=user_action,
                    workspace=workspace_action,
                    membership=membership_action,
                ),
            )
        finally:
            secret.clear()

    def _read_operator(self) -> Any:
        operator = None
        failed = False
        try:
            operator = self._client.me
        except Exception:
            failed = True
        if failed or operator is None:
            raise ArgillaProvisioningError(
                "operator_lookup_failed",
                "无法通过 Argilla 2.8 public client.me 验证 provisioning operator",
            ) from None
        return operator

    def _ensure_user(
        self,
        *,
        username: str,
        first_name: str | None,
        last_name: str | None,
        expected_uuid: str | None,
        secret: _EphemeralPassword,
    ) -> tuple[Any, str]:
        if expected_uuid is not None:
            secret.clear()
            user = self._user_by_uuid(expected_uuid)
            if user is None:
                raise ArgillaProvisioningError(
                    "expected_user_missing",
                    "expected Argilla user UUID 不存在；拒绝按同名 username 回退",
                )
            self._user_dto(user, username=username, expected_uuid=expected_uuid)
            return user, "reused"

        existing = self._user_by_username(username)
        if existing is not None:
            secret.clear()
            self._user_dto(existing, username=username)
            return existing, "reused"

        password = secret.take()
        if not isinstance(password, str) or not 8 <= len(password) <= 100:
            password = None
            raise ArgillaProvisioningError(
                "creation_password_required",
                "首次创建 Argilla annotator 需要 8 到 100 字符的一次性密码",
            )

        normalized_first_name = first_name.strip() if isinstance(first_name, str) and first_name.strip() else username
        normalized_last_name = last_name.strip() if isinstance(last_name, str) and last_name.strip() else None
        candidate = None
        construction_failed = False
        try:
            candidate = self._rg.User(
                username=username,
                role=_ARGILLA_ANNOTATOR_ROLE,
                first_name=normalized_first_name,
                last_name=normalized_last_name,
                password=password,
                client=self._client,
            )
        except Exception:
            construction_failed = True
        finally:
            password = None

        if construction_failed or candidate is None:
            raise ArgillaProvisioningError(
                "user_resource_initialization_failed",
                f"无法初始化 Argilla 2.8 annotator resource: {username}",
            ) from None

        created = None
        create_failed = False
        try:
            created = candidate.create()
        except Exception:
            create_failed = True
        finally:
            clear_failed = not _clear_sdk_password(candidate)
            if created is not candidate:
                clear_failed = not _clear_sdk_password(created) or clear_failed

        if clear_failed:
            candidate = None
            created = None
            raise ArgillaProvisioningError(
                "password_clear_failed",
                "Argilla User.create() 返回后无法通过 public password 属性清除一次性密码",
            ) from None

        if create_failed:
            recovered = self._user_by_username(username)
            if recovered is None:
                raise ArgillaProvisioningError(
                    "user_creation_failed",
                    f"Argilla annotator 创建失败且重读未发现资源: {username}",
                ) from None
            self._user_dto(recovered, username=username)
            return recovered, "recovered"

        self._user_dto(created, username=username)
        return created, "created"

    def _ensure_workspace(self, *, name: str, expected_uuid: str | None) -> tuple[Any, str]:
        if expected_uuid is not None:
            workspace = self._workspace_by_uuid(expected_uuid)
            if workspace is None:
                raise ArgillaProvisioningError(
                    "expected_workspace_missing",
                    "expected Argilla workspace UUID 不存在；拒绝按同名 workspace 回退",
                )
            self._workspace_dto(workspace, name=name, expected_uuid=expected_uuid)
            return workspace, "reused"

        existing = self._workspace_by_name(name)
        if existing is not None:
            self._workspace_dto(existing, name=name)
            return existing, "reused"

        candidate = None
        construction_failed = False
        try:
            candidate = self._rg.Workspace(name=name, client=self._client)
        except Exception:
            construction_failed = True
        if construction_failed or candidate is None:
            raise ArgillaProvisioningError(
                "workspace_resource_initialization_failed",
                f"无法初始化 Argilla 2.8 personal workspace resource: {name}",
            ) from None

        created = None
        create_failed = False
        try:
            created = candidate.create()
        except Exception:
            create_failed = True

        if create_failed:
            recovered = self._workspace_by_name(name)
            if recovered is None:
                raise ArgillaProvisioningError(
                    "workspace_visibility_conflict",
                    f"personal workspace 创建失败且同名资源重读不可见: {name}",
                ) from None
            self._workspace_dto(recovered, name=name)
            return recovered, "recovered"

        self._workspace_dto(created, name=name)
        return created, "created"

    def _ensure_membership(
        self,
        *,
        workspace: Any,
        user: Any,
        user_dto: ArgillaUserDTO,
    ) -> str:
        members = self._workspace_members(workspace)
        if self._verified_member(members, user_dto) is not None:
            return "reused"

        add_failed = False
        try:
            workspace.add_user(user)
        except Exception:
            add_failed = True

        members = self._workspace_members(workspace)
        if self._verified_member(members, user_dto) is not None:
            return "recovered" if add_failed else "created"
        if add_failed:
            raise ArgillaProvisioningError(
                "membership_creation_failed",
                "Argilla workspace membership 添加失败且重读未确认目标成员",
            ) from None
        raise ArgillaProvisioningError(
            "membership_verification_failed",
            "Argilla workspace membership 写入后重读未确认目标成员",
        )

    def _user_by_uuid(self, user_uuid: str) -> Any | None:
        user = None
        failed = False
        try:
            user = self._client.users(id=user_uuid)
        except Exception:
            failed = True
        if failed:
            raise ArgillaProvisioningError(
                "user_lookup_failed",
                f"无法通过 Argilla 2.8 public users(id=...) 查询 user UUID: {user_uuid}",
            ) from None
        return user

    def _user_by_username(self, username: str) -> Any | None:
        users = None
        failed = False
        try:
            users = list(self._client.users.list())
        except Exception:
            failed = True
        if failed or users is None:
            raise ArgillaProvisioningError(
                "user_list_failed",
                "无法通过 Argilla 2.8 public users.list() 查询 annotator",
            ) from None

        matches = [item for item in users if _text_or_none(getattr(item, "username", None)) == username]
        matches.sort(key=lambda item: _text_or_none(getattr(item, "id", None)) or "")
        if len(matches) > 1:
            ids = ",".join(_canonical_uuid(getattr(item, "id", None), "user") for item in matches)
            raise ArgillaProvisioningError(
                "username_identity_conflict",
                f"Argilla username 对应多个 UUID，拒绝选择: {username} [{ids}]",
            )
        return matches[0] if matches else None

    def _workspace_by_uuid(self, workspace_uuid: str) -> Any | None:
        workspace = None
        failed = False
        try:
            workspace = self._client.workspaces(id=workspace_uuid)
        except Exception:
            failed = True
        if failed:
            raise ArgillaProvisioningError(
                "workspace_lookup_failed",
                f"无法通过 Argilla 2.8 public workspaces(id=...) 查询 workspace UUID: {workspace_uuid}",
            ) from None
        return workspace

    def _workspace_by_name(self, name: str) -> Any | None:
        workspaces = None
        failed = False
        try:
            workspaces = list(self._client.workspaces.list())
        except Exception:
            failed = True
        if failed or workspaces is None:
            raise ArgillaProvisioningError(
                "workspace_list_failed",
                "无法通过 Argilla 2.8 public workspaces.list() 查询 personal workspace",
            ) from None

        matches = [item for item in workspaces if _text_or_none(getattr(item, "name", None)) == name]
        matches.sort(key=lambda item: _text_or_none(getattr(item, "id", None)) or "")
        if len(matches) > 1:
            ids = ",".join(_canonical_uuid(getattr(item, "id", None), "workspace") for item in matches)
            raise ArgillaProvisioningError(
                "workspace_name_identity_conflict",
                f"Argilla workspace name 对应多个 UUID，拒绝选择: {name} [{ids}]",
            )
        return matches[0] if matches else None

    def _workspace_members(self, workspace: Any) -> list[Any]:
        members = None
        failed = False
        try:
            members = list(workspace.users)
        except Exception:
            failed = True
        if failed or members is None:
            workspace_uuid = _canonical_uuid(getattr(workspace, "id", None), "workspace")
            raise ArgillaProvisioningError(
                "membership_list_failed",
                f"无法通过 Argilla 2.8 public workspace.users 查询成员: {workspace_uuid}",
            ) from None
        members.sort(
            key=lambda item: (
                _text_or_none(getattr(item, "username", None)) or "",
                _text_or_none(getattr(item, "id", None)) or "",
            )
        )
        return members

    def _verified_member(self, members: list[Any], expected: ArgillaUserDTO) -> Any | None:
        matching_id = [
            member
            for member in members
            if _text_or_none(getattr(member, "id", None)) is not None
            and _canonical_uuid(getattr(member, "id", None), "workspace member") == expected.uuid
        ]
        if matching_id:
            self._user_dto(
                matching_id[0],
                username=expected.username,
                expected_uuid=expected.uuid,
            )
            return matching_id[0]

        same_name = [
            member
            for member in members
            if _text_or_none(getattr(member, "username", None)) == expected.username
        ]
        if same_name:
            ids = ",".join(
                sorted(_canonical_uuid(getattr(member, "id", None), "workspace member") for member in same_name)
            )
            raise ArgillaProvisioningError(
                "membership_identity_conflict",
                f"workspace 已包含同名异 UUID 成员，拒绝添加: {expected.username} [{ids}]",
            )
        return None

    def _user_dto(
        self,
        user: Any,
        *,
        username: str,
        expected_uuid: str | None = None,
    ) -> ArgillaUserDTO:
        user_uuid = _canonical_uuid(getattr(user, "id", None), "user")
        actual_username = _text_or_none(getattr(user, "username", None)) or ""
        role = _role_value(getattr(user, "role", None))
        if expected_uuid is not None and user_uuid != expected_uuid:
            raise ArgillaProvisioningError(
                "user_uuid_drift",
                f"Argilla user UUID 与 expected UUID 不一致: {user_uuid}",
            )
        if actual_username != username:
            raise ArgillaProvisioningError(
                "username_drift",
                f"Argilla user UUID 对应的 username 与派生身份不一致: {user_uuid}",
            )
        if user_uuid == self._operator_uuid and self._operator_role == "owner":
            raise ArgillaProvisioningError(
                "owner_identity_conflict",
                "Argilla owner 账号不得作为 annotator provisioning 结果返回",
            )
        if role != _ARGILLA_ANNOTATOR_ROLE:
            raise ArgillaProvisioningError(
                "user_role_drift",
                f"Argilla user role 必须是 annotator，实际为 {role or '<missing>'}: {user_uuid}",
            )
        return ArgillaUserDTO(
            uuid=user_uuid,
            username=actual_username,
            role=role,
            first_name=_text_or_none(getattr(user, "first_name", None)),
            last_name=_text_or_none(getattr(user, "last_name", None)),
        )

    def _workspace_dto(
        self,
        workspace: Any,
        *,
        name: str,
        expected_uuid: str | None = None,
    ) -> ArgillaWorkspaceDTO:
        workspace_uuid = _canonical_uuid(getattr(workspace, "id", None), "workspace")
        actual_name = _text_or_none(getattr(workspace, "name", None)) or ""
        if expected_uuid is not None and workspace_uuid != expected_uuid:
            raise ArgillaProvisioningError(
                "workspace_uuid_drift",
                f"Argilla workspace UUID 与 expected UUID 不一致: {workspace_uuid}",
            )
        if actual_name != name:
            raise ArgillaProvisioningError(
                "workspace_name_drift",
                f"Argilla workspace UUID 对应的 name 与稳定 personal workspace 不一致: {workspace_uuid}",
            )
        if not _ARGILLA_WORKSPACE_NAME_PATTERN.fullmatch(actual_name):
            raise ArgillaProvisioningError(
                "workspace_name_invalid",
                f"Argilla personal workspace name 包含非法字符: {workspace_uuid}",
            )
        return ArgillaWorkspaceDTO(uuid=workspace_uuid, name=actual_name)


__all__ = [
    "ArgillaAdminAdapter",
    "ArgillaProvisioningAction",
    "ArgillaProvisioningDTO",
    "ArgillaProvisioningError",
    "ArgillaUserDTO",
    "ArgillaWorkspaceDTO",
    "derive_argilla_username",
    "derive_personal_workspace_name",
]
