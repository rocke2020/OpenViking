# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from types import SimpleNamespace

import pytest

from openviking.session.compressor_v2 import SessionCompressorV2
from openviking.session.compressor_v3 import SessionCompressorV3
from openviking.session.memory.dataclass import ResolvedOperations
from openviking.session.memory.streaming_memory_updater import (
    MemoryUpdateRequest,
    StreamingMemoryUpdater,
)
from openviking.session.train import PatchMergePolicyOptimizerContext, PipelineContext
from openviking.session.train.components import policy_trainer
from openviking.session.train.components.gradient_estimator import ExperienceGradientContext


@pytest.mark.asyncio
async def test_streaming_memory_updater_uses_strictest_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_merge_memory_operations(**kwargs):
        observed.update(kwargs)
        return kwargs["operations"]

    monkeypatch.setattr(
        "openviking.session.memory.streaming_memory_updater.merge_memory_operations",
        fake_merge_memory_operations,
    )
    updater = StreamingMemoryUpdater()
    empty_operations = ResolvedOperations(
        upsert_operations=[],
        delete_file_contents=[],
        errors=[],
    )
    requests = [
        MemoryUpdateRequest(
            operations=empty_operations,
            messages=[],
            ctx=SimpleNamespace(),
            request_max_tokens=None,
        ),
        MemoryUpdateRequest(
            operations=empty_operations,
            messages=[],
            ctx=SimpleNamespace(),
            request_max_tokens=400,
        ),
        MemoryUpdateRequest(
            operations=empty_operations,
            messages=[],
            ctx=SimpleNamespace(),
            request_max_tokens=200,
        ),
    ]

    await updater._merge_requests(requests)

    assert observed["request_max_tokens"] == 200


def test_new_budget_fields_preserve_positional_api_compatibility() -> None:
    operations = ResolvedOperations(
        upsert_operations=[],
        delete_file_contents=[],
        errors=[],
    )
    isolation_options = {"allow_self": False}
    metadata = {"caller": "metadata"}
    update_request = MemoryUpdateRequest(
        operations,
        [],
        SimpleNamespace(),
        True,
        isolation_options,
        metadata,
    )
    gradient_context = ExperienceGradientContext(
        SimpleNamespace(),
        [],
        True,
        metadata,
    )

    assert update_request.isolation_options == isolation_options
    assert update_request.metadata == metadata
    assert update_request.request_max_tokens is None
    assert gradient_context.metadata == metadata
    assert gradient_context.request_max_tokens is None


def test_streaming_policy_chunks_keep_strictest_item_budget() -> None:
    items = [
        policy_trainer._BufferedRolloutTraining(
            gradients=[object()],
            request_max_tokens=None,
        ),
        policy_trainer._BufferedRolloutTraining(
            gradients=[object()],
            request_max_tokens=400,
        ),
        policy_trainer._BufferedRolloutTraining(
            gradients=[object()],
            request_max_tokens=200,
        ),
    ]

    chunks = policy_trainer._chunks_buffered_items_by_gradient_count(items, size=16)

    assert chunks[0].request_max_tokens == 200


def test_streaming_policy_chunks_leave_unscoped_work_unbounded() -> None:
    items = [
        policy_trainer._BufferedRolloutTraining(
            gradients=[object()],
            request_max_tokens=None,
        )
    ]

    chunks = policy_trainer._chunks_buffered_items_by_gradient_count(items, size=16)

    assert chunks[0].request_max_tokens is None


def test_preexisting_policy_worker_context_is_scoped_per_chunk() -> None:
    original = PipelineContext(
        optimization_context=PatchMergePolicyOptimizerContext(
            request_context=SimpleNamespace(),
            request_max_tokens=None,
        )
    )

    scoped = policy_trainer._pipeline_context_with_request_budget(original, 200)

    assert scoped is not original
    assert scoped.optimization_context.request_max_tokens == 200
    assert original.optimization_context.request_max_tokens is None


def test_capped_created_policy_worker_does_not_cap_later_unscoped_work() -> None:
    capped_creation_context = PipelineContext(
        optimization_context=PatchMergePolicyOptimizerContext(
            request_context=SimpleNamespace(),
            request_max_tokens=200,
        )
    )

    trainer = policy_trainer.StreamingPolicyTrainer(
        policy_set=SimpleNamespace(),
        rollout_analyzer=SimpleNamespace(),
        gradient_estimator=SimpleNamespace(),
        policy_optimizer=SimpleNamespace(),
        policy_updater=SimpleNamespace(),
        context=capped_creation_context,
    )
    later_context = policy_trainer._pipeline_context_with_request_budget(
        trainer.context,
        None,
    )

    assert trainer.context.optimization_context.request_max_tokens is None
    assert later_context.optimization_context.request_max_tokens is None
    assert capped_creation_context.optimization_context.request_max_tokens == 200


def test_v3_user_extract_loop_receives_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    class RecordingExtractLoop:
        def __init__(self, **kwargs):
            observed.update(kwargs)

    config = SimpleNamespace(
        vlm=SimpleNamespace(get_vlm_instance=lambda: object()),
        memory=SimpleNamespace(
            eager_prefetch=False,
            prefetch_search_topn=5,
            link_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v3.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v3.get_viking_fs",
        lambda: object(),
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v3.ExtractLoop",
        RecordingExtractLoop,
    )
    compressor = SessionCompressorV3(
        vikingdb=None,
        rollout_analyzer=object(),
    )

    compressor._get_or_create_react(messages=[], request_max_tokens=321)

    assert observed["request_max_tokens"] == 321


def test_v2_user_extract_loop_receives_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    class RecordingExtractLoop:
        def __init__(self, **kwargs):
            observed.update(kwargs)

    config = SimpleNamespace(
        vlm=SimpleNamespace(get_vlm_instance=lambda: object()),
        memory=SimpleNamespace(
            eager_prefetch=False,
            prefetch_search_topn=5,
            link_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v2.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_openviking_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v2.get_viking_fs",
        lambda: object(),
    )
    monkeypatch.setattr(
        "openviking.session.compressor_v2.ExtractLoop",
        RecordingExtractLoop,
    )
    compressor = SessionCompressorV2(vikingdb=None)

    compressor._get_or_create_react(messages=[], request_max_tokens=321)

    assert observed["request_max_tokens"] == 321
