# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegate send names the reason its answer carries (OMN-25878).

Recorded 2026-11-19 on h053: eight of the eleven R8 failure events of the day read ``no reason given`` and the three that
named one were cut mid-word at the 240-character detail limit (``fixtures/delegate_failed_terminal_20261119.json``). The delegate answer
carries a failed run's ``terminal_failure_reason`` inside the terminal payload; the contract named it at the top of the
answer, where it is never present, so a run that failed with an empty ``error_message`` read as no reason at all.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    handler_scenario_r8,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    LaneReadError,
    ModelSendRequest,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    load_lane_settings,
)

pytestmark = pytest.mark.unit

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "delegate_failed_terminal_20261119.json"
    ).read_text(encoding="utf-8")
)
SETTINGS = load_lane_settings()
OVERLAY = ModelScenarioTenantReadOverlay(
    bus="kafka",
    bus_lane="dev",
    probe_tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
    timeout_seconds=5.0,
)


def _send(monkeypatch: pytest.MonkeyPatch, answer: dict[str, object]) -> None:
    assert SETTINGS.bus is not None

    def fake_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            list(argv), 1, stdout=json.dumps(answer), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    lane = handler_scenario_r8._build_bus_lane(
        SETTINGS, {SETTINGS.bus.workspace_env: "/workspace"}, OVERLAY
    )
    lane.send(
        ModelSendRequest(
            task_type="summarization",
            prompt="p",
            traffic_class="synthetic",
            source_surface="readiness_probe",
        )
    )


def test_error_chain_a_failed_run_with_an_empty_error_message_names_its_terminal_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded answer shape: ``error_message`` empty, the reason in ``terminal_failure_reason`` of the payload."""
    with pytest.raises(LaneReadError) as raised:
        _send(monkeypatch, FIXTURE["answer_shape"])
    assert "quality_veto" in str(raised.value)
    assert "no reason given" not in str(raised.value)
