# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resource, privilege, virtualization and probe failures before provisioning."""

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
        ({"cpus": 2}, "CPU"),
        ({"ram_bytes": 4 * GIB}, "RAM"),
        ({"free_disk_bytes": 9 * GIB}, "disk"),
        ({"administrator": False}, "administrator"),
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
    ],
)
def test_explicit_docker_refuses_shortfall(
    changes: dict[str, object], tmp_path: Path
) -> None:
    result = HandlerSetupPreflight(collect=lambda _: facts(**changes)).handle(
        ModelSetupPlan(workspace=tmp_path, components=("delegation", "docker"))
    )
    assert result.status != "success"


def test_existing_stack_owns_its_ports_and_smaller_disk_floor(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(
            docker_installed=True,
            stack_running=True,
            free_disk_bytes=20 * GIB,
            occupied_ports=[5436],
        )
    ).handle(ModelSetupPlan(workspace=tmp_path, components=("delegation", "docker")))
    assert result.status == "success"


def test_missing_tools_are_named_before_install(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(missing_tools=["gh", "uv"])
    ).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "unavailable"
    assert result.evidence["missing_tools"] == "gh,uv"


def test_probe_error_is_not_a_success_or_zero_measurement(tmp_path: Path) -> None:
    def fail(_: Path) -> ModelSetupMachineFacts:
        raise OSError("credential-SENTINEL")

    result = HandlerSetupPreflight(collect=fail).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "failure"
    assert "SENTINEL" not in result.model_dump_json()
    assert "free_disk_bytes" not in result.evidence
