# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Bound Phase 2 model requests without truncating authored prompt content."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openviking.utils.token_estimation import (
    estimate_serialized_tokens,
    estimate_text_tokens,
    truncate_text_to_token_budget,
)
from openviking_cli.exceptions import ResourceExhaustedError

_TOOL_RESULT_MAX_TOKENS = 4096
_TOOL_RESULT_MARKER = "\n… [middle omitted from extraction prompt] …\n"
_STRUCTURED_TEXT_MIN_TOKENS = 64


@dataclass(frozen=True)
class BoundedModelRequest:
    """A prompt copy proven to fit its configured estimated input budget."""

    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]]
    estimated_tokens: int


@dataclass
class _ToolResultEnvelope:
    message_index: int
    payload: Dict[str, Any]
    original_result: Any


def strictest_request_budget(*budgets: Optional[int]) -> Optional[int]:
    """Return the smallest active budget without imposing one on unscoped work."""

    active = [budget for budget in budgets if budget is not None]
    return min(active) if active else None


def _request_tokens(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
) -> int:
    return estimate_serialized_tokens({"messages": messages, "tools": tools or []})


def _find_tool_result_envelopes(
    messages: List[Dict[str, Any]],
) -> List[_ToolResultEnvelope]:
    envelopes: List[_ToolResultEnvelope] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(payload, dict)
            or "tool_call_name" not in payload
            or "result" not in payload
        ):
            continue
        envelopes.append(
            _ToolResultEnvelope(
                message_index=index,
                payload=payload,
                original_result=copy.deepcopy(payload["result"]),
            )
        )
    return envelopes


def _structured_text_paths(
    value: Any,
    path: tuple[str | int, ...] = (),
) -> List[tuple[tuple[str | int, ...], str]]:
    paths: List[tuple[tuple[str | int, ...], str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            paths.extend(_structured_text_paths(item, (*path, key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_structured_text_paths(item, (*path, index)))
    elif isinstance(value, str) and estimate_text_tokens(value) > _STRUCTURED_TEXT_MIN_TOKENS:
        paths.append((path, value))
    return paths


def _set_path(value: Any, path: tuple[str | int, ...], replacement: str) -> None:
    target = value
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement


def _compact_tool_result(result: Any, result_budget: int) -> Any:
    if isinstance(result, str):
        return truncate_text_to_token_budget(
            result,
            max(0, result_budget),
            marker=_TOOL_RESULT_MARKER,
            head_ratio=0.5,
        )
    if not isinstance(result, (dict, list)):
        return copy.deepcopy(result)

    compacted = copy.deepcopy(result)
    if estimate_serialized_tokens(compacted) <= result_budget:
        return compacted
    candidates = sorted(
        _structured_text_paths(compacted),
        key=lambda item: estimate_text_tokens(item[1]),
        reverse=True,
    )
    for path, original_text in candidates:
        excess = estimate_serialized_tokens(compacted) - result_budget
        if excess <= 0:
            break
        current_tokens = estimate_text_tokens(original_text)
        text_budget = max(0, current_tokens - excess - 8)
        _set_path(
            compacted,
            path,
            truncate_text_to_token_budget(
                original_text,
                text_budget,
                marker=_TOOL_RESULT_MARKER,
                head_ratio=0.5,
            ),
        )
    return compacted


def _write_envelope(
    messages: List[Dict[str, Any]],
    envelope: _ToolResultEnvelope,
    result_budget: int,
) -> None:
    payload = copy.deepcopy(envelope.payload)
    payload["result"] = _compact_tool_result(
        envelope.original_result,
        max(0, result_budget),
    )
    messages[envelope.message_index]["content"] = json.dumps(payload, ensure_ascii=False)


def prepare_bounded_model_request(
    *,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    max_tokens: int,
) -> BoundedModelRequest:
    """Compact recognized tool results, then fail closed if the request is still too large."""

    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than zero")

    bounded_messages = copy.deepcopy(messages)
    bounded_tools = copy.deepcopy(tools)
    envelopes = _find_tool_result_envelopes(bounded_messages)

    per_result_budget = min(_TOOL_RESULT_MAX_TOKENS, max(16, max_tokens // 4))
    for envelope in envelopes:
        if estimate_serialized_tokens(envelope.original_result) > per_result_budget:
            _write_envelope(bounded_messages, envelope, per_result_budget)

    estimated_tokens = _request_tokens(bounded_messages, bounded_tools)
    if estimated_tokens > max_tokens:
        for envelope in sorted(
            envelopes,
            key=lambda item: estimate_serialized_tokens(item.original_result),
            reverse=True,
        ):
            current_content = bounded_messages[envelope.message_index].get("content", "")
            try:
                current_result = json.loads(current_content).get("result", "")
            except (AttributeError, TypeError, ValueError):
                current_result = ""
            current_result_tokens = estimate_serialized_tokens(current_result)
            if current_result_tokens <= 0:
                continue
            excess = estimated_tokens - max_tokens
            result_budget = max(0, current_result_tokens - excess - 8)
            _write_envelope(bounded_messages, envelope, result_budget)
            estimated_tokens = _request_tokens(bounded_messages, bounded_tools)
            if estimated_tokens <= max_tokens:
                break

    if estimated_tokens > max_tokens:
        raise ResourceExhaustedError(
            "Memory extraction request exceeds configured Phase 2 input limit: "
            f"estimated_tokens={estimated_tokens}, max_tokens={max_tokens}. "
            "Increase memory.extraction_request_max_tokens or reduce the upstream "
            "Phase 2 window size."
        )

    return BoundedModelRequest(
        messages=bounded_messages,
        tools=bounded_tools,
        estimated_tokens=estimated_tokens,
    )


def ensure_model_request_within_budget(
    request: Dict[str, Any],
    *,
    max_tokens: int,
    request_name: str,
) -> int:
    """Fail closed when a non-ExtractLoop Phase 2 request exceeds its input budget."""

    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than zero")
    estimated_tokens = estimate_serialized_tokens(request)
    if estimated_tokens > max_tokens:
        raise ResourceExhaustedError(
            f"{request_name} exceeds configured Phase 2 input limit: "
            f"estimated_tokens={estimated_tokens}, max_tokens={max_tokens}"
        )
    return estimated_tokens
