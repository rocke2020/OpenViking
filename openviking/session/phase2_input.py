# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Build bounded, loss-preserving model inputs for session commit Phase 2."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Iterable, List

from openviking.message import Message
from openviking.message.part import ContextPart, Part, TextPart, ToolPart
from openviking.session.retention import build_turns
from openviking.utils.token_estimation import (
    estimate_text_tokens,
    truncate_text_to_token_budget,
)

_PLAN_VERSION = "v1"
_TOOL_OUTPUT_PREVIEW_MAX_TOKENS = 2048


@dataclass(frozen=True)
class Phase2Window:
    """One turn-aware, bounded Phase 2 extraction input."""

    messages: List[Message]
    fragment_ids: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    estimated_tokens: int


@dataclass(frozen=True)
class Phase2InputPlan:
    """Deterministic windows derived from immutable archived messages."""

    windows: List[Phase2Window]
    version: str = _PLAN_VERSION


def _clone_message(message: Message, *, message_id: str, parts: List[Part]) -> Message:
    cloned = copy.deepcopy(message)
    cloned.id = message_id
    cloned.parts = parts
    cloned.source_message_ids = list(message.source_message_ids or [message.id])
    return cloned


def _fragment_id(
    source_message_id: str,
    part_index: int,
    start: int,
    end: int,
    content: str,
) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{source_message_id}#phase2:{_PLAN_VERSION}:{part_index}:{start}:{end}:{digest}"


def _largest_prefix_within_budget(text: str, token_budget: int) -> int:
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return low


