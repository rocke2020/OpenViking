# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Durable add-resource queue consumer."""

import asyncio
import concurrent.futures
import json
from typing import Any, Dict, Optional

from openviking.observability.context import bind_execution_context
from openviking.server.identity import RequestContext, Role
from openviking.service.task_tracker import (
    ADD_RESOURCE_CANCEL_PROTOCOL_VERSION,
    TaskStatus,
    get_task_tracker,
)
from openviking.service.task_work_index import bind_task_context, extract_task_metadata
from openviking.storage.queuefs.add_resource_msg import AddResourceMsg
from openviking.storage.queuefs.named_queue import DequeueHandlerBase
from openviking.telemetry import bind_telemetry, resolve_telemetry, unregister_telemetry
from openviking.telemetry.request_wait_tracker import get_request_wait_tracker
from openviking.telemetry.resource_summary import record_resource_queue_metrics
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class AddResourceTaskCancelled(Exception):
    """Internal control flow for cooperative user-requested cancellation."""


class AddResourceProcessor(DequeueHandlerBase):
    """Own an add-resource task until it reaches a terminal state and can be ACKed."""

    def __init__(
        self,
        resource_service: Any,
        service_loop: asyncio.AbstractEventLoop,
        queue_name: str,
        viking_fs: Any,
    ):
        self._resource_service = resource_service
        self._service_loop = service_loop
        self._queue_name = queue_name
        self._viking_fs = viking_fs

    async def _load_lock(self, msg: AddResourceMsg, ctx: RequestContext) -> Any:
        """Adopt a pathlock handoff ref, returning an owned lease dict."""
        if msg.lock_handoff is None:
            msg._lock_handoff_adopted = False
            return None
        try:
            lock = await self._viking_fs._async_agfs.pathlock_adopt(msg.lock_handoff)
            msg._lock_handoff_adopted = True
            return lock
        except Exception as handoff_error:
            try:
                lock = await self._resource_service.reacquire_add_resource_job_lock(
                    msg.root_uri,
                    ctx,
                    materialization_pending=msg.target_materialization_pending,
                )
                msg._lock_handoff_adopted = False
                return lock
            except Exception as reacquire_error:
                if msg.target_materialization_pending:
                    from openviking.storage.errors import ResourceBusyError

                    raise ResourceBusyError(
                        f"Resource reservation is still busy: {msg.root_uri}",
                        uri=msg.root_uri,
                        conflict_type="path_busy",
                        retryable=True,
                    ) from reacquire_error
                raise handoff_error

    async def _requeue_lock_handoff(self, msg: AddResourceMsg, exc: Exception) -> bool:
        if msg.lock_handoff_retry >= 2:
            return False

        from openviking.storage.queuefs import get_queue_manager

        payload = msg.to_dict()
        payload["lock_handoff_retry"] = msg.lock_handoff_retry + 1
        await get_queue_manager().enqueue(self._queue_name, payload)
        logger.warning(
            "[AddResource] Requeued task %s after lock handoff failure: %s",
            msg.task_id,
            exc,
        )
        self.report_requeue()
        self.report_success()
        return True

    async def _release_final_lock(self, resource_lock: Dict[str, Any]) -> None:
        """Release a completion lock, retrying one transient backend failure."""
        try:
            await self._viking_fs._async_agfs.pathlock_release(resource_lock)
        except Exception as first_error:
            logger.warning(
                "[AddResource] Retrying final lock release after transient failure: %s",
                first_error,
            )
            await self._viking_fs._async_agfs.pathlock_release(resource_lock)

    async def _release_terminal_handoff(self, msg: AddResourceMsg) -> None:
        """Drain a legacy terminal message's still-live handoff before ACK."""
        if msg.lock_handoff is None:
            return
        try:
            resource_lock = await self._viking_fs._async_agfs.pathlock_adopt(msg.lock_handoff)
        except Exception as exc:
            error = str(exc)
            if "handoff/adopt failed:" in error and (
                "is no longer owned by" in error or "changed while adopting owner" in error
            ):
                return
            raise
        await self._viking_fs._async_agfs.pathlock_release(resource_lock)

    @staticmethod
    def _restore_rollback_target(msg: AddResourceMsg, task: Any) -> None:
        resource_id = getattr(task, "resource_id", None)
        if task is None or not isinstance(resource_id, str) or not resource_id:
            return
        rollback_target_created = getattr(task, "rollback_target_created", None)
        materialization_pending = bool(
            getattr(task, "rollback_target_materialization_pending", False)
        )
        lock_handoff = getattr(task, "rollback_lock_handoff", None)
        if type(rollback_target_created) is not bool and not materialization_pending:
            return
        msg.root_uri = resource_id
        msg.target_created = (
            rollback_target_created if type(rollback_target_created) is bool else None
        )
        msg.target_materialization_pending = materialization_pending
        if isinstance(lock_handoff, dict):
            msg.lock_handoff = dict(lock_handoff)
        msg.defer_target_resolution = False

    async def _rollback_cancelled(
        self,
        msg: AddResourceMsg,
        *,
        ctx: RequestContext,
        tracker: Any,
        resource_lock: Any,
    ) -> None:
        task = await tracker.get(
            msg.task_id,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
        )
        self._restore_rollback_target(msg, task)
        await self._resource_service.rollback_cancelled_add_resource(
            msg,
            ctx=ctx,
            resource_lock=resource_lock,
        )

    async def _fail_or_rollback_cancelled(
        self,
        msg: AddResourceMsg,
        error: str,
        *,
        ctx: RequestContext,
        tracker: Any,
        resource_lock: Any,
    ) -> bool:
        """Fail the task unless cancellation won, in which case roll it back."""
        current_task = await tracker.get(
            msg.task_id,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
        )
        if current_task is None or current_task.status not in (
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
        ):
            await tracker.fail(
                msg.task_id,
                error,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            )
            current_task = await tracker.get(
                msg.task_id,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            )
        if current_task is None or current_task.status not in (
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
        ):
            return False
        await self._rollback_cancelled(
            msg,
            ctx=ctx,
            tracker=tracker,
            resource_lock=resource_lock,
        )
        return True

    async def _process(self, msg: AddResourceMsg, data: Dict[str, Any]) -> None:
        telemetry_id = msg.telemetry_id or ""
        ctx = RequestContext(
            user=UserIdentifier(msg.account_id, msg.user_id),
            role=Role(msg.role),
            actor_peer_id=msg.actor_peer_id,
        )
        tracker = get_task_tracker()
        task = await tracker.create(
            "add_resource",
            resource_id=None if msg.defer_target_resolution else msg.root_uri,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
            task_id=msg.task_id,
            cancel_protocol_version=ADD_RESOURCE_CANCEL_PROTOCOL_VERSION,
            meta={"source_path": msg.source_path},
        )
        self._restore_rollback_target(msg, task)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            try:
                await self._release_terminal_handoff(msg)
            finally:
                unregister_telemetry(telemetry_id)
            self.report_success()
            return None
        resource_lock = None
        finalizing_completion = False
        try:
            resource_lock = await self._load_lock(msg, ctx)
        except Exception as exc:
            from openviking.storage.errors import ResourceBusyError

            if (
                isinstance(exc, ResourceBusyError)
                and msg.target_materialization_pending
                and exc.retryable
            ):
                raise
            current_task = await tracker.get(
                msg.task_id,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            )
            if current_task is not None and current_task.status in (
                TaskStatus.CANCELLING,
                TaskStatus.CANCELLED,
            ):
                await self._rollback_cancelled(
                    msg,
                    ctx=ctx,
                    tracker=tracker,
                    resource_lock=None,
                )
                self.report_success()
                return None
            if await self._requeue_lock_handoff(msg, exc):
                return None
            error = f"Invalid lock_handoff: {exc}"
            if await self._fail_or_rollback_cancelled(
                msg,
                error,
                ctx=ctx,
                tracker=tracker,
                resource_lock=None,
            ):
                self.report_success()
                return None
            self.report_error(error, data)
            unregister_telemetry(telemetry_id)
            return None

        if task.status in (TaskStatus.CANCELLING, TaskStatus.CANCELLED):
            try:
                await self._rollback_cancelled(
                    msg,
                    ctx=ctx,
                    tracker=tracker,
                    resource_lock=resource_lock,
                )
            finally:
                if resource_lock is not None:
                    await self._viking_fs._async_agfs.pathlock_release(resource_lock)
                    resource_lock = None
            self.report_success()
            unregister_telemetry(telemetry_id)
            return None

        telemetry = resolve_telemetry(telemetry_id) if telemetry_id else None
        if telemetry is None:
            from openviking.telemetry.operation import OperationTelemetry

            telemetry = OperationTelemetry(operation="add_resource_job", enabled=False)
            if telemetry_id:
                telemetry.telemetry_id = telemetry_id
        request_wait_tracker = get_request_wait_tracker()
        request_wait_tracker.register_request(telemetry_id)

        async def _set_stage(stage: str) -> None:
            if tracker.is_cancelled(msg.task_id):
                raise AddResourceTaskCancelled
            await tracker.update_stage(
                msg.task_id,
                stage,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
            )

        with (
            bind_execution_context(),
            bind_telemetry(telemetry),
            bind_task_context(msg.task_id, ctx.account_id, ctx.user.user_id),
        ):
            try:
                metadata = extract_task_metadata(data)
                await tracker.start(
                    msg.task_id,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                    stage="queued",
                )
                result = await self._resource_service.execute_add_resource_job(
                    msg,
                    ctx=ctx,
                    resource_lock=resource_lock,
                    stage_callback=_set_stage,
                )
                if result.pop("_resource_lock_transferred", False):
                    resource_lock = None
                current_task = await tracker.get(
                    msg.task_id,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                )
                if current_task is not None and current_task.status in (
                    TaskStatus.CANCELLING,
                    TaskStatus.CANCELLED,
                ):
                    await self._rollback_cancelled(
                        msg,
                        ctx=ctx,
                        tracker=tracker,
                        resource_lock=resource_lock,
                    )
                    self.report_success()
                    return None
                if result.get("status") == "error":
                    errors = result.get("errors") or ["resource processing failed"]
                    error = "; ".join(str(error) for error in errors)
                    if await self._fail_or_rollback_cancelled(
                        msg,
                        error,
                        ctx=ctx,
                        tracker=tracker,
                        resource_lock=resource_lock,
                    ):
                        self.report_success()
                        return None
                    self.report_error("resource processing failed", data)
                    return None
                await tracker.wait_for_descendants(msg.task_id, metadata.work_id)
                result["queue_status"] = request_wait_tracker.build_queue_status(telemetry_id)
                record_resource_queue_metrics(
                    telemetry=telemetry,
                    telemetry_id=telemetry_id,
                    root_uri=result.get("root_uri"),
                )
                await self._resource_service._link_resource_reason_memory(
                    result=result,
                    ctx=ctx,
                    reason=msg.reason,
                    source_name=msg.source_name,
                    timeout=msg.timeout,
                )
                current_task = await tracker.get(
                    msg.task_id,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                )
                if current_task is not None and current_task.status in (
                    TaskStatus.CANCELLING,
                    TaskStatus.CANCELLED,
                ):
                    await self._rollback_cancelled(
                        msg,
                        ctx=ctx,
                        tracker=tracker,
                        resource_lock=resource_lock,
                    )
                    self.report_success()
                    return None
                finalizing_completion = True
                if resource_lock is not None:
                    await self._release_final_lock(resource_lock)
                    resource_lock = None
                await tracker.update_add_resource_rollback_target(
                    msg.task_id,
                    result.get("root_uri") or msg.root_uri,
                    msg.target_created,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                    materialization_pending=False,
                    lock_handoff=None,
                )
                await tracker.complete(
                    msg.task_id,
                    result,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                    resource_id=result.get("root_uri"),
                )
                finalizing_completion = False
                current_task = await tracker.get(
                    msg.task_id,
                    account_id=ctx.account_id,
                    user_id=ctx.user.user_id,
                )
                if current_task is not None and current_task.status in (
                    TaskStatus.CANCELLING,
                    TaskStatus.CANCELLED,
                ):
                    await self._rollback_cancelled(
                        msg,
                        ctx=ctx,
                        tracker=tracker,
                        resource_lock=resource_lock,
                    )
                self.report_success()
                return None
            except AddResourceTaskCancelled:
                await self._rollback_cancelled(
                    msg,
                    ctx=ctx,
                    tracker=tracker,
                    resource_lock=resource_lock,
                )
                self.report_success()
                return None
            except Exception as exc:
                if finalizing_completion:
                    raise
                from openviking.storage.errors import ResourceBusyError
                from openviking.utils.resource_processor import (
                    RollbackTargetPersistenceError,
                )

                if isinstance(exc, RollbackTargetPersistenceError):
                    raise
                if (
                    isinstance(exc, ResourceBusyError)
                    and msg.target_materialization_pending
                    and exc.retryable
                ):
                    # Keep the message unACKed for RecoverStale instead of failing a durable task.
                    raise
                if await self._fail_or_rollback_cancelled(
                    msg,
                    str(exc),
                    ctx=ctx,
                    tracker=tracker,
                    resource_lock=resource_lock,
                ):
                    self.report_success()
                    return None
                self.report_error(str(exc), data)
                return None
            finally:
                request_wait_tracker.cleanup(telemetry_id)
                unregister_telemetry(telemetry_id)
                if resource_lock is not None and not finalizing_completion:
                    await self._viking_fs._async_agfs.pathlock_release(resource_lock)

    async def on_cancelled(self, data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Release an enqueue-time lock before ACKing cancelled work."""
        try:
            payload = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(payload, str):
                payload = json.loads(payload)
            msg = AddResourceMsg.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.report_error(str(exc), data)
            return None
        future = asyncio.run_coroutine_threadsafe(self._process(msg, payload), self._service_loop)
        await asyncio.wrap_future(future)
        return None

    async def on_dequeue(self, data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not data:
            return None
        try:
            if not isinstance(data, dict):
                raise ValueError("Queue message must be an object")
            payload = data.get("data", data)
            if isinstance(payload, str):
                payload = json.loads(payload)
            msg = AddResourceMsg.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.report_error(str(exc), data)
            return None

        future: concurrent.futures.Future[None] = asyncio.run_coroutine_threadsafe(
            self._process(msg, data),
            self._service_loop,
        )
        try:
            await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        return None
