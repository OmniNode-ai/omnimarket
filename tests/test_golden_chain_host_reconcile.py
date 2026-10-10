# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain from recorded host facts through compute, effect and event."""

from pathlib import Path
from typing import Literal

import pytest

from omnimarket.nodes.node_host_reconcile_compute.handlers.handler_host_reconcile_compute import (
    HandlerHostReconcileCompute,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)
from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    command,
    handler,
    setup_workspace,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["check", "repair"])
def test_golden_chain_host_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["check", "repair"]
) -> None:
    root = setup_workspace(tmp_path, monkeypatch, behind=True)
    evaluator, publisher = RecordingEvaluator(), RecordingPublisher()
    cmd = command(root, mode)
    result = handler(evaluator, publisher).handle(cmd)
    final_request = evaluator.requests[-1]
    decisions = HandlerHostReconcileCompute().handle(final_request)
    assert result.surfaces == decisions.surfaces
    assert result.exit_code == decisions.exit_code
    assert result.floor_stamped == decisions.stamp_floor
    assert (
        result.diagnostics[-len(decisions.verdict_lines) :] == decisions.verdict_lines
    )
    assert publisher.events == [(publish_topics()[1], result)]
    assert result.correlation_id == cmd.correlation_id
    assert (
        final_request.rerun_command
        == f"python -m omnimarket.nodes.node_host_reconcile_effect --omni-home {root}"
    )
