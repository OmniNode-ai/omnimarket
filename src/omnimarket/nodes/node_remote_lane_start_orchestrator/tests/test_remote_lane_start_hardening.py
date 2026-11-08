# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22848: a close that did not land is not final, a repo needs its sha, the timeout bounds the loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    Rig,
    request,
    stream_result,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    ModelAppendCommandResult,
)

pytestmark = [pytest.mark.unit]

DONE = ModelScriptedTransportScript(
    exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
)


def test_a_refused_terminal_is_not_a_durable_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, DONE)
    appended = rig.executor.append

    def refuse_terminal(rows: str) -> ModelAppendCommandResult:
        if rows.split(" | ")[1] == "TERMINAL":
            return ModelAppendCommandResult(exit_code=65, stderr="row refused")
        return appended(rows)

    monkeypatch.setattr(rig.executor, "append", refuse_terminal)
    result = rig.orchestrator.run(request())
    assert rig.ledger_rows("TERMINAL") == []
    assert result.exit_code != 0
    assert not result.receipt.final
    assert "not persisted" in (result.receipt.error or "")
    assert result.receipt.status == "ledger-refused"


def test_a_repo_request_without_a_sha_is_refused_before_the_claim(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, DONE)
    result = rig.orchestrator.run(request(repo="omnimarket"))
    assert result.outcome is EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE
    assert result.exit_code == 64
    assert rig.ledger_rows() == []
    assert rig.host_calls() == []
    assert any("without a sha" in line for line in result.lines)
