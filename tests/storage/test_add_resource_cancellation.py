# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.resource_service import ResourceService
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import (
    ADD_RESOURCE_CANCEL_PROTOCOL_VERSION,
    TaskStatus,
    TaskTracker,
    set_task_tracker,
)
from openviking.storage.errors import ResourceBusyError
from openviking.storage.queuefs.add_resource_msg import AddResourceMsg
from openviking.storage.queuefs.add_resource_processor import (
    AddResourceProcessor,
    AddResourceTaskCancelled,
)
from openviking.storage.queuefs.semantic_dag import DagWork, SemanticDagExecutor, VectorizeTask
from openviking.storage.queuefs.semantic_msg import SemanticMsg
from openviking.storage.queuefs.semantic_processor import SemanticProcessor
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError, NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from tests.test_task_tracker import _FakeAgfs


def test_add_resource_message_persists_target_ownership_for_safe_rollback():
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
    )

    restored = AddResourceMsg.from_dict(msg.to_dict())

    assert restored.target_created is True


def test_add_resource_args_reject_user_controlled_target_ownership():
    service = ResourceService()

    with pytest.raises(InvalidArgumentError, match="target_created"):
        service._normalize_add_resource_args(
            {"target_created": True},
            watch_interval=0,
        )


def test_legacy_add_resource_message_treats_target_ownership_as_unknown():
    restored = AddResourceMsg.from_dict(
        {
            "task_id": "task-1",
            "root_uri": "viking://resources/demo",
            "account_id": "acme",
            "user_id": "alice",
            "role": "user",
            "path": "https://example.com/demo.git",
        }
    )

    assert restored.target_created is None


def test_truthy_non_boolean_target_ownership_never_grants_delete_authority():
    payload = {
        "task_id": "task-1",
        "root_uri": "viking://resources/demo",
        "account_id": "acme",
        "user_id": "alice",
        "role": "user",
        "path": "https://example.com/demo.git",
        "target_created": "false",
    }

    assert AddResourceMsg.from_dict(payload).target_created is None


def test_add_resource_message_propagates_task_to_semantic_pipeline():
    msg = SemanticMsg(
        uri="viking://resources/demo",
        context_type="resource",
        source_task_id="task-1",
    )

    restored = SemanticMsg.from_dict(msg.to_dict())

    assert restored.source_task_id == "task-1"


@pytest.mark.asyncio
async def test_cancel_add_resource_is_owner_scoped_and_idempotent():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = ResourceService()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        cancel_protocol_version=ADD_RESOURCE_CANCEL_PROTOCOL_VERSION,
    )
    await tracker.start(task.task_id, account_id="acme", user_id="alice")

    bob_ctx = RequestContext(user=UserIdentifier("acme", "bob"), role=Role.USER)
    with pytest.raises(NotFoundError):
        await service.cancel_add_resource_task(task.task_id, ctx=bob_ctx)
    still_running = await tracker.get(task.task_id, account_id="acme", user_id="alice")
    assert still_running is not None
    assert still_running.status == TaskStatus.RUNNING

    first = await service.cancel_add_resource_task(task.task_id, ctx=ctx)
    second = await service.cancel_add_resource_task(task.task_id, ctx=ctx)

    assert first["status"] == TaskStatus.CANCELLED.value
    assert second == first


@pytest.mark.asyncio
async def test_cancel_rejects_pre_upgrade_task_without_cancel_protocol():
    agfs = _FakeAgfs()
    tracker = TaskTracker(store=PersistentTaskStore(agfs))
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        task_id="legacy-task",
    )
    await tracker.start(task.task_id, account_id="acme", user_id="alice")
    task_path = f"/local/acme/_system/tasks/alice/{task.task_id}.json"
    payload = json.loads(agfs.files[task_path].decode("utf-8"))
    payload.pop("cancel_protocol_version", None)
    agfs.files[task_path] = json.dumps(payload).encode("utf-8")

    recovered_tracker = TaskTracker(store=PersistentTaskStore(agfs))
    set_task_tracker(recovered_tracker)
    service = ResourceService()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)

    with pytest.raises(FailedPreconditionError, match="before durable cancellation support"):
        await service.cancel_add_resource_task(task.task_id, ctx=ctx)

    persisted = await recovered_tracker.get(task.task_id, account_id="acme", user_id="alice")
    assert persisted is not None
    assert persisted.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_root_cancel_loads_persisted_task_from_explicit_owner_scope():
    agfs = _FakeAgfs()
    original_tracker = TaskTracker(store=PersistentTaskStore(agfs))
    task = await original_tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
        cancel_protocol_version=ADD_RESOURCE_CANCEL_PROTOCOL_VERSION,
    )
    set_task_tracker(TaskTracker(store=PersistentTaskStore(agfs)))
    service = ResourceService()
    root_ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.ROOT)

    cancelled = await service.cancel_add_resource_task(task.task_id, ctx=root_ctx)

    assert cancelled["status"] == TaskStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_default_root_identity_can_cancel_cached_tenant_task():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
        cancel_protocol_version=ADD_RESOURCE_CANCEL_PROTOCOL_VERSION,
    )
    service = ResourceService()
    root_ctx = RequestContext(user=UserIdentifier("default", "default"), role=Role.ROOT)

    cancelled = await service.cancel_add_resource_task(task.task_id, ctx=root_ctx)

    assert cancelled["status"] == TaskStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_explicit_target_ownership_is_read_after_lifecycle_lock(monkeypatch):
    service = ResourceService()
    resource_lock = MagicMock()
    resource_lock.active = True
    resource_lock.handle = SimpleNamespace(
        created_paths=["/local/acme/resources/demo"],
    )
    resource_processor = MagicMock()
    resource_processor.tree_builder.resolve_target_uri = AsyncMock(
        return_value=("viking://resources/demo", None)
    )
    lifecycle_lock_acquired = False

    async def acquire_lock(*_args, **_kwargs):
        nonlocal lifecycle_lock_acquired
        lifecycle_lock_acquired = True
        return resource_lock

    async def read_target_ownership(*_args, **_kwargs):
        assert lifecycle_lock_acquired
        return False

    resource_processor.acquire_resource_lock = AsyncMock(side_effect=acquire_lock)
    resource_processor.target_contains_preexisting_data = AsyncMock(
        side_effect=read_target_ownership
    )
    service._resource_processor = resource_processor
    service._viking_fs = MagicMock()
    service._viking_fs._uri_to_path.return_value = "/local/acme/resources/demo"
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: object(),
    )

    (
        root_uri,
        acquired_lock,
        target_preexisting,
        target_created,
    ) = await service._plan_resource_target(
        path="https://example.com/demo.git",
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        target=SimpleNamespace(to="viking://resources/demo", parent=None, create_parent=False),
        source_name=None,
        source_info=SimpleNamespace(
            source_name="demo",
            source_path="https://example.com/demo.git",
            source_format="repository",
        ),
    )

    assert root_uri == "viking://resources/demo"
    assert acquired_lock is resource_lock
    assert target_preexisting is False
    assert target_created is True
    resource_processor.target_contains_preexisting_data.assert_awaited_once()


