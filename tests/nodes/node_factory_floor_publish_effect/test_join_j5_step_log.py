# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23717: the publisher's log line for a failed J5 step carries the runner's reason (the seam between the two).

The publisher keeps ``exit <code>`` as the step's detail and the last stderr lines as its diagnostic. The J5 runner wrote
its result to stdout alone, so a failed J5 step had an empty diagnostic. The runner now writes ``JOIN-J5-FAILED <reason>`` to
stderr from ``failure_report``; this test feeds the publisher exactly that line, built by the runner's own code from a real
result, so a change to either side's shape fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.work_ledger_append import (
    EnumJoinJ5Outcome,
    ModelJoinJ5RunItem,
    ModelJoinJ5RunResult,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
)
from omnimarket.nodes.node_work_ledger_append_effect.runtime_join_j5 import (
    failure_report,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    Scripted,
    _refresh_request,
)

pytestmark = pytest.mark.unit

CID = "0f8b6c1e2d3a4b5c8d9e0a1b2c3d4e5f"
READ_ERROR = "the projection read failed on its last of 6 attempts: read-command-exit-1"


def _publish(tmp_path: Path, stderr: bytes) -> dict[str, tuple[str, str, str]]:
    runner = Scripted({"--json": (0, b"[]", b""), "run": (1, b"{}", stderr)})
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    return {s.name: (s.status, s.detail, s.diagnostic) for s in result.steps}


def test_golden_the_j5_step_diagnostic_carries_the_runner_reason(
    tmp_path: Path,
) -> None:
    result = ModelJoinJ5RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ5RunItem(
                correlation_id=CID,
                outcome=EnumJoinJ5Outcome.PROJECTION_READ_FAILED,
                failure_code="projection_read_failed",
                detail=READ_ERROR,
                ledger_seq=3405,
            ),
        ),
    )
    report = failure_report(result)
    assert report is not None

    steps = _publish(tmp_path, (report + "\n").encode())

    status, detail, diagnostic = steps["join_j5"]
    assert (status, detail) == ("failed", "exit 1")
    assert diagnostic == report
    assert "projection_read_failed" in diagnostic
    assert READ_ERROR in diagnostic


def test_control_a_runner_that_writes_no_stderr_leaves_the_diagnostic_empty(
    tmp_path: Path,
) -> None:
    status, detail, diagnostic = _publish(tmp_path, b"")["join_j5"]

    assert (status, detail) == ("failed", "exit 1")
    assert diagnostic == ""
