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


class _FakePathLock:
    def __init__(self, *, busy_tree_paths=None):
        self._next_id = 0
        self.acquired_exact_paths: list[str] = []
        self.acquired_tree_paths: list[str] = []
        self.tree_attempts: list[tuple[str, float]] = []
        self.busy_tree_paths = set(busy_tree_paths or [])
        self.released: list[dict] = []
        self.release_failures: set[str] = set()

    def _new_lease(self):
        self._next_id += 1
        return {"id": f"lock-{self._next_id}"}

    async def pathlock_acquire_exact(self, path, timeout_secs=0.0):
        from openviking.storage.errors import LockAcquisitionError

        if path in self.busy_tree_paths:
            raise LockAcquisitionError(f"busy: {path}")
        self.acquired_exact_paths.append(path)
        return self._new_lease()

    async def pathlock_acquire_tree(
        self,
        path,
        timeout_secs=0.0,
        owner_lease_ref=None,
    ):
        from openviking.storage.errors import LockAcquisitionError

        self.tree_attempts.append((path, timeout_secs))
        if path in self.busy_tree_paths:
            raise LockAcquisitionError(f"busy: {path}")
        self.acquired_tree_paths.append(path)
        return self._new_lease()

    async def pathlock_release(self, lease):
        self.released.append(lease)
        if lease["id"] in self.release_failures:
            raise RuntimeError(f"release failed: {lease['id']}")

    async def pathlock_to_handoff(self, lease):
        return {"handoff_ref": lease["id"]}


class _FakeVikingFS:
    def __init__(self, *, exists_result=False, existing_uris=None, pathlock=None):
        self.agfs = SimpleNamespace(
            write=MagicMock(return_value={"status": "ok"}),
        )
        self._async_agfs = pathlock or _FakePathLock()
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

    async def delete_temp(self, temp_dir_path, ctx=None, lease_ref=None):
        self.delete_temp_calls.append((temp_dir_path, lease_ref))
        return None

    async def persist_temp_tree(self, temp_uri, target_uri, ctx=None, lease_ref=None):
        self.persist_calls.append((temp_uri, target_uri, lease_ref))
        self.agfs.write(self._uri_to_path(target_uri, ctx=ctx), b"content")

    async def rm(self, uri, recursive=False, ctx=None, lease_ref=None):
        self.rm_calls.append((uri, recursive, lease_ref))
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
    summarize_calls = []

    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)

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
    assert fake_fs.persist_calls == [
        ("viking://temp/root_tmp", "viking://resources/root", {"id": "lock-1"})
    ]
    assert fake_fs.delete_temp_calls == [("viking://temp/tmpdir", None)]
    assert summarize_calls[0]["temp_uris"] == ["viking://resources/root"]
    assert summarize_calls[0]["target_preexisting"] is False


@pytest.mark.asyncio
async def test_resource_processor_second_add_preserves_temp_uri_for_incremental(monkeypatch):
    from openviking.utils.resource_processor import ResourceProcessor

    fake_fs = _FakeVikingFS(exists_result=True)
    summarize_calls = []

    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)

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

    fake_pathlock = _FakePathLock(busy_tree_paths={"/mock/resources/root_1"})
    fake_fs = _FakeVikingFS(
        existing_uris={"viking://resources/root"},
        pathlock=fake_pathlock,
    )
    events = []

    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)

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

    async def record_rollback_target(root_uri, target_created, lock_handoff):
        assert fake_fs.persist_calls == []
        if target_created is True:
            assert fake_pathlock.released == []
        events.append(("rollback_target", root_uri, target_created, lock_handoff))

    result = await rp.process_resource(
        path="x",
        ctx=object(),
        build_index=True,
        source_task_id="task-1",
        rollback_lock_handoff_callback=record_rollback_target,
    )

    assert result["status"] == "success"
    assert result["root_uri"] == "viking://resources/root_2"
    assert fake_fs.exists_calls == [
        "viking://resources/root",
        "viking://resources/root_1",
        "viking://resources/root_2",
        "viking://resources/root_2",
        "viking://resources/root_2",
    ]
    assert fake_pathlock.tree_attempts == [("/mock/resources/root_2", 0.0)]
    assert fake_pathlock.acquired_exact_paths == ["/mock/resources/root_2"]
    assert fake_pathlock.acquired_tree_paths == ["/mock/resources/root_2"]
    assert events[0] == (
        "rollback_target",
        "viking://resources/root_2",
        None,
        {"handoff_ref": "lock-1"},
    )
    assert events[1] == (
        "rollback_target",
        "viking://resources/root_2",
        True,
        {"handoff_ref": "lock-2"},
    )
    assert events[2][0] == "summarize"
    assert events[2][1]["temp_uris"] == ["viking://resources/root_2"]
    assert events[2][1]["target_preexisting"] is False
    assert result["_target_created"] is True
    assert fake_fs.persist_calls[0][0:2] == (
        "viking://temp/root_tmp",
        "viking://resources/root_2",
    )


@pytest.mark.asyncio
async def test_candidate_delete_authority_is_not_granted_when_tree_materialization_loses(
    monkeypatch,
):
    from openviking.storage.errors import ResourceBusyError
    from openviking.utils.resource_processor import ResourceProcessor

    fake_pathlock = _FakePathLock()
    fake_fs = _FakeVikingFS(pathlock=fake_pathlock)
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)

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
            fake_pathlock.busy_tree_paths.add("/mock/resources/resolved")

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

    fake_pathlock = _FakePathLock()
    fake_fs = _FakeVikingFS(pathlock=fake_pathlock)
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)

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
        target_existed_before_persistence = bool(fake_pathlock.acquired_tree_paths)
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
    assert fake_pathlock.released == [{"id": "lock-2"}, {"id": "lock-1"}]


@pytest.mark.asyncio
async def test_release_lock_refs_attempts_every_lease_before_raising():
    from openviking.utils.resource_processor import _release_lock_refs

    async_agfs = SimpleNamespace(
        pathlock_release=AsyncMock(
            side_effect=[RuntimeError("tree release failed"), None],
        )
    )
    tree_lock = {"id": "tree-lock"}
    exact_lock = {"id": "exact-lock"}

    with pytest.raises(RuntimeError, match="tree release failed"):
        await _release_lock_refs(async_agfs, (tree_lock, exact_lock))

    assert [awaited.args[0] for awaited in async_agfs.pathlock_release.await_args_list] == [
        tree_lock,
        exact_lock,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_with_exact_lease", "expected_persisted_ownership", "expected_target_created"),
    [(True, [None, True], True), (False, [False], False)],
)
async def test_pending_candidate_replay_reclaims_empty_materialized_target(
    monkeypatch,
    start_with_exact_lease,
    expected_persisted_ownership,
    expected_target_created,
):
    from openviking.utils.resource_processor import ResourceProcessor

    fake_pathlock = _FakePathLock()
    fake_fs = _FakeVikingFS(exists_result=True, pathlock=fake_pathlock)
    target_path = "/mock/resources/resolved"
    monkeypatch.setattr(
        "openviking.utils.resource_processor.get_current_telemetry",
        lambda: _DummyTelemetry(),
    )
    _patch_viking_fs(monkeypatch, fake_fs)

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
        resource_lock = await fake_pathlock.pathlock_acquire_exact(
            target_path,
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

    assert fake_pathlock.acquired_tree_paths == [target_path]
    assert persisted_ownership == expected_persisted_ownership
    assert result["_target_created"] is expected_target_created
    assert fake_fs.persist_calls[0][0:2] == (
        "viking://temp/resolved_tmp",
        "viking://resources/resolved",
    )