@pytest.mark.asyncio
async def test_preexisting_empty_target_is_never_marked_task_created(monkeypatch):
    service = ResourceService()
    resource_lock = MagicMock()
    resource_lock.handle = SimpleNamespace(created_paths=[])
    resource_processor = MagicMock()
    resource_processor.tree_builder.resolve_target_uri = AsyncMock(
        return_value=("viking://resources/demo", None)
    )
    resource_processor.acquire_resource_lock = AsyncMock(return_value=resource_lock)
    resource_processor.target_contains_preexisting_data = AsyncMock(return_value=False)
    service._resource_processor = resource_processor
    service._viking_fs = MagicMock()
    service._viking_fs.exists = AsyncMock(return_value=True)
    service._viking_fs._uri_to_path.return_value = "/local/acme/resources/demo"
    monkeypatch.setattr("openviking.storage.transaction.get_lock_manager", lambda: object())

    _, _, target_preexisting, target_created = await service._plan_resource_target(
        path="https://example.com/demo.git",
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        target=SimpleNamespace(to="viking://resources/demo", parent=None, create_parent=False),
        source_name="demo",
        source_info=SimpleNamespace(
            source_name="demo",
            source_path="https://example.com/demo.git",
            source_format="repository",
        ),
    )

    assert target_preexisting is False
    assert target_created is False


@pytest.mark.asyncio
async def test_target_created_during_lock_race_is_not_claimed(monkeypatch):
    service = ResourceService()
    resource_processor = MagicMock()
    resource_processor.tree_builder.resolve_target_uri = AsyncMock(
        return_value=("viking://resources/demo", None)
    )
    resource_lock = MagicMock()
    resource_lock.handle = SimpleNamespace(created_paths=[])
    resource_processor.acquire_resource_lock = AsyncMock(return_value=resource_lock)
    resource_processor.target_contains_preexisting_data = AsyncMock(return_value=False)
    service._resource_processor = resource_processor
    service._viking_fs = MagicMock()
    service._viking_fs._uri_to_path.return_value = "/local/acme/resources/demo"
    monkeypatch.setattr("openviking.storage.transaction.get_lock_manager", lambda: object())

    _, _, target_preexisting, target_created = await service._plan_resource_target(
        path="https://example.com/demo.git",
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        target=SimpleNamespace(to="viking://resources/demo", parent=None, create_parent=False),
        source_name="demo",
        source_info=SimpleNamespace(
            source_name="demo",
            source_path="https://example.com/demo.git",
            source_format="repository",
        ),
    )

    assert target_preexisting is False
    assert target_created is False


@pytest.mark.asyncio
@pytest.mark.parametrize("target_created", [False, None])
async def test_cancel_rollback_never_deletes_preexisting_or_unknown_target(target_created):
    service = ResourceService()
    service._viking_fs = AsyncMock()
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=target_created,
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)

    await service.rollback_cancelled_add_resource(msg, ctx=ctx)

    service._viking_fs.rm.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_rollback_deletes_only_task_created_target(monkeypatch):
    enqueued = []

    class FakeQueue:
        async def enqueue(self, msg):
            enqueued.append(msg)

    class FakeQueueManager:
        SEMANTIC = "Semantic"

        def get_queue(self, name, allow_create=False):
            assert name == self.SEMANTIC
            assert allow_create is True
            return FakeQueue()

    monkeypatch.setattr(
        "openviking.service.fs_service.get_queue_manager",
        lambda: FakeQueueManager(),
    )
    service = ResourceService()
    service._viking_fs = AsyncMock()
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    resource_lock = MagicMock()
    resource_lock.handle = "lock-handle"

    await service.rollback_cancelled_add_resource(msg, ctx=ctx, resource_lock=resource_lock)

    service._viking_fs.rm.assert_awaited_once_with(
        "viking://resources/demo",
        recursive=True,
        ctx=ctx,
        lock_handle="lock-handle",
    )
    assert len(enqueued) == 1
    assert enqueued[0].uri == "viking://resources"
    assert enqueued[0].source_task_id == ""
    assert enqueued[0].changes == {"deleted": ["viking://resources/demo"]}


