# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import importlib
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.session.session import (
    Session,
    _CheckpointRequest,
    _phase2_message_is_completed,
    _phase2_window_peer_ids,
    _record_fully_completed_phase2_sources,
)
from openviking_cli.exceptions import ResourceExhaustedError
from openviking_cli.utils.config.memory_config import MemoryConfig


def test_phase2_plan_preserves_authored_text_across_windows() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    first_text = "alpha " * 80
    second_text = "omega " * 80
    messages = [
        Message(id="user-1", role="user", parts=[TextPart(text=first_text)]),
        Message(id="assistant-1", role="assistant", parts=[TextPart(text=second_text)]),
    ]

    plan = phase2_input.build_phase2_input_plan(messages, window_max_tokens=40)

    fragments = [
        part.text
        for window in plan.windows
        for message in window.messages
        for part in message.parts
        if isinstance(part, TextPart)
    ]
    assert "".join(fragments) == first_text + second_text
    assert len(plan.windows) > 1
    assert all(window.estimated_tokens <= 40 for window in plan.windows)


def test_phase2_plan_bounds_tool_output_head_and_tail_without_mutating_source() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    output = "HEAD-" + ("middle-" * 2_000) + "-TAIL"
    message = Message(
        id="assistant-tool",
        role="assistant",
        parts=[
            ToolPart(
                tool_id="call-1",
                tool_name="read",
                tool_output=output,
                tool_output_ref="viking://tool-results/call-1",
                tool_status="completed",
            )
        ],
    )

    plan = phase2_input.build_phase2_input_plan([message], window_max_tokens=200)

    planned_part = plan.windows[0].messages[0].parts[0]
    assert isinstance(planned_part, ToolPart)
    assert planned_part.tool_output.startswith("HEAD-")
    assert planned_part.tool_output.endswith("-TAIL")
    assert "middle omitted from Phase 2 prompt" in planned_part.tool_output
    assert planned_part.tool_output_truncated is True
    assert planned_part.tool_output_original_chars == len(output)
    assert message.parts[0].tool_output == output
    assert plan.windows[0].estimated_tokens <= 200


def test_phase2_plan_preserves_structured_tool_input() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    tool_input = {
        "command": "python -c " + ("print('important') " * 2_000),
        "cwd": "/workspace",
    }
    message = Message(
        id="assistant-tool-input",
        role="assistant",
        parts=[
            ToolPart(
                tool_id="call-1",
                tool_name="shell",
                tool_input=tool_input,
                tool_output="completed",
                tool_status="completed",
            )
        ],
    )

    with pytest.raises(ValueError, match="metadata exceeds"):
        phase2_input.build_phase2_input_plan([message], window_max_tokens=200)

    assert message.parts[0].tool_input == tool_input


def test_phase2_plan_keeps_a_fitting_turn_together() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    messages = [
        Message(
            id="user-1",
            role="user",
            turn_id="turn-1",
            parts=[TextPart(text="question")],
        ),
        Message(
            id="assistant-1",
            role="assistant",
            turn_id="turn-1",
            parts=[TextPart(text="answer")],
        ),
        Message(
            id="user-2",
            role="user",
            turn_id="turn-2",
            parts=[TextPart(text="later " * 40)],
        ),
    ]

    plan = phase2_input.build_phase2_input_plan(messages, window_max_tokens=20)

    assert plan.windows[0].source_message_ids == ("user-1", "assistant-1")
    assert all(window.estimated_tokens <= 20 for window in plan.windows)


def test_phase2_plan_fragment_ids_are_deterministic() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    messages = [
        Message(
            id="user-large",
            role="user",
            parts=[TextPart(text="stable content " * 200)],
        )
    ]

    first = phase2_input.build_phase2_input_plan(messages, window_max_tokens=40)
    second = phase2_input.build_phase2_input_plan(messages, window_max_tokens=40)

    assert [window.fragment_ids for window in first.windows] == [
        window.fragment_ids for window in second.windows
    ]
    assert all(
        fragment_id.startswith("user-large#phase2:v1:")
        for window in first.windows
        for fragment_id in window.fragment_ids
    )


