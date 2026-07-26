import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class _DummyVikingDB:
    def get_embedder(self):
        return None


class _DummyTelemetry:
    def set(self, *args, **kwargs):
        return None

    def set_error(self, *args, **kwargs):
        return None

    class _Measure:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def measure(self, *args, **kwargs):
        return self._Measure()


class _CtxMgr:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeLockManager:
    def __init__(self, *, busy_tree_paths=None, existing_tree_paths=None):
        from openviking.storage.transaction.lock_handle import LockHandle

        self._lock_handle_cls = LockHandle
        self._handles = {}
        self.acquired_exact_paths = []
        self.acquired_tree_paths = []
        self.tree_attempts = []
        self.busy_tree_paths = set(busy_tree_paths or [])
        self.existing_tree_paths = set(existing_tree_paths or [])

    def create_handle(self):
        handle = self._lock_handle_cls()
        self._handles[handle.id] = handle
        return handle

    async def acquire_exact_path_batch(self, handle, paths, timeout=None):
        if any(path in self.busy_tree_paths for path in paths):
            return False
        for path in paths:
            lock_path = f"exact:{path}"
            handle.add_lock(lock_path)
            self.acquired_exact_paths.append(path)
        return True

    async def acquire_tree(self, handle, path, timeout=None):
        self.tree_attempts.append((path, timeout))
        if path in self.busy_tree_paths:
            return False
        lock_path = f"tree:{path}"
        handle.add_lock(lock_path)
        if path not in self.existing_tree_paths:
            handle.created_paths.append(path)
        self.acquired_tree_paths.append(path)
        return True

    async def release_selected(self, handle, lock_paths):
        for path in lock_paths:
            handle.remove_lock(path)

    async def release(self, handle):
        for path in list(handle.locks):
            handle.remove_lock(path)
        self._handles.pop(handle.id, None)

    def get_handle(self, handle_id):
        handle = self._handles.get(handle_id)
        if handle and handle.locks:
            return handle
        return None


class _FakeVikingFS:
    def __init__(self, *, exists_result=False, existing_uris=None):
        self.agfs = SimpleNamespace(
            write=MagicMock(return_value={"status": "ok"}),
        )
        self._exists_result = exists_result
        self._existing_uris = set(existing_uris or [])
        self.exists_calls = []
        self.persist_calls = []
        self.delete_temp_calls = []
        self.rm_calls = []

    def bind_request_context(self, ctx):
        return _CtxMgr()

    async def exists(self, uri, ctx=None):
        self.exists_calls.append(uri)
        if self._existing_uris:
            return uri in self._existing_uris
        return self._exists_result

    async def mkdir(self, uri, exist_ok=False, ctx=None):
        return None

    async def delete_temp(self, temp_dir_path, ctx=None):
        self.delete_temp_calls.append(temp_dir_path)
        return None

    async def persist_temp_tree(self, temp_uri, target_uri, ctx=None):
        self.persist_calls.append((temp_uri, target_uri))
        self.agfs.write(self._uri_to_path(target_uri, ctx=ctx), b"content")

    async def rm(self, uri, recursive=False, ctx=None, lock_handle=None):
        self.rm_calls.append((uri, recursive, lock_handle))
        return {"status": "ok"}

    async def glob(self, pattern, uri=None, ctx=None):
        return {"matches": []}

    def _uri_to_path(self, uri, ctx=None):
        return f"/mock/{uri.replace('viking://', '')}"


def _patch_viking_fs(monkeypatch, fake_fs):
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: fake_fs)
    monkeypatch.setattr("openviking.parse.image_rewrite.get_viking_fs", lambda: fake_fs)