@pytest.mark.asyncio
async def test_cancel_rollback_rejects_truthy_non_boolean_prepared_ownership():
    service = ResourceService()
    service._viking_fs = AsyncMock()
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        target_created=True,
        prepared={"target_created": "false"},
    )

    await service.rollback_cancelled_add_resource(
        msg,
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
    )

    service._viking_fs.rm.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_cancelled_job_is_acked_and_rolled_back_without_execution():
    agfs = _FakeAgfs()
    tracker = TaskTracker(store=PersistentTaskStore(agfs))
    set_task_tracker(tracker)
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await tracker.cancel(task.task_id, account_id="acme", user_id="alice")
    set_task_tracker(TaskTracker(store=PersistentTaskStore(agfs)))
    service = MagicMock()
    service.rollback_cancelled_add_resource = AsyncMock()
    service.execute_add_resource_job = AsyncMock()
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    processor._load_lock = AsyncMock(return_value=resource_lock)
    rollback_saw_active_lock = False

    async def rollback(*_args, **_kwargs):
        nonlocal rollback_saw_active_lock
        rollback_saw_active_lock = resource_lock.close.await_count == 0

    service.rollback_cancelled_add_resource.side_effect = rollback
    msg = AddResourceMsg(
        task_id=task.task_id,
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
    )

    await processor._process(msg, msg.to_dict())

    service.execute_add_resource_job.assert_not_awaited()
    assert rollback_saw_active_lock
    resource_lock.close.assert_awaited_once()
    service.rollback_cancelled_add_resource.assert_awaited_once_with(
        msg,
        ctx=ANY,
        resource_lock=resource_lock,
    )


@pytest.mark.asyncio
async def test_cancelled_job_falls_back_to_fresh_lock_when_handoff_is_invalid():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await tracker.start(task.task_id, account_id="acme", user_id="alice")
    service = MagicMock()
    service.rollback_cancelled_add_resource = AsyncMock()
    service.execute_add_resource_job = AsyncMock()
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    async def cancel_while_loading_lock(*_args, **_kwargs):
        await tracker.cancel(task.task_id, account_id="acme", user_id="alice")
        raise ValueError("stale handoff")

    processor._load_lock = AsyncMock(side_effect=cancel_while_loading_lock)
    msg = AddResourceMsg(
        task_id=task.task_id,
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
        lock_handoff_retry=2,
    )

    await processor._process(msg, msg.to_dict())

    service.execute_add_resource_job.assert_not_awaited()
    service.rollback_cancelled_add_resource.assert_awaited_once_with(
        msg,
        ctx=ANY,
        resource_lock=None,
    )


@pytest.mark.asyncio
async def test_post_execution_cancel_rolls_back_without_completing():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = MagicMock()
    service.rollback_cancelled_add_resource = AsyncMock()
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        target_created=True,
    )

    async def execute(*_args, **_kwargs):
        await tracker.cancel(msg.task_id, account_id="acme", user_id="alice")
        return {"status": "success"}

    service.execute_add_resource_job = AsyncMock(side_effect=execute)
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    await processor._process(msg, msg.to_dict())

    task = await tracker.get(msg.task_id, account_id="acme", user_id="alice")
    assert task is not None
    assert task.status == TaskStatus.CANCELLED
    service.rollback_cancelled_add_resource.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_execution_rollback_failure_is_not_acked():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = MagicMock()
    service.rollback_cancelled_add_resource = AsyncMock(side_effect=RuntimeError("rollback failed"))
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        target_created=True,
    )

    async def execute(*_args, **_kwargs):
        await tracker.cancel(msg.task_id, account_id="acme", user_id="alice")
        return {"status": "success"}

    service.execute_add_resource_job = AsyncMock(side_effect=execute)
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    with pytest.raises(RuntimeError, match="rollback failed"):
        await processor._process(msg, msg.to_dict())


@pytest.mark.asyncio
async def test_cancel_racing_with_failure_rolls_back_before_ack():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = MagicMock()
    service.execute_add_resource_job = AsyncMock(side_effect=RuntimeError("processing failed"))
    service.rollback_cancelled_add_resource = AsyncMock()
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
    )
    original_fail = tracker.fail

    async def cancel_before_fail(task_id, error, account_id=None, user_id=None):
        await tracker.cancel(task_id, account_id=account_id, user_id=user_id)
        await original_fail(
            task_id,
            error,
            account_id=account_id,
            user_id=user_id,
        )

    tracker.fail = cancel_before_fail
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    await processor._process(msg, msg.to_dict())

    task = await tracker.get(msg.task_id, account_id="acme", user_id="alice")
    assert task is not None
    assert task.status == TaskStatus.CANCELLED
    service.rollback_cancelled_add_resource.assert_awaited_once_with(
        msg,
        ctx=ANY,
        resource_lock=None,
    )


@pytest.mark.asyncio
async def test_rollback_target_persistence_failure_is_not_acked():
    from openviking.utils.resource_processor import RollbackTargetPersistenceError

    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = MagicMock()
    service.execute_add_resource_job = AsyncMock(
        side_effect=RollbackTargetPersistenceError("task persistence failed")
    )
    service.rollback_cancelled_add_resource = AsyncMock()
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        defer_target_resolution=True,
    )
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    with pytest.raises(RollbackTargetPersistenceError, match="task persistence failed"):
        await processor._process(msg, msg.to_dict())

    task = await tracker.get(msg.task_id, account_id="acme", user_id="alice")
    assert task is not None
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_cancel_racing_with_completion_still_rolls_back():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = MagicMock()
    service.rollback_cancelled_add_resource = AsyncMock()
    service.execute_add_resource_job = AsyncMock(
        return_value={"status": "success", "root_uri": "viking://resources/demo"}
    )
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
    )
    complete = tracker.complete

    async def cancel_before_complete(*args, **kwargs):
        await tracker.cancel(msg.task_id, account_id="acme", user_id="alice")
        return await complete(*args, **kwargs)

    tracker.complete = cancel_before_complete
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    await processor._process(msg, msg.to_dict())

    task = await tracker.get(msg.task_id, account_id="acme", user_id="alice")
    assert task is not None
    assert task.status == TaskStatus.CANCELLED
    service.rollback_cancelled_add_resource.assert_awaited_once_with(
        msg,
        ctx=ANY,
        resource_lock=None,
    )


