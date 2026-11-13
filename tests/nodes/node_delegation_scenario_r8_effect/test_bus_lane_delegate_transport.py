# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate transport flags the R8 bus lane sends with: golden and error chains and the seam to the delegate command.

The lane under test is the real ``BusLane`` built through the production wiring ``handler_scenario_r8._build_bus_lane``.
Its delegate command is a fake that records the argv it is called with, so the transport flags the lane appends are visible
here: a change that drops or renames a flag turns these tests red.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from uuid import UUID

import pytest
from click.testing import CliRunner
from omnibase_core.cli.cli_commands import cli

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    handler_scenario_r8,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    LaneReadError,
    ModelSendRequest,
    delegate_transport_args,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    load_lane_settings,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
SETTINGS = load_lane_settings()
KAFKA_OVERLAY = ModelScenarioTenantReadOverlay(
    bus="kafka", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
)


def test_golden_chain_kafka_overlay_names_the_transport_on_every_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kafka overlay names the bus, lane and workspace on every send."""
    assert SETTINGS.bus is not None
    lane = handler_scenario_r8._build_bus_lane(
        SETTINGS, {SETTINGS.bus.workspace_env: "/workspace"}, KAFKA_OVERLAY
    )
    recorded: list[Sequence[str]] = []

    def fake_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        recorded.append(list(argv))
        return subprocess.CompletedProcess(
            list(argv),
            0,
            stdout=json.dumps({SETTINGS.delegate_id_key: "run-1"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    lane.send(
        ModelSendRequest(
            task_type="summarization",
            prompt="p",
            traffic_class="synthetic",
            source_surface="readiness_probe",
        )
    )

    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[: len(SETTINGS.delegate_command)] == list(SETTINGS.delegate_command)
    assert argv[-6:] == [
        "--bus",
        "kafka",
        "--lane",
        "dev",
        "--omnibase-path",
        "/workspace",
    ]
    assert "--traffic-class" in argv
    assert argv[argv.index("--traffic-class") + 1] == "synthetic"
    assert "--source-surface" in argv
    assert argv[argv.index("--source-surface") + 1] == "readiness_probe"


def test_golden_chain_non_kafka_overlay_names_only_the_bus() -> None:
    """A non-kafka overlay names only the bus."""
    overlay = ModelScenarioTenantReadOverlay(
        bus="inmemory", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
    )
    assert delegate_transport_args(overlay, None) == ("--bus", "inmemory")


def test_error_chain_kafka_overlay_without_workspace_is_not_configured() -> None:
    """A kafka overlay without a workspace is not configured."""
    with pytest.raises(LaneReadError, match="workspace root"):
        handler_scenario_r8._build_bus_lane(SETTINGS, {}, KAFKA_OVERLAY)
    with pytest.raises(LaneReadError, match="workspace root"):
        delegate_transport_args(KAFKA_OVERLAY, "   ")


def test_seam_every_flag_the_lane_sends_is_an_option_of_the_delegate_command() -> None:
    """Every flag the lane sends is an option of the delegate command."""
    result = CliRunner().invoke(cli, ["delegate", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--bus",
        "--lane",
        "--omnibase-path",
        "--traffic-class",
        "--source-surface",
        "--json",
        "--task-type",
    ):
        assert re.search(rf"^\s*{re.escape(flag)}(?=[\s\[])", result.output, re.M), flag