@pytest.mark.asyncio
async def test_resource_processor_first_add_summarizes_from_committed_uri(monkeypatch):
    from openviking.utils.resource_processor import ResourceProcessor

    fake_fs = _FakeVikingFS()
    fake_lock_manager = _FakeLockManager()
    summarize_calls = []

    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: fake_lock_manager,
    )

    rp = ResourceProcessor(vikingdb=_DummyVikingDB(), media_storage=None)
    rp._get_media_processor = MagicMock()
    rp._get_media_processor.return_value.process = AsyncMock(
        return_value=SimpleNamespace(
            temp_dir_path="viking://temp/tmpdir",
            source_path="x",
            source_format="text",
            meta={},
            warnings=[],
        )
    )
    rp.tree_builder.finalize_from_temp = AsyncMock(
        return_value=SimpleNamespace(
            root=SimpleNamespace(uri="viking://resources/root", temp_uri="viking://temp/root_tmp")
        )
    )
    rp._summarizer = SimpleNamespace(
        summarize=AsyncMock(
            side_effect=lambda *args, **kwargs: (
                summarize_calls.append(kwargs) or {"status": "success"}
            )
        )
    )

    result = await rp.process_resource(path="x", ctx=object(), build_index=True)

    assert result["status"] == "success"
    assert result["root_uri"] == "viking://resources/root"
    assert fake_fs.persist_calls == [("viking://temp/root_tmp", "viking://resources/root")]
    assert fake_fs.delete_temp_calls == ["viking://temp/tmpdir"]
    assert summarize_calls[0]["temp_uris"] == ["viking://resources/root"]
    assert summarize_calls[0]["target_preexisting"] is False


@pytest.mark.asyncio
async def test_resource_processor_second_add_preserves_temp_uri_for_incremental(monkeypatch):
    from openviking.utils.resource_processor import ResourceProcessor

    fake_fs = _FakeVikingFS(exists_result=True)
    fake_lock_manager = _FakeLockManager()
    summarize_calls = []

    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: fake_lock_manager,
    )

    rp = ResourceProcessor(vikingdb=_DummyVikingDB(), media_storage=None)
    rp._get_media_processor = MagicMock()
    rp._get_media_processor.return_value.process = AsyncMock(
        return_value=SimpleNamespace(
            temp_dir_path="viking://temp/tmpdir",
            source_path="x",
            source_format="text",
            meta={},
            warnings=[],
        )
    )

    context_tree = SimpleNamespace(
        root=SimpleNamespace(uri="viking://resources/root", temp_uri="viking://temp/root_tmp")
    )
    rp.tree_builder.finalize_from_temp = AsyncMock(return_value=context_tree)
    rp._summarizer = SimpleNamespace(
        summarize=AsyncMock(
            side_effect=lambda *args, **kwargs: (
                summarize_calls.append(kwargs) or {"status": "success"}
            )
        )
    )

    result = await rp.process_resource(path="x", ctx=object(), build_index=True)

    assert result["status"] == "success"
    assert result["root_uri"] == "viking://resources/root"
    assert summarize_calls[0]["temp_uris"] == ["viking://temp/root_tmp"]
    assert summarize_calls[0]["target_preexisting"] is True
    assert fake_fs.persist_calls == []


