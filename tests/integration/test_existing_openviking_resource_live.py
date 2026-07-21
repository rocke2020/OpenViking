# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Read-only live checks for the imported OpenViking repository."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

RESOURCE_URI = "viking://resources/volcengine/OpenViking"
DOCS_ZH_URI = f"{RESOURCE_URI}/docs/zh"
RUN_LIVE_CHECK = os.environ.get("OPENVIKING_RUN_LIVE_RESOURCE_TEST") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUN_LIVE_CHECK,
        reason="set OPENVIKING_RUN_LIVE_RESOURCE_TEST=1 to check the live resource",
    ),
]


def _run_ov(*args: str) -> dict:
    result = subprocess.run(
        ["ov", *args, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_find_openviking() -> None:
    result = _run_ov("find", "what is openviking")

    resources = result["result"]["resources"]
    assert any(
        item["uri"].startswith(RESOURCE_URI)
        and "OpenViking" in (item.get("abstract") or "")
        for item in resources
    )


def test_grep_openviking_in_chinese_docs() -> None:
    result = _run_ov("grep", "openviking", "--uri", DOCS_ZH_URI)

    matches = result["result"]["matches"]
    assert matches
    assert all(match["uri"].startswith(f"{DOCS_ZH_URI}/") for match in matches)
    assert all("openviking" in match["content"].lower() for match in matches)