@pytest.mark.asyncio
async def test_deferred_target_resolution_updates_rollback_ownership():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    await tracker.create(
        "add_resource",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    service = ResourceService()
    service.add_resource = AsyncMock(
        return_value={
            "status": "success",
            "root_uri": "viking://resources/resolved",
            "_target_created": True,
        }
    )
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )

    result = await service.execute_add_resource_job(
        msg,
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        resource_lock=None,
        stage_callback=AsyncMock(),
    )

    assert result == {"status": "success", "root_uri": "viking://resources/resolved"}
    assert msg.root_uri == "viking://resources/resolved"
    assert msg.target_created is True


@pytest.mark.asyncio
async def test_execute_job_does_not_repeat_durable_rollback_target_update():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    await tracker.create(
        "add_resource",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    update_rollback_target = tracker.update_add_resource_rollback_target
    tracker.update_add_resource_rollback_target = AsyncMock(wraps=update_rollback_target)
    service = ResourceService()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)

    async def add_resource(**_kwargs):
        await service._persist_add_resource_rollback_target(
            "task-1",
            {"root_uri": "viking://resources/resolved"},
            True,
            ctx=ctx,
        )
        return {
            "status": "success",
            "root_uri": "viking://resources/resolved",
            "_target_created": True,
        }

    service.add_resource = AsyncMock(side_effect=add_resource)
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )

    result = await service.execute_add_resource_job(
        msg,
        ctx=ctx,
        resource_lock=None,
        stage_callback=AsyncMock(),
    )

    assert result == {"status": "success", "root_uri": "viking://resources/resolved"}
    tracker.update_add_resource_rollback_target.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_target_is_persisted_before_next_cancellation_stage():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    await tracker.create(
        "add_resource",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    resource_processor = MagicMock()

    async def process_resource(**kwargs):
        assert kwargs["defer_post_processing"] is False
        await kwargs["rollback_target_callback"](
            "viking://resources/resolved",
            True,
        )
        return {
            "status": "success",
            "root_uri": "viking://resources/resolved",
            "_target_created": True,
        }

    resource_processor.process_resource = AsyncMock(side_effect=process_resource)
    service = ResourceService(
        viking_fs=MagicMock(),
        resource_processor=resource_processor,
        skill_processor=MagicMock(),
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)

    async def cancel_at_processing_queue(stage):
        assert stage == "processing_queue"
        persisted = await tracker.get(
            "task-1",
            account_id="acme",
            user_id="alice",
        )
        assert persisted is not None
        assert persisted.resource_id == "viking://resources/resolved"
        assert persisted.rollback_target_created is True
        await tracker.cancel("task-1", account_id="acme", user_id="alice")
        raise AddResourceTaskCancelled

    with pytest.raises(AddResourceTaskCancelled):
        await service.add_resource(
            path="/tmp/demo.txt",
            ctx=ctx,
            to="viking://resources/placeholder",
            wait=True,
            skip_watch_management=True,
            source_task_id="task-1",
            stage_callback=cancel_at_processing_queue,
        )


@pytest.mark.asyncio
async def test_deferred_target_resolution_survives_restart_for_rollback(monkeypatch):
    agfs = _FakeAgfs()
    tracker = TaskTracker(store=PersistentTaskStore(agfs))
    set_task_tracker(tracker)
    await tracker.create(
        "add_resource",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    service = ResourceService()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)

    async def add_resource(**_kwargs):
        await service._persist_add_resource_rollback_target(
            "task-1",
            {"root_uri": "viking://resources/resolved"},
            True,
            ctx=ctx,
        )
        return {
            "status": "success",
            "root_uri": "viking://resources/resolved",
            "_target_created": True,
        }

    service.add_resource = AsyncMock(side_effect=add_resource)
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )
    persisted_queue_payload = msg.to_dict()

    await service.execute_add_resource_job(
        msg,
        ctx=ctx,
        resource_lock=None,
        stage_callback=AsyncMock(),
    )
    await tracker.cancel(msg.task_id, account_id="acme", user_id="alice")

    set_task_tracker(TaskTracker(store=PersistentTaskStore(agfs)))
    recovered_service = ResourceService()
    recovered_service._viking_fs = AsyncMock()
    semantic_queue = SimpleNamespace(enqueue=AsyncMock())
    queue_manager = SimpleNamespace(
        SEMANTIC="Semantic",
        get_queue=MagicMock(return_value=semantic_queue),
    )
    monkeypatch.setattr(
        "openviking.service.fs_service.get_queue_manager",
        lambda: queue_manager,
    )
    recovered_msg = AddResourceMsg.from_dict(persisted_queue_payload)
    processor = AddResourceProcessor(
        recovered_service,
        asyncio.get_running_loop(),
        "AddResource",
    )

    await processor._process(recovered_msg, persisted_queue_payload)

    recovered_service._viking_fs.rm.assert_awaited_once_with(
        "viking://resources/resolved",
        recursive=True,
        ctx=ANY,
        lock_handle=None,
    )


