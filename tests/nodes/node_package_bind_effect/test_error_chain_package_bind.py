# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the package bind effect.

Inject a defined fault and assert the typed failure event.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.nodes.node_package_bind_effect.test_golden_chain_package_bind import (
    CORRELATION,
    TOPICS,
    publish,
    request_payload,
)

pytestmark = pytest.mark.unit


def test_error_chain_a_missing_node_directory_gives_one_failed_event(
    tmp_path: Path,
) -> None:
    events = asyncio.run(publish(request_payload(tmp_path, "node_absent_compute")))
    assert [topic for topic, _ in events] == [TOPICS["package_bind_run_failed"]]
    assert events[0][1]["correlation_id"] == CORRELATION
    assert events[0][1]["failure"].startswith("no-package-dir:")


def test_error_chain_a_malformed_node_name_gives_one_failed_event(
    tmp_path: Path,
) -> None:
    events = asyncio.run(publish(request_payload(tmp_path, "../escape")))
    assert [topic for topic, _ in events] == [TOPICS["package_bind_run_failed"]]
    assert events[0][1]["failure"] == "bad-node-name:../escape"


def test_error_chain_a_payload_that_is_not_a_request_gives_one_failed_event() -> None:
    events = asyncio.run(publish(b'{"node": 7}'))
    assert [topic for topic, _ in events] == [TOPICS["package_bind_run_failed"]]
    assert events[0][1]["failure"].startswith("bad-payload:")