def test_compacted_tool_fragment_id_is_deterministic_without_created_at() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    messages = [
        Message(
            id="tool-large",
            role="assistant",
            parts=[
                ToolPart(
                    tool_id="call-1",
                    tool_name="read",
                    tool_input={"uri": "viking://user/resources/report.md"},
                    tool_output="stable output " * 2_000,
                    tool_status="completed",
                )
            ],
        )
    ]

    first = phase2_input.build_phase2_input_plan(messages, window_max_tokens=200)
    second = phase2_input.build_phase2_input_plan(messages, window_max_tokens=200)

    assert first.windows[0].fragment_ids == second.windows[0].fragment_ids


def test_phase2_plan_rejects_non_positive_budget() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")

    with pytest.raises(ValueError, match="greater than zero"):
        phase2_input.build_phase2_input_plan([], window_max_tokens=0)


def test_memory_config_exposes_safe_phase2_defaults() -> None:
    config = MemoryConfig()

    assert config.phase2_window_max_tokens == 12_000
    assert config.extraction_request_max_tokens == 32_768


def test_memory_config_rejects_request_budget_below_window_budget() -> None:
    with pytest.raises(ValueError, match="extraction_request_max_tokens"):
        MemoryConfig(
            phase2_window_max_tokens=2_000,
            extraction_request_max_tokens=1_999,
        )


@pytest.mark.asyncio
async def test_working_memory_folds_phase2_windows_in_order() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    messages = [
        Message(id="user-1", role="user", parts=[TextPart(text="first " * 20)]),
        Message(id="user-2", role="user", parts=[TextPart(text="second " * 20)]),
    ]
    plan = phase2_input.build_phase2_input_plan(messages, window_max_tokens=40)
    session = object.__new__(Session)
    session._generate_archive_summary_async = AsyncMock(
        side_effect=["overview-after-first", "overview-after-second"]
    )

    result = await session._generate_archive_summary_for_phase2_windows(
        plan.windows,
        latest_archive_overview="overview-before",
        request_max_tokens=300,
    )

    assert result == "overview-after-second"
    assert session._generate_archive_summary_async.await_count == 2
    first_call, second_call = session._generate_archive_summary_async.await_args_list
    assert first_call.kwargs["latest_archive_overview"] == "overview-before"
    assert second_call.kwargs["latest_archive_overview"] == "overview-after-first"
    assert first_call.kwargs["strict"] is True
    assert second_call.kwargs["strict"] is True
    assert first_call.kwargs["request_max_tokens"] == 300


@pytest.mark.asyncio
async def test_single_window_working_memory_uses_explicit_request_budget() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    plan = phase2_input.build_phase2_input_plan(
        [Message(id="user-1", role="user", parts=[TextPart(text="remember this")])],
        window_max_tokens=40,
    )
    session = object.__new__(Session)
    session._generate_archive_summary_async = AsyncMock(return_value="overview")

    await session._generate_archive_summary_for_phase2_windows(
        plan.windows,
        request_max_tokens=300,
    )

    assert session._generate_archive_summary_async.await_args.kwargs[
        "request_max_tokens"
    ] == 300


@pytest.mark.asyncio
async def test_single_window_checkpoint_uses_fragment_ids() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    source = Message(
        id="tool-source",
        role="assistant",
        parts=[
            ToolPart(
                tool_id="call-1",
                tool_name="read",
                tool_output="HEAD-" + ("middle-" * 2_000) + "-TAIL",
            )
        ],
    )
    plan = phase2_input.build_phase2_input_plan([source], window_max_tokens=200)
    assert len(plan.windows) == 1
    derived_id = plan.windows[0].messages[0].id
    assert derived_id != source.id

    session = object.__new__(Session)
    session._generate_archive_summary_async = AsyncMock(return_value="overview")
    request = _CheckpointRequest(
        turn_anchor_message_id=source.id,
        source_message_ids=(source.id,),
        retained_message_token_budget=100,
        estimated_active_tokens=10,
    )

    await session._generate_archive_summary_for_phase2_windows(
        plan.windows,
        checkpoint_requests=[request],
        request_max_tokens=300,
    )

    local_request = session._generate_archive_summary_async.await_args.kwargs[
        "checkpoint_requests"
    ][0]
    assert local_request.turn_anchor_message_id == derived_id
    assert local_request.source_message_ids == (derived_id,)