@pytest.mark.asyncio
async def test_recovered_deferred_target_replays_exact_persisted_uri():
    agfs = _FakeAgfs()
    tracker = TaskTracker(store=PersistentTaskStore(agfs))
    await tracker.create(
        "add_resource",
        resource_id="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await tracker.update_add_resource_rollback_target(
        "task-1",
        "viking://resources/resolved",
        True,
        account_id="acme",
        user_id="alice",
    )
    recovered_tracker = TaskTracker(store=PersistentTaskStore(agfs))
    recovered_task = await recovered_tracker.get(
        "task-1",
        account_id="acme",
        user_id="alice",
    )
    service = ResourceService()
    service.add_resource = AsyncMock(
        return_value={
            "status": "success",
            "root_uri": "viking://resources/resolved",
            "_target_created": True,
        }
    )
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )

    AddResourceProcessor._restore_rollback_target(msg, recovered_task)
    await service.execute_add_resource_job(
        msg,
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        resource_lock=None,
        stage_callback=AsyncMock(),
    )

    assert service.add_resource.await_args.kwargs["to"] == "viking://resources/resolved"
    assert service.add_resource.await_args.kwargs["parent"] is None


@pytest.mark.asyncio
async def test_recovered_pending_materialization_replays_under_tree_lock():
    agfs = _FakeAgfs()
    tracker = TaskTracker(store=PersistentTaskStore(agfs))
    await tracker.create(
        "add_resource",
        resource_id="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await tracker.update_add_resource_rollback_target(
        "task-1",
        "viking://resources/resolved",
        None,
        account_id="acme",
        user_id="alice",
        materialization_pending=True,
    )
    recovered_tracker = TaskTracker(store=PersistentTaskStore(agfs))
    recovered_task = await recovered_tracker.get(
        "task-1",
        account_id="acme",
        user_id="alice",
    )
    service = ResourceService()
    service.add_resource = AsyncMock(
        return_value={
            "status": "success",
            "root_uri": "viking://resources/resolved",
            "_target_created": True,
        }
    )
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )

    AddResourceProcessor._restore_rollback_target(msg, recovered_task)
    await service.execute_add_resource_job(
        msg,
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        resource_lock=None,
        stage_callback=AsyncMock(),
    )

    assert service.add_resource.await_args.kwargs["to"] == "viking://resources/resolved"
    assert service.add_resource.await_args.kwargs["target_created"] is None
    assert service.add_resource.await_args.kwargs["target_materialization_pending"] is True
    assert msg.target_created is None


@pytest.mark.asyncio
async def test_cancel_rollback_removes_materialized_pending_target_under_lock(monkeypatch):
    agfs = _FakeAgfs()
    tracker = TaskTracker(store=PersistentTaskStore(agfs))
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await tracker.update_add_resource_rollback_target(
        task.task_id,
        "viking://resources/resolved",
        None,
        account_id="acme",
        user_id="alice",
        materialization_pending=True,
    )
    await tracker.cancel(task.task_id, account_id="acme", user_id="alice")
    recovered_task = await TaskTracker(store=PersistentTaskStore(agfs)).get(
        task.task_id,
        account_id="acme",
        user_id="alice",
    )
    msg = AddResourceMsg(
        task_id=task.task_id,
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )
    AddResourceProcessor._restore_rollback_target(msg, recovered_task)

    service = ResourceService()
    service._viking_fs = AsyncMock()
    service._resource_processor = MagicMock()
    service._resource_processor.target_contains_preexisting_data = AsyncMock(return_value=False)
    service._resource_processor.materialize_candidate_reservation = AsyncMock()
    enqueue_delete_refresh = AsyncMock()
    monkeypatch.setattr(
        "openviking.service.fs_service.enqueue_delete_refresh",
        enqueue_delete_refresh,
    )
    resource_lock = MagicMock()
    resource_lock.handle = object()

    await service.rollback_cancelled_add_resource(
        msg,
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        resource_lock=resource_lock,
    )

    service._resource_processor.materialize_candidate_reservation.assert_awaited_once_with(
        resource_lock,
        "viking://resources/resolved",
        ctx=ANY,
        allow_existing_empty=True,
    )
    service._viking_fs.rm.assert_awaited_once_with(
        "viking://resources/resolved",
        recursive=True,
        ctx=ANY,
        lock_handle=resource_lock.handle,
    )


