# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Internal storage backends for TaskTracker."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Awaitable, Callable, Collection, Dict, List, Optional, Protocol

from openviking.pyagfs import AsyncAGFSClient
from openviking.pyagfs.async_client import fs_ctx_from_agfs_path
from openviking.pyagfs.exceptions import AGFSAlreadyExistsError, AGFSNotFoundError
from openviking.storage.pathlock_lease import NativePathLockLease

SYSTEM_TASK_ACCOUNT_ID = "_system"
SYSTEM_TASK_USER_ID = "root"
TASK_STORE_LOCK_TIMEOUT_SECS = 30.0


class TaskStore(Protocol):
    async def create(self, task: Any) -> None: ...

    async def update(self, task: Any) -> None: ...

    async def update_if_status(self, task: Any, expected_statuses: Collection[str]) -> bool: ...

    async def update_add_resource_rollback_target(
        self,
        task_id: str,
        *,
        account_id: str,
        user_id: str,
        resource_id: str,
        rollback_target_created: Optional[bool],
        materialization_pending: bool = False,
        lock_handoff: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]: ...

    async def run_if_status(
        self,
        task_id: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        account_id: str,
        user_id: str,
        expected_statuses: Collection[str],
    ) -> tuple[bool, Any]: ...

    async def get(
        self,
        task_id: str,
        *,
        account_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]: ...

    async def list(
        self, account_id: str, *, user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]: ...

    async def delete(
        self, task_id: str, *, account_id: str, user_id: Optional[str] = None
    ) -> None: ...


