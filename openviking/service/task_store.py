# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Internal storage backends for TaskTracker."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Awaitable, Callable, Collection, Dict, List, Optional, Protocol

from openviking.pyagfs import AsyncAGFSClient
from openviking.pyagfs.exceptions import AGFSAlreadyExistsError, AGFSNotFoundError
from openviking.storage.transaction.lock_handle import LockHandle
from openviking.storage.transaction.lock_lease import OwnedLockLease
from openviking.storage.transaction.lock_manager import LockManager
from openviking.storage.transaction.path_lock import PathLockEngine

SYSTEM_TASK_ACCOUNT_ID = "_system"
SYSTEM_TASK_USER_ID = "root"


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
        sync_agfs = agfs._client if isinstance(agfs, AsyncAGFSClient) else agfs
        self._agfs = agfs if isinstance(agfs, AsyncAGFSClient) else AsyncAGFSClient(agfs)
        self._task_lock_manager = LockManager(sync_agfs, redo_recovery_enabled=False)
        self._task_locks: PathLockEngine = self._task_lock_manager._path_lock

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
        owner = LockHandle()
        acquired = await self._task_locks.acquire_exact_path(
            path,
            owner,
            timeout=float("inf"),
        )
        if not acquired:
            raise RuntimeError(f"Failed to acquire task state lock: {task.task_id}")
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
            if type(persisted_rollback_target_created) is bool or persisted_materialization_pending:
                task.resource_id = current.get("resource_id")
                task.rollback_target_created = (
                    persisted_rollback_target_created
                    if type(persisted_rollback_target_created) is bool
                    else None
                )
                task.rollback_target_materialization_pending = persisted_materialization_pending
            await self._write_task(task)
            return True
        finally:
            await self._task_locks.release(owner)

    async def update_add_resource_rollback_target(
        self,
        task_id: str,
        *,
        account_id: str,
        user_id: str,
        resource_id: str,
        rollback_target_created: Optional[bool],
        materialization_pending: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Persist rollback metadata without overwriting a concurrent lifecycle transition."""
        path = self._task_path(account_id, user_id, task_id)
        owner = LockHandle()
        acquired = await self._task_locks.acquire_exact_path(
            path,
            owner,
            timeout=float("inf"),
        )
        if not acquired:
            raise RuntimeError(f"Failed to acquire task state lock: {task_id}")
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
            await self._write_payload(path, current)
            return deepcopy(current)
        finally:
            await self._task_locks.release(owner)

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
        lease = await OwnedLockLease.acquire_exact_paths(
            self._task_lock_manager,
            [path],
            timeout=float("inf"),
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

    async def _write_task(self, task: Any) -> None:
        account_id = getattr(task, "account_id", None)
        user_id = getattr(task, "user_id", None)
        if not account_id or not user_id:
            raise ValueError("PersistentTaskStore requires account_id and user_id")
        await self._ensure_task_dir(account_id, user_id)
        await self._write_payload(
            self._task_path(account_id, user_id, task.task_id),
            _task_to_payload(task),
        )

    async def _write_payload(self, path: str, payload: Dict[str, Any]) -> None:
        await self._agfs.write(
            path,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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
        "stage": task.stage,
        "result": deepcopy(task.result),
        "error": task.error,
        "rollback_target_created": task.rollback_target_created,
        "rollback_target_materialization_pending": (task.rollback_target_materialization_pending),
    }


def _decode_bytes(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)