@pytest.mark.asyncio
async def test_recovered_pending_materialization_remains_unacked_while_old_lock_is_busy():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    task = await tracker.create(
        "add_resource",
        resource_id="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await tracker.update_add_resource_rollback_target(
        task.task_id,
        "viking://resources/resolved",
        None,
        account_id="acme",
        user_id="alice",
        materialization_pending=True,
    )
    service = MagicMock()
    service.execute_add_resource_job = AsyncMock(
        side_effect=ResourceBusyError(
            "Resource reservation could not be materialized",
            uri="viking://resources/resolved",
            retryable=True,
        )
    )
    service.rollback_cancelled_add_resource = AsyncMock()
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")
    report_success = MagicMock()
    report_requeue = MagicMock()
    report_error = MagicMock()
    processor.set_callbacks(report_success, report_requeue, report_error)
    msg = AddResourceMsg(
        task_id=task.task_id,
        root_uri="viking://resources/placeholder",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.larkoffice.com/docx/token",
        defer_target_resolution=True,
    )

    with pytest.raises(ResourceBusyError, match="could not be materialized"):
        await processor._process(msg, msg.to_dict())

    current = await tracker.get(task.task_id, account_id="acme", user_id="alice")
    assert current is not None
    assert current.status == TaskStatus.RUNNING
    report_success.assert_not_called()
    report_requeue.assert_not_called()
    report_error.assert_not_called()
    service.rollback_cancelled_add_resource.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_job_rolls_back_instead_of_completing_after_cancel():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    set_task_tracker(tracker)
    service = MagicMock()
    service.rollback_cancelled_add_resource = AsyncMock()
    continued_after_cancel = False
    msg = AddResourceMsg(
        task_id="task-1",
        root_uri="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        role="user",
        path="https://example.com/demo.git",
        target_created=True,
    )

    async def execute(*_args, **kwargs):
        nonlocal continued_after_cancel
        await tracker.cancel(msg.task_id, account_id="acme", user_id="alice")
        await kwargs["stage_callback"]("parsing")
        continued_after_cancel = True
        return {"status": "success"}

    service.execute_add_resource_job = AsyncMock(side_effect=execute)
    processor = AddResourceProcessor(service, asyncio.get_running_loop(), "AddResource")

    await processor._process(msg, msg.to_dict())

    completed = await tracker.get(msg.task_id, account_id="acme", user_id="alice")
    assert completed is not None
    assert completed.status == TaskStatus.CANCELLED
    assert not continued_after_cancel
    service.rollback_cancelled_add_resource.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_semantic_dag_stops_before_scheduling_work(monkeypatch):
    fake_fs = MagicMock()
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        lambda: fake_fs,
    )
    processor = MagicMock()
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        lock=resource_lock,
        is_cancelled=lambda: True,
    )

    await executor.run("viking://resources/demo")

    assert executor.stale
    fake_fs.ls.assert_not_called()
    resource_lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cancellation_drains_inflight_work_before_releasing_lock(monkeypatch):
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False
    processor = MagicMock()

    async def vectorize(*_args, **_kwargs):
        started.set()
        await release.wait()

    processor._vectorize_directory = AsyncMock(side_effect=vectorize)
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        lock=resource_lock,
        is_cancelled=lambda: cancelled,
    )
    work = DagWork(
        kind="vectorize",
        dir_uri="viking://resources/demo",
        vectorize_task=VectorizeTask(
            task_type="directory",
            uri="viking://resources/demo",
            context_type="resource",
            ctx=ctx,
        ),
    )
    executor._schedule_dir = lambda *_args, **_kwargs: executor._schedule_work(work)

    run_task = asyncio.create_task(executor.run("viking://resources/demo"))
    await started.wait()
    cancelled = True
    assert executor._stop_if_cancelled()
    await asyncio.sleep(0)

    resource_lock.close.assert_not_awaited()
    release.set()
    await run_task
    resource_lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cancellation_after_embedding_registration_releases_lock(monkeypatch):
    from openviking.storage.queuefs.embedding_tracker import EmbeddingTaskTracker

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    tracker = EmbeddingTaskTracker.get_instance()
    tracker._tasks.clear()
    cancelled = False
    original_register = tracker.register

    async def cancel_after_register(*args, **kwargs):
        nonlocal cancelled
        await original_register(*args, **kwargs)
        cancelled = True

    monkeypatch.setattr(tracker, "register", cancel_after_register)
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=MagicMock(),
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        semantic_msg_id="semantic-1",
        lock=resource_lock,
        is_cancelled=lambda: cancelled,
    )

    def seed_vectorization(*_args, **_kwargs):
        executor._vectorize_task_count = 1
        executor._pending_vectorize_tasks = [
            VectorizeTask(
                task_type="file",
                uri="viking://resources/demo/file.txt",
                context_type="resource",
                ctx=ctx,
                file_path="viking://resources/demo/file.txt",
            )
        ]
        executor._root_done.set()

    executor._schedule_dir = seed_vectorization

    await executor.run("viking://resources/demo")

    assert "semantic-1" not in tracker._tasks
    resource_lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cancellation_after_embedding_dispatch_waits_for_worker_drain(monkeypatch):
    from openviking.storage.queuefs.embedding_tracker import EmbeddingTaskTracker

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    tracker = EmbeddingTaskTracker.get_instance()
    tracker._tasks.clear()
    cancelled = False
    wait_tracker = MagicMock()
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_request_wait_tracker",
        lambda: wait_tracker,
    )
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=MagicMock(),
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        telemetry_id="telemetry-1",
        semantic_msg_id="semantic-1",
        lock=resource_lock,
        is_cancelled=lambda: cancelled,
    )

    def seed_vectorization(*_args, **_kwargs):
        executor._vectorize_task_count = 1
        executor._pending_vectorize_tasks = [
            VectorizeTask(
                task_type="file",
                uri="viking://resources/demo/file.txt",
                context_type="resource",
                ctx=ctx,
                file_path="viking://resources/demo/file.txt",
            )
        ]
        executor._root_done.set()

    async def dispatch_then_cancel(_tasks):
        nonlocal cancelled
        cancelled = True

    executor._schedule_dir = seed_vectorization
    monkeypatch.setattr(executor, "_dispatch_vectorize_tasks", dispatch_then_cancel)

    await executor.run("viking://resources/demo")

    assert "semantic-1" in tracker._tasks
    wait_tracker.mark_semantic_done.assert_not_called()
    resource_lock.close.assert_not_awaited()

    await tracker.decrement("semantic-1")

    assert "semantic-1" not in tracker._tasks
    wait_tracker.mark_semantic_done.assert_called_once_with(
        "telemetry-1",
        "semantic-1",
        processed_delta=0,
    )
    resource_lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cancellation_reconciles_vectorize_work_skipped_before_enqueue(monkeypatch):
    from openviking.storage.queuefs.embedding_tracker import EmbeddingTaskTracker

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    tracker = EmbeddingTaskTracker.get_instance()
    tracker._tasks.clear()
    cancelled = False
    processor = MagicMock()

    async def enqueue_first_embedding(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True

    processor._vectorize_single_file = AsyncMock(side_effect=enqueue_first_embedding)
    refresh_cancelled = AsyncMock(side_effect=lambda: cancelled)
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        semantic_msg_id="semantic-2",
        lock=resource_lock,
        is_cancelled=lambda: cancelled,
        refresh_cancelled=refresh_cancelled,
    )

    def seed_vectorization(*_args, **_kwargs):
        executor._vectorize_task_count = 3
        executor._pending_vectorize_tasks = [
            VectorizeTask(
                task_type="file",
                uri="viking://resources/demo/first.txt",
                context_type="resource",
                ctx=ctx,
                semantic_msg_id="semantic-2",
                file_path="viking://resources/demo/first.txt",
            ),
            VectorizeTask(
                task_type="directory",
                uri="viking://resources/demo/child",
                context_type="resource",
                ctx=ctx,
                semantic_msg_id="semantic-2",
            ),
        ]
        executor._root_done.set()

    executor._schedule_dir = seed_vectorization

    await executor.run("viking://resources/demo")

    processor._vectorize_single_file.assert_awaited_once()
    refresh_cancelled.assert_awaited_once()
    resource_lock.close.assert_not_awaited()

    await tracker.decrement("semantic-2")

    assert "semantic-2" not in tracker._tasks
    resource_lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cancellation_during_vectorize_submission_reconciles_rejected_work(
    monkeypatch,
):
    from openviking.storage.queuefs.embedding_tracker import EmbeddingTaskTracker

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    tracker = EmbeddingTaskTracker.get_instance()
    tracker._tasks.clear()
    cancelled = False
    accepted_started = asyncio.Event()
    release_accepted = asyncio.Event()
    accepted_work = []
    accepted_runners = []

    class CancelAfterFirstSubmitScheduler:
        def submit(self, submitted_executor, work):
            nonlocal cancelled
            accepted_work.append(work)
            cancelled = True

            async def run_accepted_work():
                accepted_started.set()
                await release_accepted.wait()
                await submitted_executor._run_work(work)

            accepted_runners.append(asyncio.create_task(run_accepted_work()))

    scheduler = CancelAfterFirstSubmitScheduler()
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_semantic_node_scheduler",
        lambda _max_workers: scheduler,
    )
    processor = MagicMock()
    processor._vectorize_single_file = AsyncMock()
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        semantic_msg_id="semantic-mid-submit",
        lock=resource_lock,
        is_cancelled=lambda: cancelled,
    )

    def seed_vectorization(*_args, **_kwargs):
        executor._vectorize_task_count = 3
        executor._pending_vectorize_tasks = [
            VectorizeTask(
                task_type="file",
                uri=f"viking://resources/demo/{index}.txt",
                context_type="resource",
                ctx=ctx,
                semantic_msg_id="semantic-mid-submit",
                file_path=f"viking://resources/demo/{index}.txt",
            )
            for index in range(3)
        ]
        executor._root_done.set()

    executor._schedule_dir = seed_vectorization
    run_task = asyncio.create_task(executor.run("viking://resources/demo"))

    try:
        await accepted_started.wait()
        await asyncio.sleep(0)

        record = tracker._tasks["semantic-mid-submit"]
        assert not run_task.done()
        assert len(accepted_work) == 1
        assert executor._pending_vectorize_work == 1
        assert record.remaining == 1
        resource_lock.close.assert_not_awaited()

        release_accepted.set()
        await asyncio.gather(run_task, *accepted_runners)

        assert executor._pending_vectorize_work == 0
        assert "semantic-mid-submit" not in tracker._tasks
        processor._vectorize_single_file.assert_not_awaited()
        resource_lock.close.assert_awaited_once()
    finally:
        release_accepted.set()
        await asyncio.gather(run_task, *accepted_runners, return_exceptions=True)
        tracker._tasks.clear()


