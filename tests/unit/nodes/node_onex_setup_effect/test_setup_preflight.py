# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Platform support is shared; selected capability owners check real requirements."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit
GIB = 1024**3


def facts(**changes: object) -> ModelSetupMachineFacts:
    return ModelSetupMachineFacts.model_validate(
        {
            "platform": "Darwin",
            "architecture": "arm64",
            "macos_version": "26.1",
            "cpus": 10,
            "ram_bytes": 32 * GIB,
            "free_disk_bytes": 50 * GIB,
            "administrator": True,
            "virtual_machine": False,
            "rosetta": False,
            "docker_installed": False,
            "stack_running": False,
            "occupied_ports": [],
            "missing_tools": [],
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("changes", "remedy"),
    [
        ({"platform": "Linux"}, "macOS"),
        ({"macos_version": "13.6"}, "14"),
        ({"architecture": "aarch32"}, "architecture"),
        ({"rosetta": True}, "native"),
    ],
)
def test_native_refuses_failed_requirement(
    changes: dict[str, object], remedy: str, tmp_path: Path
) -> None:
    handler = HandlerSetupPreflight(collect=lambda _: facts(**changes))
    result = handler.handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status != "success"
    assert remedy in (result.fix or "")
    assert not list(tmp_path.iterdir())


def test_native_vm_needs_no_docker_or_free_ports(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(virtual_machine=True, occupied_ports=[5436])
    ).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "success"


@pytest.mark.parametrize(
    "changes",
    [
        {"virtual_machine": True},
        {"cpus": 4},
        {"ram_bytes": 8 * GIB},
        {"free_disk_bytes": 29 * GIB},
        {"occupied_ports": [5436]},
        {"administrator": False, "missing_tools": ["git", "brew", "gh"]},
    ],
)
@pytest.mark.parametrize("component", ["local-stack", "docker"])
def test_component_name_never_activates_retired_blanket_floors(
    changes: dict[str, object], tmp_path: Path, component: str
) -> None:
    result = HandlerSetupPreflight(collect=lambda _: facts(**changes)).handle(
        ModelSetupPlan(workspace=tmp_path, components=(component,))
    )
    assert result.status == "success", result.fix
    assert set(result.evidence) == {
        "platform",
        "architecture",
        "macos_version",
        "rosetta",
        "virtual_machine",
    }


def test_selected_stack_owns_its_ports_and_allocation_checks(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(
            docker_installed=True,
            stack_running=True,
            free_disk_bytes=20 * GIB,
            occupied_ports=[5436],
        )
    ).handle(ModelSetupPlan(workspace=tmp_path, components=("local-stack",)))
    assert result.status == "success"


def test_endpoint_client_does_not_require_developer_tools(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(missing_tools=["gh", "uv"])
    ).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "success"
    assert "missing_tools" not in result.evidence


def test_probe_error_is_not_a_success_or_zero_measurement(tmp_path: Path) -> None:
    def fail(_: Path) -> ModelSetupMachineFacts:
        raise OSError("credential-SENTINEL")

    result = HandlerSetupPreflight(collect=fail).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "failure"
    assert "SENTINEL" not in result.model_dump_json()
    assert "free_disk_bytes" not in result.evidence