class PersistentTaskStore:
    """Persist task records into AGFS under account-scoped system task directories."""

    ROOT_PREFIX = "/local"
    SYSTEM_DIRNAME = "_system"
    TASKS_DIRNAME = "tasks"

    def __init__(self, agfs: Any) -> None:
        self._agfs = agfs if isinstance(agfs, AsyncAGFSClient) else AsyncAGFSClient(agfs)

    async def create(self, task: Any) -> None:
        await self._write_task(task)

    async def update(self, task: Any) -> None:
        await self._write_task(task)

    async def update_if_status(
        self,
        task: Any,
        expected_statuses: Collection[str],
    ) -> bool:
        """Update a task only while its persisted status is still expected."""
        path = self._task_path(task.account_id, task.user_id, task.task_id)
        lease = NativePathLockLease(
            self._agfs,
            await self._agfs.pathlock_acquire_exact(
                path,
                timeout_secs=TASK_STORE_LOCK_TIMEOUT_SECS,
            ),
        )
        try:
            current = await self.get(
                task.task_id,
                account_id=task.account_id,
                user_id=task.user_id,
            )
            if current is None or current.get("status") not in expected_statuses:
                return False
            persisted_rollback_target_created = current.get("rollback_target_created")
            persisted_materialization_pending = bool(
                current.get("rollback_target_materialization_pending", False)
            )
            persisted_lock_handoff = current.get("rollback_lock_handoff")
            if type(persisted_rollback_target_created) is bool or persisted_materialization_pending:
                task.resource_id = current.get("resource_id")
                task.rollback_target_created = (
                    persisted_rollback_target_created
                    if type(persisted_rollback_target_created) is bool
                    else None
                )
                task.rollback_target_materialization_pending = persisted_materialization_pending
                task.rollback_lock_handoff = (
                    deepcopy(persisted_lock_handoff)
                    if isinstance(persisted_lock_handoff, dict)
                    else None
                )
            await self._write_task(task, lease_ref=lease.ref)
            return True
        finally:
            await lease.close()

    async def update_add_resource_rollback_target(
        self,
        task_id: str,
        *,
        account_id: str,
        user_id: str,
        resource_id: str,
        rollback_target_created: Optional[bool],
        materialization_pending: bool = False,
        lock_handoff: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist rollback metadata without overwriting a concurrent lifecycle transition."""
        path = self._task_path(account_id, user_id, task_id)
        lease = NativePathLockLease(
            self._agfs,
            await self._agfs.pathlock_acquire_exact(
                path,
                timeout_secs=TASK_STORE_LOCK_TIMEOUT_SECS,
            ),
        )
        try:
            current = await self.get(
                task_id,
                account_id=account_id,
                user_id=user_id,
            )
            if current is None:
                return None
            current["resource_id"] = resource_id
            current["rollback_target_created"] = rollback_target_created
            current["rollback_target_materialization_pending"] = materialization_pending
            current["rollback_lock_handoff"] = deepcopy(lock_handoff)
            await self._write_payload(path, current, lease_ref=lease.ref)
            return deepcopy(current)
        finally:
            await lease.close()

    async def run_if_status(
        self,
        task_id: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        account_id: str,
        user_id: str,
        expected_statuses: Collection[str],
    ) -> tuple[bool, Any]:
        """Run an operation while the persisted task remains in an expected state."""
        path = self._task_path(account_id, user_id, task_id)
        lease = NativePathLockLease(
            self._agfs,
            await self._agfs.pathlock_acquire_exact(
                path,
                timeout_secs=TASK_STORE_LOCK_TIMEOUT_SECS,
            ),
        )
        try:
            current = await self.get(
                task_id,
                account_id=account_id,
                user_id=user_id,
            )
            if current is None or current.get("status") not in expected_statuses:
                return False, None
            return True, await operation()
        finally:
            await lease.close()

    async def get(
        self,
        task_id: str,
        *,
        account_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not account_id or not user_id:
            return None
        path = self._task_path(account_id, user_id, task_id)
        try:
            raw = await self._agfs.read(path)
        except (AGFSNotFoundError, FileNotFoundError):
            return None
        return json.loads(_decode_bytes(raw))

    async def list(self, account_id: str, *, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        directory = self._task_dir(account_id, user_id)
        try:
            items = await self._agfs.ls(directory)
        except (AGFSNotFoundError, FileNotFoundError):
            return []
        tasks: List[Dict[str, Any]] = []
        for item in items:
            path = item.get("path") or f"{directory}/{item.get('name', '')}"
            if not path.endswith(".json"):
                continue
            try:
                raw = await self._agfs.read(path)
            except (AGFSNotFoundError, FileNotFoundError):
                continue
            tasks.append(json.loads(_decode_bytes(raw)))
        return tasks

    async def delete(self, task_id: str, *, account_id: str, user_id: Optional[str] = None) -> None:
        if not user_id:
            return
        await self._agfs.rm(self._task_path(account_id, user_id, task_id), force=True)

    async def _write_task(
        self,
        task: Any,
        *,
        lease_ref: Optional[Dict[str, Any]] = None,
    ) -> None:
        account_id = getattr(task, "account_id", None)
        user_id = getattr(task, "user_id", None)
        if not account_id or not user_id:
            raise ValueError("PersistentTaskStore requires account_id and user_id")
        await self._ensure_task_dir(account_id, user_id)
        await self._write_payload(
            self._task_path(account_id, user_id, task.task_id),
            _task_to_payload(task),
            lease_ref=lease_ref,
        )

    async def _write_payload(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        lease_ref: Optional[Dict[str, Any]] = None,
    ) -> None:
        fs_ctx = fs_ctx_from_agfs_path(path)
        if lease_ref is not None:
            opaque_ref = lease_ref.get("lease_ref")
            if not isinstance(opaque_ref, str) or not opaque_ref:
                raise ValueError("owned task-store lease must contain lease_ref")
            fs_ctx["lease_ref"] = opaque_ref
        await self._agfs.write(
            path,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            fs_ctx=fs_ctx,
        )

    async def _ensure_task_dir(self, account_id: str, user_id: str) -> None:
        await self._mkdir_if_missing(self._account_dir(account_id))
        await self._mkdir_if_missing(self._system_dir(account_id))
        await self._mkdir_if_missing(self._task_root_dir(account_id))
        await self._mkdir_if_missing(self._task_dir(account_id, user_id))

    async def _mkdir_if_missing(self, path: str) -> None:
        try:
            await self._agfs.mkdir(path)
        except AGFSAlreadyExistsError:
            return
        except Exception as exc:
            if "already exists" in str(exc).lower():
                return
            raise

    def _account_dir(self, account_id: str) -> str:
        return f"{self.ROOT_PREFIX}/{account_id}"

    def _system_dir(self, account_id: str) -> str:
        if account_id == SYSTEM_TASK_ACCOUNT_ID:
            return self._account_dir(account_id)
        return f"{self._account_dir(account_id)}/{self.SYSTEM_DIRNAME}"

    def _task_root_dir(self, account_id: str) -> str:
        return f"{self._system_dir(account_id)}/{self.TASKS_DIRNAME}"

    def _task_dir(self, account_id: str, user_id: str) -> str:
        return f"{self._task_root_dir(account_id)}/{user_id}"

    def _task_path(self, account_id: str, user_id: str, task_id: str) -> str:
        return f"{self._task_dir(account_id, user_id)}/{task_id}.json"


def _task_to_payload(task: Any) -> Dict[str, Any]:
    status = getattr(task, "status", None)
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "status": status.value if hasattr(status, "value") else status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "resource_id": task.resource_id,
        "account_id": task.account_id,
        "user_id": task.user_id,
        "meta": deepcopy(task.meta),
        "stage": task.stage,
        "result": deepcopy(task.result),
        "error": task.error,
        "cancel_protocol_version": task.cancel_protocol_version,
        "rollback_target_created": task.rollback_target_created,
        "rollback_target_materialization_pending": (task.rollback_target_materialization_pending),
        "rollback_lock_handoff": deepcopy(task.rollback_lock_handoff),
    }


def _decode_bytes(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)