@pytest.mark.asyncio
async def test_semantic_cancellation_refresh_is_shared_within_poll_interval(monkeypatch):
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    refresh_cancelled = AsyncMock(return_value=False)
    executor = SemanticDagExecutor(
        processor=MagicMock(),
        context_type="resource",
        max_concurrent_llm=4,
        ctx=RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER),
        is_cancelled=lambda: False,
        refresh_cancelled=refresh_cancelled,
    )

    await asyncio.gather(*(executor._refresh_cancelled_state() for _ in range(20)))

    refresh_cancelled.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_vectorize_refresh_failure_reconciles_reserved_work(monkeypatch):
    from openviking.storage.queuefs.embedding_tracker import EmbeddingTaskTracker

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    tracker = EmbeddingTaskTracker.get_instance()
    tracker._tasks.clear()
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=MagicMock(),
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        semantic_msg_id="semantic-refresh-failure",
        lock=resource_lock,
        is_cancelled=lambda: False,
        refresh_cancelled=AsyncMock(side_effect=RuntimeError("task store unavailable")),
    )

    def seed_vectorization(*_args, **_kwargs):
        executor._vectorize_task_count = 2
        executor._pending_vectorize_tasks = [
            VectorizeTask(
                task_type="file",
                uri=f"viking://resources/demo/{index}.txt",
                context_type="resource",
                ctx=ctx,
                semantic_msg_id="semantic-refresh-failure",
                file_path=f"viking://resources/demo/{index}.txt",
            )
            for index in range(2)
        ]
        executor._root_done.set()

    executor._schedule_dir = seed_vectorization

    try:
        with pytest.raises(RuntimeError, match="task store unavailable"):
            await asyncio.wait_for(executor.run("viking://resources/demo"), timeout=1)

        assert "semantic-refresh-failure" not in tracker._tasks
        assert executor._pending_vectorize_work == 0
        resource_lock.close.assert_awaited_once()
    finally:
        tracker._tasks.clear()