@pytest.mark.asyncio
async def test_checkpoint_partials_are_reduced_pairwise_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingVLM:
        def __init__(self) -> None:
            self.requests = []

        async def get_completion_async(self, **request):
            self.requests.append(request)
            return "merged checkpoint"

    vlm = RecordingVLM()
    config = type("Config", (), {"vlm": vlm})()
    monkeypatch.setattr(
        "openviking.session.session.get_openviking_config",
        lambda: config,
    )
    session = object.__new__(Session)

    result = await session._reduce_checkpoint_summaries(
        ["first concrete fact", "second concrete fact"],
        request_max_tokens=300,
    )

    assert result == "merged checkpoint"
    assert len(vlm.requests) == 1
    assert "first concrete fact" in vlm.requests[0]["prompt"]
    assert "second concrete fact" in vlm.requests[0]["prompt"]


@pytest.mark.asyncio
async def test_checkpoint_reduction_fails_before_oversized_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingVLM:
        called = False

        async def get_completion_async(self, **request):
            self.called = True
            return "unused"

    vlm = RecordingVLM()
    config = type("Config", (), {"vlm": vlm})()
    monkeypatch.setattr(
        "openviking.session.session.get_openviking_config",
        lambda: config,
    )
    session = object.__new__(Session)

    with pytest.raises(ResourceExhaustedError, match="checkpoint reduction"):
        await session._reduce_checkpoint_summaries(
            ["first " * 1_000, "second " * 1_000],
            request_max_tokens=100,
        )

    assert vlm.called is False


@pytest.mark.asyncio
async def test_single_window_working_memory_budget_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingVLM:
        called = False

        def is_available(self) -> bool:
            return True

        async def get_completion_async(self, **request):
            self.called = True
            return "unused"

    vlm = RecordingVLM()
    config = type(
        "Config",
        (),
        {
            "vlm": vlm,
            "memory": type(
                "Memory",
                (),
                {"extraction_request_max_tokens": 100},
            )(),
        },
    )()
    monkeypatch.setattr(
        "openviking.session.session.get_openviking_config",
        lambda: config,
    )
    session = object.__new__(Session)
    message = Message(
        id="large",
        role="user",
        parts=[TextPart(text="authored " * 2_000)],
    )

    with pytest.raises(ResourceExhaustedError, match="Working Memory request"):
        await session._generate_archive_summary_async([message])

    assert vlm.called is False


def test_phase2_window_peer_scope_excludes_other_windows() -> None:
    messages = [
        Message(
            id="peer-a-message",
            role="user",
            peer_id="peer-a",
            parts=[TextPart(text="hello")],
        )
    ]

    assert _phase2_window_peer_ids({"peer-a", "peer-b"}, messages) == {"peer-a"}


def test_completed_source_id_survives_a_different_retry_fragmentation() -> None:
    phase2_input = importlib.import_module("openviking.session.phase2_input")
    source = Message(
        id="large-source",
        role="user",
        parts=[TextPart(text="durable authored text " * 200)],
    )
    original_plan = phase2_input.build_phase2_input_plan(
        [source],
        window_max_tokens=40,
    )
    completed_ids = {message.id for window in original_plan.windows for message in window.messages}

    _record_fully_completed_phase2_sources(original_plan, completed_ids)

    retry_plan = phase2_input.build_phase2_input_plan(
        [source],
        window_max_tokens=60,
    )
    assert source.id in completed_ids
    assert all(
        _phase2_message_is_completed(message, completed_ids)
        for window in retry_plan.windows
        for message in window.messages
    )


@pytest.mark.asyncio
async def test_usage_reporting_hydrates_a_separate_non_model_copy() -> None:
    source = [
        Message(
            id="source",
            role="assistant",
            parts=[TextPart(text="model-facing preview")],
        )
    ]
    hydrated = [
        Message(
            id="usage-copy",
            role="assistant",
            parts=[TextPart(text="full tool output for rule-based usage")],
        )
    ]
    session = object.__new__(Session)
    session._usage_reporter = object()
    session._hydrate_tool_outputs_for_extraction = AsyncMock(return_value=hydrated)

    result = await session._usage_reporting_messages(source)

    assert result is hydrated
    assert source[0].parts[0].text == "model-facing preview"
    session._hydrate_tool_outputs_for_extraction.assert_awaited_once_with(source)
