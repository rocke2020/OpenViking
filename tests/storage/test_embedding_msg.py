# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from uuid import uuid4

from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.queuefs.semantic_msg import SemanticMsg
from openviking.telemetry.request_wait_tracker import RequestWaitTracker


def test_embedding_msg_roundtrip_preserves_id_for_request_wait_tracker():
    telemetry_id = f"tm_{uuid4().hex}"
    tracker = RequestWaitTracker.get_instance()
    tracker.register_request(telemetry_id)

    try:
        msg = EmbeddingMsg(
            "hello",
            {"uri": "viking://user/default/skills/demo"},
            telemetry_id=telemetry_id,
            source_task_id="task-1",
        )
        tracker.register_embedding_root(telemetry_id, msg.id)

        restored = EmbeddingMsg.from_dict(msg.to_dict())

        assert restored.id == msg.id
        assert restored.source_task_id == "task-1"
        tracker.mark_embedding_done(telemetry_id, restored.id)
        assert tracker.is_complete(telemetry_id)
    finally:
        tracker.cleanup(telemetry_id)


def test_legacy_embedding_payload_recovers_task_from_semantic_message_id():
    semantic_msg = SemanticMsg(
        uri="viking://resources/demo",
        context_type="resource",
        source_task_id="task-1",
    )
    embedding_msg = EmbeddingMsg(
        "hello",
        {"uri": "viking://resources/demo/file.txt"},
        semantic_msg_id=semantic_msg.id,
        source_task_id="task-1",
    )
    legacy_payload = embedding_msg.to_dict()
    legacy_payload.pop("source_task_id")

    restored = EmbeddingMsg.from_dict(legacy_payload)

    assert restored.source_task_id == "task-1"


def test_plain_legacy_semantic_id_does_not_invent_source_task():
    restored = EmbeddingMsg.from_dict(
        {
            "message": "hello",
            "context_data": {"uri": "viking://resources/demo/file.txt"},
            "semantic_msg_id": str(uuid4()),
        }
    )

    assert restored.source_task_id == ""
    assert restored.legacy_task_identity_unknown is True


def test_current_non_task_semantic_payload_is_not_treated_as_legacy_unknown():
    msg = EmbeddingMsg(
        "hello",
        {"uri": "viking://resources/demo/file.txt"},
        semantic_msg_id=str(uuid4()),
    )

    restored = EmbeddingMsg.from_dict(msg.to_dict())

    assert restored.source_task_id == ""
    assert restored.legacy_task_identity_unknown is False
