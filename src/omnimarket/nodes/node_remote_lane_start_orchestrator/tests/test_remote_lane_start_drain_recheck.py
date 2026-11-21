# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start re-reads the drain state after placement, before the engine start (OMN-24489).

A host can start draining after the placement stood, after the CLAIM row was written and while the
provisioning steps ran, and the engine must not start on it: the start re-reads the drain state
right after the BEFORE_ENGINE provisioning block, before step Start. The placed host draining at
the second read, or a second read that cannot be read, ends the start ``start-failed`` with a
closing row and no engine (the provisioning failure's shape); never draining goes on to the engine
start as the existing start tests do. The reader answers a different tuple per call (a counter),
so the second read differs from the first.
"""

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

pytestmark = [pytest.mark.unit]


class StepwiseDrainReader:
    """The ``draining_hosts`` reader: each call hands out the next answer, a tuple or the exception to raise."""

    def __init__(self, answers: list[tuple[str, ...] | BaseException]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def __call__(self) -> tuple[str, ...]:
        answer = self._answers[min(self.calls, len(self._answers) - 1)]
        self.calls += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer


def host_ops(rig: Rig) -> list[str]:
    """The host operations of the call log, in order."""
    return [
        c.split()[0].removeprefix("host:") for c in rig.calls if c.startswith("host:")
    ]


def rig_with(tmp_path: Path, reader: StepwiseDrainReader) -> Rig:
    """The start over the scripted transport with the engine ending done, on the reader's drain state."""
    return Rig(
        tmp_path,
        ModelScriptedTransportScript(
            exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
        ),
        draining_hosts=reader,
    )


def test_a_host_that_starts_draining_after_placement_ends_the_start_without_an_engine(
    tmp_path: Path,
) -> None:
    reader = StepwiseDrainReader([(), ("h106",)])
    rig = rig_with(tmp_path, reader)
    result = rig.orchestrator.run(request())
    assert reader.calls == 2
    assert result.outcome is EnumRemoteLaneStartOutcome.START_FAILED
    assert result.exit_code == 1
    assert "start_engine_detached" not in host_ops(rig)
    assert any(
        line.startswith("REMOTE-LANE-DRAIN-RECHECK")
        and "outcome=host-draining" in line
        and "no engine was started" in line
        for line in result.lines
    )
    [terminal] = rig.ledger_rows("TERMINAL")
    assert "REMOTE-LANE-DRAIN-RECHECK" in terminal
    assert result.receipt.final is True


def test_a_second_read_that_cannot_be_read_ends_the_start_without_an_engine(
    tmp_path: Path,
) -> None:
    reader = StepwiseDrainReader([(), OSError("the drain journal cannot be read")])
    rig = rig_with(tmp_path, reader)
    result = rig.orchestrator.run(request())
    assert reader.calls == 2
    assert result.outcome is EnumRemoteLaneStartOutcome.START_FAILED
    assert result.exit_code == 1
    assert "start_engine_detached" not in host_ops(rig)
    assert any(
        line.startswith("REMOTE-LANE-DRAIN-RECHECK")
        and "outcome=drain-state-unreadable" in line
        for line in result.lines
    )
    [terminal] = rig.ledger_rows("TERMINAL")
    assert "REMOTE-LANE-DRAIN-RECHECK" in terminal
    assert result.receipt.final is True


def test_never_draining_goes_on_to_the_engine_start(tmp_path: Path) -> None:
    reader = StepwiseDrainReader([(), ()])
    rig = rig_with(tmp_path, reader)
    result = rig.orchestrator.run(request())
    assert reader.calls == 2
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert result.exit_code == 0
    assert "start_engine_detached" in host_ops(rig)
    [terminal] = rig.ledger_rows("TERMINAL")
    assert "handed-off" in terminal
