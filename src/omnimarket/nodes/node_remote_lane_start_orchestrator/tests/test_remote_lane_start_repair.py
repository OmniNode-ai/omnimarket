# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repair lane pinned to a host refusing admission starts on another host that reached it over ssh (OMN-23389)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)

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

DONE = ModelScriptedTransportScript(
    exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
)


def _pool() -> ModelLaneHostPoolSnapshot:
    def reading(name: str, slots: int, refusal: str | None) -> ModelLaneHostPoolReading:
        return ModelLaneHostPoolReading(
            name=name,
            target=name,
            engines=("claude_opus", "claude_sonnet"),
            lane_slots=slots,
            lane_cap=4,
            rank_free=4.0,
            mem_avail_gb=16.0,
            lane_admission_refusal=refusal,
        )

    return ModelLaneHostPoolSnapshot(
        readings=(
            reading("full-host", 0, "DISK-FREE-UNDER-BAR"),
            reading("host-b", 2, None),
        )
    )


def test_a_repair_lane_pinned_to_the_refusing_host_starts_on_another(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, DONE, pool=_pool())
    result = rig.orchestrator.run(
        request(
            pinned_host="full-host",
            repair_target="full-host",
            host_ssh_reach={"host-b": ("full-host",)},
        )
    )
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert result.receipt.host == "host-b"


def test_error_chain_the_pin_without_a_repair_target_places_nothing(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, DONE, pool=_pool())
    result = rig.orchestrator.run(request(pinned_host="full-host"))
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert not any(c.startswith("host:") for c in rig.calls)


def test_error_chain_a_repair_lane_with_no_ssh_reach_places_nothing(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, DONE, pool=_pool())
    result = rig.orchestrator.run(
        request(pinned_host="full-host", repair_target="full-host")
    )
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert any("no-ssh-to-full-host" in line for line in rig.printed)