@pytest.mark.asyncio
async def test_semantic_refresh_failure_keeps_lock_until_transferred_embedding_drains(monkeypatch):
    from openviking.storage.queuefs.embedding_tracker import EmbeddingTaskTracker

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    tracker = EmbeddingTaskTracker.get_instance()
    tracker._tasks.clear()
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock(
        side_effect=[RuntimeError("release failed"), None],
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=MagicMock(),
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        semantic_msg_id="semantic-partial-refresh-failure",
        lock=resource_lock,
        is_cancelled=lambda: False,
        refresh_cancelled=AsyncMock(side_effect=[False, RuntimeError("task store unavailable")]),
    )
    executor._cancellation_refresh_interval = 0
    executor._run_vectorize_task = AsyncMock()

    def seed_vectorization(*_args, **_kwargs):
        executor._vectorize_task_count = 2
        executor._pending_vectorize_tasks = [
            VectorizeTask(
                task_type="file",
                uri=f"viking://resources/demo/{index}.txt",
                context_type="resource",
                ctx=ctx,
                semantic_msg_id="semantic-partial-refresh-failure",
                file_path=f"viking://resources/demo/{index}.txt",
            )
            for index in range(2)
        ]
        executor._root_done.set()

    executor._schedule_dir = seed_vectorization

    try:
        with pytest.raises(RuntimeError, match="task store unavailable"):
            await asyncio.wait_for(executor.run("viking://resources/demo"), timeout=1)

        assert tracker._tasks["semantic-partial-refresh-failure"].remaining == 1
        resource_lock.close.assert_not_awaited()

        await tracker.decrement("semantic-partial-refresh-failure")

        assert "semantic-partial-refresh-failure" not in tracker._tasks
        assert resource_lock.close.await_count == 2
    finally:
        tracker._tasks.clear()


@pytest.mark.asyncio
async def test_semantic_processor_restores_cancelled_state_after_restart(monkeypatch):
    agfs = _FakeAgfs()
    original_tracker = TaskTracker(store=PersistentTaskStore(agfs))
    task = await original_tracker.create(
        "add_resource",
        resource_id="viking://resources/demo",
        account_id="acme",
        user_id="alice",
        task_id="task-1",
    )
    await original_tracker.cancel(task.task_id, account_id="acme", user_id="alice")
    set_task_tracker(TaskTracker(store=PersistentTaskStore(agfs)))
    resource_lock = MagicMock()
    resource_lock.close = AsyncMock()
    lock_scope = SimpleNamespace(lock=resource_lock, close=resource_lock.close)
    dag_constructed = False

    class FakeDagExecutor:
        stale = True

        def __init__(self, **kwargs):
            nonlocal dag_constructed
            dag_constructed = True

        async def run(self, _root_uri):
            raise AssertionError("cancelled work must not enter the semantic DAG")

        def get_stats(self):
            return SimpleNamespace()

    fake_fs = MagicMock()
    fake_fs.exists = AsyncMock(return_value=False)
    fake_fs._uri_to_path.return_value = "/local/acme/resources/demo"
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.get_viking_fs",
        lambda: fake_fs,
    )
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.SemanticLockScope.resolve",
        AsyncMock(return_value=lock_scope),
    )
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.SemanticDagExecutor",
        FakeDagExecutor,
    )
    processor = SemanticProcessor()
    processor._circuit_breaker.check = MagicMock()
    processor._enqueue_parent_refresh = AsyncMock()
    processor._sync_topdown_recursive = AsyncMock()
    msg = SemanticMsg(
        uri="viking://resources/demo",
        context_type="resource",
        account_id="acme",
        user_id="alice",
        role="user",
        source_task_id="task-1",
    )

    await processor.on_dequeue(msg.to_dict())

    assert not dag_constructed
    processor._circuit_breaker.check.assert_not_called()
    processor._sync_topdown_recursive.assert_not_awaited()
    resource_lock.close.assert_awaited_once()
    processor._enqueue_parent_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_semantic_refresh_separates_source_task_coalescing(monkeypatch):
    enqueued = []

    class FakeQueue:
        async def enqueue(self, msg):
            enqueued.append(msg)

    class FakeQueueManager:
        SEMANTIC = "Semantic"

        def get_queue(self, name, allow_create=False):
            assert name == self.SEMANTIC
            assert allow_create is True
            return FakeQueue()

    monkeypatch.setattr(
        "openviking.storage.queuefs.get_queue_manager",
        lambda: FakeQueueManager(),
    )
    processor = SemanticProcessor()
    first_msg = SemanticMsg(
        uri="viking://resources/demo/child",
        context_type="resource",
        account_id="acme",
        user_id="alice",
        role="user",
        source_task_id="task-1",
    )
    second_msg = SemanticMsg(
        uri=first_msg.uri,
        context_type="resource",
        account_id="acme",
        user_id="alice",
        role="user",
        source_task_id="task-2",
    )

    await processor._enqueue_parent_refresh(first_msg, first_msg.uri)
    await processor._enqueue_parent_refresh(second_msg, second_msg.uri)

    assert len(enqueued) == 2
    assert enqueued[0].source_task_id == "task-1"
    assert enqueued[1].source_task_id == "task-2"
    assert enqueued[0].coalesce_key != enqueued[1].coalesce_key


@pytest.mark.asyncio
async def test_semantic_dag_passes_source_task_id_to_vectorizers(monkeypatch):
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_viking_fs",
        MagicMock,
    )
    processor = MagicMock()
    processor._vectorize_single_file = AsyncMock()
    processor._vectorize_directory = AsyncMock()
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=ctx,
        source_task_id="task-1",
    )

    await executor._run_vectorize_task(
        VectorizeTask(
            task_type="file",
            uri="viking://resources/demo/readme.md",
            context_type="resource",
            ctx=ctx,
            file_path="viking://resources/demo/readme.md",
            summary_dict={"name": "readme.md", "summary": "summary"},
            parent_uri="viking://resources/demo",
        )
    )
    await executor._run_vectorize_task(
        VectorizeTask(
            task_type="directory",
            uri="viking://resources/demo",
            context_type="resource",
            ctx=ctx,
            abstract="abstract",
            overview="overview",
        )
    )

    assert processor._vectorize_single_file.await_args.kwargs["source_task_id"] == "task-1"
    assert processor._vectorize_directory.await_args.kwargs["source_task_id"] == "task-1"