@pytest.mark.asyncio
async def test_resource_processor_auto_candidate_skips_existing_and_busy(monkeypatch):
    from openviking.utils.resource_processor import ResourceProcessor

    fake_fs = _FakeVikingFS(existing_uris={"viking://resources/root"})
    fake_lock_manager = _FakeLockManager(busy_tree_paths={"/mock/resources/root_1"})
    events = []

    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: fake_lock_manager,
    )

    rp = ResourceProcessor(vikingdb=_DummyVikingDB(), media_storage=None)
    rp._get_media_processor = MagicMock()
    rp._get_media_processor.return_value.process = AsyncMock(
        return_value=SimpleNamespace(
            temp_dir_path="viking://temp/tmpdir",
            source_path="x",
            source_format="text",
            meta={},
            warnings=[],
        )
    )

    context_tree = SimpleNamespace(
        root=SimpleNamespace(uri="viking://resources/root", temp_uri="viking://temp/root_tmp"),
        _candidate_uri="viking://resources/root",
    )
    rp.tree_builder.finalize_from_temp = AsyncMock(return_value=context_tree)
    rp._summarizer = SimpleNamespace(
        summarize=AsyncMock(
            side_effect=lambda *args, **kwargs: (
                events.append(("summarize", kwargs)) or {"status": "success"}
            )
        )
    )

    async def record_rollback_target(root_uri, target_created):
        assert fake_fs.persist_calls == []
        events.append(("rollback_target", root_uri, target_created))

    result = await rp.process_resource(
        path="x",
        ctx=object(),
        build_index=True,
        source_task_id="task-1",
        rollback_target_callback=record_rollback_target,
    )

    assert result["status"] == "success"
    assert result["root_uri"] == "viking://resources/root_2"
    assert fake_fs.exists_calls == [
        "viking://resources/root",
        "viking://resources/root_1",
        "viking://resources/root_2",
        "viking://resources/root_2",
    ]
    assert fake_lock_manager.tree_attempts == [("/mock/resources/root_2", 0.0)]
    assert fake_lock_manager.acquired_exact_paths == ["/mock/resources/root_2"]
    assert fake_lock_manager.acquired_tree_paths == ["/mock/resources/root_2"]
    assert events[0] == ("rollback_target", "viking://resources/root_2", None)
    assert events[1] == ("rollback_target", "viking://resources/root_2", True)
    assert events[2][0] == "summarize"
    assert events[2][1]["temp_uris"] == ["viking://resources/root_2"]
    assert events[2][1]["target_preexisting"] is False
    assert result["_target_created"] is True
    assert fake_fs.persist_calls == [("viking://temp/root_tmp", "viking://resources/root_2")]


@pytest.mark.asyncio
async def test_candidate_delete_authority_is_not_granted_when_tree_materialization_loses(
    monkeypatch,
):
    from openviking.storage.errors import ResourceBusyError
    from openviking.utils.resource_processor import ResourceProcessor

    fake_fs = _FakeVikingFS()
    fake_lock_manager = _FakeLockManager()
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: fake_lock_manager,
    )

    processor = ResourceProcessor(vikingdb=_DummyVikingDB(), media_storage=None)
    processor._get_media_processor = MagicMock()
    processor._get_media_processor.return_value.process = AsyncMock(
        return_value=SimpleNamespace(
            temp_dir_path="viking://temp/tmpdir",
            source_path="x",
            source_format="text",
            meta={},
            warnings=[],
        )
    )
    processor.tree_builder.finalize_from_temp = AsyncMock(
        return_value=SimpleNamespace(
            root=SimpleNamespace(
                uri="viking://resources/resolved",
                temp_uri="viking://temp/resolved_tmp",
            ),
            _candidate_uri="viking://resources/resolved",
        )
    )
    processor._summarizer = SimpleNamespace(summarize=AsyncMock())
    persisted_ownership = []

    async def persist_rollback_target(_root_uri, target_created):
        persisted_ownership.append(target_created)
        if target_created is None:
            fake_lock_manager.busy_tree_paths.add("/mock/resources/resolved")

    with pytest.raises(ResourceBusyError, match="could not be materialized"):
        await processor.process_resource(
            path="x",
            ctx=object(),
            build_index=True,
            rollback_target_callback=persist_rollback_target,
        )

    assert persisted_ownership == [None]
    assert fake_fs.persist_calls == []
    assert fake_fs.rm_calls == []


