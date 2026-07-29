# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import importlib
import json

import pytest

from openviking.message import ImagePart, Message
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.vision_message_normalizer import describe_image_message
from openviking.utils.token_estimation import estimate_serialized_tokens
from openviking_cli.exceptions import ResourceExhaustedError


def test_request_budget_compacts_tool_result_head_and_tail_on_a_copy() -> None:
    request_budget = importlib.import_module("openviking.session.memory.request_budget")
    result = "HEAD-" + ("large-result-" * 2_000) + "-TAIL"
    messages = [
        {"role": "system", "content": "extract memories"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "tool_call_name": "read",
                    "args": {"uri": "viking://user/memories/profile.md"},
                    "result": result,
                }
            ),
        },
    ]

    bounded = request_budget.prepare_bounded_model_request(
        messages=messages,
        tools=[],
        max_tokens=300,
    )

    envelope = json.loads(bounded.messages[1]["content"])
    assert envelope["result"].startswith("HEAD-")
    assert envelope["result"].endswith("-TAIL")
    assert "middle omitted from extraction prompt" in envelope["result"]
    assert json.loads(messages[1]["content"])["result"] == result
    assert estimate_serialized_tokens({"messages": bounded.messages, "tools": bounded.tools}) <= 300


def test_request_budget_preserves_structured_tool_result_metadata() -> None:
    request_budget = importlib.import_module("openviking.session.memory.request_budget")
    result = {
        "uri": "viking://user/resources/report.md",
        "page_id": "page-17",
        "content": "HEAD-" + ("large-result-" * 2_000) + "-TAIL",
    }
    messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "tool_call_name": "read",
                    "args": {"uri": result["uri"]},
                    "result": result,
                }
            ),
        }
    ]

    bounded = request_budget.prepare_bounded_model_request(
        messages=messages,
        tools=[],
        max_tokens=300,
    )

    bounded_result = json.loads(bounded.messages[0]["content"])["result"]
    assert isinstance(bounded_result, dict)
    assert bounded_result["uri"] == result["uri"]
    assert bounded_result["page_id"] == result["page_id"]
    assert bounded_result["content"].startswith("HEAD-")
    assert bounded_result["content"].endswith("-TAIL")
    assert json.loads(messages[0]["content"])["result"] == result


def test_request_budget_fails_closed_for_oversized_non_tool_prompt() -> None:
    request_budget = importlib.import_module("openviking.session.memory.request_budget")
    messages = [{"role": "user", "content": "authored " * 2_000}]

    with pytest.raises(ResourceExhaustedError, match="exceeds configured Phase 2"):
        request_budget.prepare_bounded_model_request(
            messages=messages,
            tools=[],
            max_tokens=100,
        )


def test_strictest_request_budget_ignores_unscoped_items() -> None:
    request_budget = importlib.import_module("openviking.session.memory.request_budget")

    assert request_budget.strictest_request_budget(None, 400, 200) == 200
    assert request_budget.strictest_request_budget(None, None) is None


@pytest.mark.asyncio
async def test_extract_loop_applies_budget_immediately_before_provider_call() -> None:
    class RecordingVLM:
        model = "test-model"

        def __init__(self) -> None:
            self.calls = []

        async def get_completion_async(self, **kwargs):
            self.calls.append(kwargs)
            return None

    vlm = RecordingVLM()
    loop = ExtractLoop(
        vlm=vlm,
        viking_fs=object(),
        request_max_tokens=300,
    )
    loop._tool_schemas = []
    result = "HEAD-" + ("large-result-" * 2_000) + "-TAIL"
    messages = [
        {
            "role": "user",
            "content": json.dumps({"tool_call_name": "read", "args": {}, "result": result}),
        }
    ]

    await loop._call_llm(messages)

    sent = vlm.calls[0]
    sent_result = json.loads(sent["messages"][0]["content"])["result"]
    assert sent_result.startswith("HEAD-")
    assert sent_result.endswith("-TAIL")
    assert (
        estimate_serialized_tokens({"messages": sent["messages"], "tools": sent["tools"] or []})
        <= 300
    )
    assert json.loads(messages[0]["content"])["result"] == result


@pytest.mark.asyncio
async def test_image_description_fails_before_oversized_provider_call() -> None:
    class RecordingVLM:
        def __init__(self) -> None:
            self.called = False

        async def get_vision_completion_async(self, **kwargs):
            self.called = True
            return "unused"

    vlm = RecordingVLM()
    message = Message(
        id="image-message",
        role="user",
        parts=[ImagePart(url="data:image/png;base64," + ("A" * 10_000))],
    )

    with pytest.raises(ResourceExhaustedError, match="image description"):
        await describe_image_message(
            message,
            vlm=vlm,
            request_max_tokens=100,
        )

    assert vlm.called is False
