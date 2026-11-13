# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe tenant and control route the R8 bus lane sends with: golden and error chains and the seam to the delegate command.

The lane under test is the real ``BusLane`` built through the production wiring ``handler_scenario_r8._build_bus_lane``.
Its delegate command is a fake that records the argv and kwargs it is called with, so the probe tenant environment and the
control route pin are visible here: a change that drops or renames either turns these tests red.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from uuid import UUID

import pytest
from click.testing import CliRunner
from omnibase_core.cli.cli_commands import cli
from pydantic import ValidationError

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    handler_scenario_r8,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    ModelBusSettings,
    ModelSendRequest,
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


def _recorded_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Sequence[str], dict[str, object]]]:
    recorded: list[tuple[Sequence[str], dict[str, object]]] = []

    def fake_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        recorded.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(
            list(argv),
            0,
            stdout=json.dumps({SETTINGS.delegate_id_key: "run-1"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return recorded


def test_golden_chain_probe_tenant_every_send_carries_the_overlay_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every send carries the overlay's probe tenant in the delegate command's environment."""
    assert SETTINGS.bus is not None
    lane = handler_scenario_r8._build_bus_lane(
        SETTINGS, {SETTINGS.bus.workspace_env: "/workspace"}, KAFKA_OVERLAY
    )
    recorded = _recorded_calls(monkeypatch)

    lane.send(
        ModelSendRequest(
            task_type="summarization",
            prompt="p",
            traffic_class="synthetic",
            source_surface="readiness_probe",
        )
    )
    lane.send(ModelSendRequest(task_type="summarization", prompt="p"))

    assert len(recorded) == 2
    n = len(SETTINGS.delegate_command)
    for argv, _kwargs in recorded:
        assert argv[0] == SETTINGS.bus.env_command
        assert argv[1] == f"{SETTINGS.bus.tenant_env}={TENANT}"
        assert argv[2 : 2 + n] == list(SETTINGS.delegate_command)


def test_control_route_pins_only_the_organic_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The organic control is pinned to the overlay's control backend; the synthetic run is not."""
    assert SETTINGS.bus is not None
    overlay = ModelScenarioTenantReadOverlay(
        bus="kafka",
        bus_lane="dev",
        probe_tenant_id=TENANT,
        timeout_seconds=5.0,
        control_backend_id="backend-x",
    )
    lane = handler_scenario_r8._build_bus_lane(
        SETTINGS, {SETTINGS.bus.workspace_env: "/workspace"}, overlay
    )
    recorded = _recorded_calls(monkeypatch)

    lane.send(
        ModelSendRequest(
            task_type="summarization",
            prompt="p",
            traffic_class="synthetic",
            source_surface="readiness_probe",
        )
    )
    lane.send(ModelSendRequest(task_type="summarization", prompt="p"))

    synthetic_argv, organic_argv = recorded[0][0], recorded[1][0]
    assert "--backend-id" not in synthetic_argv
    assert "--backend-id" in organic_argv
    idx = organic_argv.index("--backend-id")
    assert organic_argv[idx + 1] == "backend-x"


def test_control_route_absent_key_sends_no_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a control backend id, neither request carries the pin."""
    assert SETTINGS.bus is not None
    lane = handler_scenario_r8._build_bus_lane(
        SETTINGS, {SETTINGS.bus.workspace_env: "/workspace"}, KAFKA_OVERLAY
    )
    recorded = _recorded_calls(monkeypatch)

    lane.send(
        ModelSendRequest(
            task_type="summarization",
            prompt="p",
            traffic_class="synthetic",
            source_surface="readiness_probe",
        )
    )
    lane.send(ModelSendRequest(task_type="summarization", prompt="p"))

    for argv, _ in recorded:
        assert "--backend-id" not in argv


def test_error_chain_control_backend_id_must_not_be_blank() -> None:
    """A blank control backend id is refused."""
    with pytest.raises(ValidationError):
        ModelScenarioTenantReadOverlay(
            bus="kafka",
            bus_lane="dev",
            probe_tenant_id=TENANT,
            timeout_seconds=5.0,
            control_backend_id="",
        )


def test_error_chain_settings_without_tenant_env_are_refused() -> None:
    """A bus settings block without a tenant environment variable is refused."""
    assert SETTINGS.bus is not None
    bus_dict = SETTINGS.bus.model_dump()
    del bus_dict["tenant_env"]
    with pytest.raises(ValidationError):
        ModelBusSettings.model_validate(bus_dict)


def test_error_chain_settings_without_env_command_are_refused() -> None:
    """A bus settings block without the environment command is refused."""
    assert SETTINGS.bus is not None
    bus_dict = SETTINGS.bus.model_dump()
    del bus_dict["env_command"]
    with pytest.raises(ValidationError):
        ModelBusSettings.model_validate(bus_dict)


def test_seam_the_pin_flag_is_an_option_of_the_delegate_command() -> None:
    """The pin flag the lane sends is an option of the delegate command."""
    result = CliRunner().invoke(cli, ["delegate", "--help"])
    assert result.exit_code == 0
    assert re.search(r"^\s*--backend-id(?=[\s\[])", result.output, re.M)


def test_seam_the_tenant_variable_is_the_one_the_delegate_command_reads() -> None:
    """The tenant variable the lane stamps is the one the delegate command reads."""
    from omnibase_infra.cli.cli_delegate import TENANT_OVERLAY_ENV

    assert SETTINGS.bus is not None
    assert SETTINGS.bus.tenant_env == TENANT_OVERLAY_ENV
    assert shutil.which(SETTINGS.bus.env_command) is not None