def _split_text(text: str, token_budget: int) -> List[tuple[int, int, str]]:
    if not text:
        return [(0, 0, "")]
    chunks: List[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        length = _largest_prefix_within_budget(text[start:], token_budget)
        if length <= 0:
            # Every supported code point fits in at least two estimated tokens,
            # while validated Phase 2 budgets are much larger than that.
            length = 1
        end = start + length
        chunks.append((start, end, text[start:end]))
        start = end
    return chunks


def _tool_output_marker(part: ToolPart) -> str:
    reference = part.tool_output_source_ref or part.tool_output_ref or part.tool_uri
    suffix = f"; full output: {reference}" if reference else "; full output remains archived"
    return f"\n… [middle omitted from Phase 2 prompt{suffix}] …\n"


def _compact_tool_part(part: ToolPart, message_budget: int) -> ToolPart:
    compacted = copy.deepcopy(part)
    output = compacted.tool_output or ""
    output_budget = min(_TOOL_OUTPUT_PREVIEW_MAX_TOKENS, max(16, message_budget // 2))
    if estimate_text_tokens(output) > output_budget:
        compacted.tool_output = truncate_text_to_token_budget(
            output,
            output_budget,
            marker=_tool_output_marker(compacted),
            head_ratio=0.5,
        )
        compacted.tool_output_truncated = True
        compacted.tool_output_original_chars = compacted.tool_output_original_chars or len(output)
        compacted.tool_output_preview_chars = len(compacted.tool_output)
        compacted.tool_output_sha256 = (
            compacted.tool_output_sha256 or hashlib.sha256(output.encode("utf-8")).hexdigest()
        )

    return compacted


def _fragment_message(message: Message, token_budget: int) -> List[Message]:
    unchanged = copy.deepcopy(message)
    unchanged.parts = [
        _compact_tool_part(part, token_budget) if isinstance(part, ToolPart) else part
        for part in unchanged.parts
    ]
    if unchanged.estimated_tokens <= token_budget and unchanged.parts == message.parts:
        unchanged.source_message_ids = list(message.source_message_ids or [message.id])
        return [unchanged]

    fragments: List[Message] = []
    pending_parts: List[Part] = []
    pending_start_index = 0

    def flush_pending(end_index: int) -> None:
        nonlocal pending_parts, pending_start_index
        if not pending_parts:
            return
        serialized = json.dumps(
            {
                "role": message.role,
                "parts": [asdict(part) for part in pending_parts],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fragment_message_id = _fragment_id(
            message.id,
            pending_start_index,
            0,
            end_index,
            serialized,
        )
        fragments.append(
            _clone_message(
                message,
                message_id=fragment_message_id,
                parts=copy.deepcopy(pending_parts),
            )
        )
        pending_parts = []

    for part_index, original_part in enumerate(message.parts):
        part = (
            _compact_tool_part(original_part, token_budget)
            if isinstance(original_part, ToolPart)
            else copy.deepcopy(original_part)
        )
        probe = _clone_message(message, message_id=message.id, parts=[part])

        if isinstance(part, (TextPart, ContextPart)) and probe.estimated_tokens > token_budget:
            flush_pending(part_index)
            attribute = "text" if isinstance(part, TextPart) else "abstract"
            original_text = getattr(part, attribute)
            for start, end, chunk in _split_text(original_text, token_budget):
                chunk_part = copy.deepcopy(part)
                setattr(chunk_part, attribute, chunk)
                message_id = _fragment_id(message.id, part_index, start, end, chunk)
                fragments.append(_clone_message(message, message_id=message_id, parts=[chunk_part]))
            pending_start_index = part_index + 1
            continue

        candidate_parts = [*pending_parts, part]
        candidate = _clone_message(message, message_id=message.id, parts=candidate_parts)
        if pending_parts and candidate.estimated_tokens > token_budget:
            flush_pending(part_index)
            pending_start_index = part_index
            candidate_parts = [part]
            candidate = _clone_message(message, message_id=message.id, parts=candidate_parts)

        if candidate.estimated_tokens > token_budget:
            # Tool metadata can still exceed a very small configured window.
            # Preserve pairing fields and bound only prompt-copy payload fields.
            if isinstance(part, ToolPart):
                part.tool_output = truncate_text_to_token_budget(
                    part.tool_output,
                    max(0, token_budget // 4),
                    marker=_tool_output_marker(part),
                    head_ratio=0.5,
                )
                candidate_parts = [part]
                candidate = _clone_message(
                    message,
                    message_id=message.id,
                    parts=candidate_parts,
                )
            if candidate.estimated_tokens > token_budget:
                raise ValueError(
                    f"Phase 2 message metadata exceeds window_max_tokens={token_budget}: "
                    f"{message.id}"
                )

        pending_parts = candidate_parts

    flush_pending(len(message.parts))
    return fragments or [_clone_message(message, message_id=message.id, parts=[])]


def _make_window(messages: Iterable[Message]) -> Phase2Window:
    window_messages = list(messages)
    source_ids: List[str] = []
    for message in window_messages:
        for source_id in message.source_message_ids or [message.id]:
            if source_id not in source_ids:
                source_ids.append(source_id)
    return Phase2Window(
        messages=window_messages,
        fragment_ids=tuple(message.id for message in window_messages),
        source_message_ids=tuple(source_ids),
        estimated_tokens=sum(message.estimated_tokens for message in window_messages),
    )


def build_phase2_input_plan(
    messages: Iterable[Message],
    *,
    window_max_tokens: int,
) -> Phase2InputPlan:
    """Return deterministic turn-aware windows without mutating archived input."""

    if window_max_tokens <= 0:
        raise ValueError("window_max_tokens must be greater than zero")

    windows: List[Phase2Window] = []
    current: List[Message] = []
    current_tokens = 0

    for turn in build_turns(messages):
        fragments = [
            fragment
            for message in turn.messages
            for fragment in _fragment_message(message, window_max_tokens)
        ]
        turn_tokens = sum(fragment.estimated_tokens for fragment in fragments)

        if turn_tokens <= window_max_tokens:
            if current and current_tokens + turn_tokens > window_max_tokens:
                windows.append(_make_window(current))
                current = []
                current_tokens = 0
            current.extend(fragments)
            current_tokens += turn_tokens
            continue

        if current:
            windows.append(_make_window(current))
            current = []
            current_tokens = 0
        for fragment in fragments:
            fragment_tokens = fragment.estimated_tokens
            if current and current_tokens + fragment_tokens > window_max_tokens:
                windows.append(_make_window(current))
                current = []
                current_tokens = 0
            current.append(fragment)
            current_tokens += fragment_tokens

    if current:
        windows.append(_make_window(current))

    return Phase2InputPlan(windows=windows)