@pytest.mark.asyncio
async def test_rollback_target_persistence_failure_rolls_back_and_releases_lock(monkeypatch):
    from openviking.utils.resource_processor import (
        ResourceProcessor,
        RollbackTargetPersistenceError,
    )

    fake_fs = _FakeVikingFS()
    fake_lock_manager = _FakeLockManager()
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: fake_lock_manager,
    )

    processor = ResourceProcessor(vikingdb=_DummyVikingDB(), media_storage=None)
    processor._get_media_processor = MagicMock()
    processor._get_media_processor.return_value.process = AsyncMock(
        return_value=SimpleNamespace(
            temp_dir_path="viking://temp/tmpdir",
            source_path="x",
            source_format="text",
            meta={},
            warnings=[],
        )
    )
    processor.tree_builder.finalize_from_temp = AsyncMock(
        return_value=SimpleNamespace(
            root=SimpleNamespace(
                uri="viking://resources/resolved",
                temp_uri="viking://temp/resolved_tmp",
            ),
            _candidate_uri="viking://resources/resolved",
        )
    )
    processor._summarizer = SimpleNamespace(summarize=AsyncMock())
    target_existed_before_persistence = None

    async def fail_persistence(_root_uri, target_created):
        nonlocal target_existed_before_persistence
        if target_created is None:
            return
        target_existed_before_persistence = bool(fake_lock_manager.acquired_tree_paths)
        raise RuntimeError("task persistence failed")

    with pytest.raises(RollbackTargetPersistenceError, match="task persistence failed"):
        await processor.process_resource(
            path="x",
            ctx=object(),
            build_index=True,
            rollback_target_callback=fail_persistence,
        )

    assert fake_fs.persist_calls == []
    assert target_existed_before_persistence is True
    assert len(fake_fs.rm_calls) == 1
    assert fake_fs.rm_calls[0][0:2] == ("viking://resources/resolved", True)
    assert fake_fs.rm_calls[0][2] is not None
    assert processor._summarizer.summarize.await_count == 0
    assert not any(handle.locks for handle in fake_lock_manager._handles.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("start_with_exact_lease", [True, False])
async def test_pending_candidate_replay_reclaims_empty_materialized_target(
    monkeypatch,
    start_with_exact_lease,
):
    from openviking.storage.transaction import OwnedLockLease
    from openviking.utils.resource_processor import ResourceProcessor

    fake_fs = _FakeVikingFS(exists_result=True)
    target_path = "/mock/resources/resolved"
    fake_lock_manager = _FakeLockManager(existing_tree_paths={target_path})
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)
    monkeypatch.setattr(
        "openviking.storage.transaction.get_lock_manager",
        lambda: fake_lock_manager,
    )

    processor = ResourceProcessor(vikingdb=_DummyVikingDB(), media_storage=None)
    processor._get_media_processor = MagicMock()
    processor._get_media_processor.return_value.process = AsyncMock(
        return_value=SimpleNamespace(
            temp_dir_path="viking://temp/tmpdir",
            source_path="x",
            source_format="text",
            meta={},
            warnings=[],
        )
    )
    processor.tree_builder.finalize_from_temp = AsyncMock(
        return_value=SimpleNamespace(
            root=SimpleNamespace(
                uri="viking://resources/resolved",
                temp_uri="viking://temp/resolved_tmp",
            )
        )
    )
    monkeypatch.setattr(
        ResourceProcessor,
        "target_contains_preexisting_data",
        AsyncMock(return_value=False),
    )
    processor._summarizer = SimpleNamespace(summarize=AsyncMock(return_value={"status": "success"}))
    resource_lock = None
    if start_with_exact_lease:
        resource_lock = await OwnedLockLease.acquire_exact_paths(
            fake_lock_manager,
            [target_path],
        )
    persisted_ownership = []

    async def persist_rollback_target(_root_uri, target_created):
        persisted_ownership.append(target_created)

    result = await processor.process_resource(
        path="x",
        ctx=object(),
        to="viking://resources/resolved",
        build_index=True,
        resource_lock=resource_lock,
        target_materialization_pending=True,
        rollback_target_callback=persist_rollback_target,
    )

    assert fake_lock_manager.acquired_tree_paths == [target_path]
    assert persisted_ownership == [True]
    assert result["_target_created"] is True
    assert fake_fs.persist_calls == [("viking://temp/resolved_tmp", "viking://resources/resolved")]
