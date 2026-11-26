# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the C30 and C31 probe units carry the lane bus environment by reference (OMN-19915).

The sibling delegation-check-probe unit reaches the dev lane through the lane declaration table found under OMNIBASE_PATH and
ONEX_WORKSPACE_CONFIG_ROOT, and its lane credential comes from the host's own client store. The C30 and C31 units
declared neither, so their tick could not publish on a lab host. No broker address or credential is written into a unit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_delegation_check_probe_bus_env as sibling,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

BUS_ENV = ("OMNIBASE_PATH", "ONEX_WORKSPACE_CONFIG_ROOT")
UNITS = {
    "c30-claude-code-plugin-probe": (
        "onex-c30-claude-code-plugin-probe",
        "src/omnimarket/nodes/node_c30_claude_code_plugin_probe_effect/runtime_c30_probe_tick.py",
    ),
    "c31-local-dashboard-parity-probe": (
        "onex-c31-local-dashboard-parity-probe",
        "src/omnimarket/nodes/node_c31_local_dashboard_parity_probe_effect/runtime_c31_probe_tick.py",
    ),
}

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


@pytest.mark.parametrize("name", BUS_ENV)
@pytest.mark.parametrize("unit", sorted(UNITS))
def test_the_declared_unit_names_the_lane_bus_environment_by_reference(
    unit: str, name: str
) -> None:
    environment = load_units()[unit]["environment"]
    assert name in environment, f"{unit} declares no {name}"
    assert environment[name] in ("{registry_root}", "{workspace_config_root}")
    assert not any(key.startswith("KAFKA_") for key in environment)


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_the_declaration_matches_the_sibling_delegation_check_probe(unit: str) -> None:
    units = load_units()
    for name in BUS_ENV:
        assert (
            units[unit]["environment"][name] == units[sibling.UNIT]["environment"][name]
        )


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_the_rendered_service_carries_the_bus_environment_and_no_broker(
    unit: str, tmp_path: Path
) -> None:
    label, entry = UNITS[unit]
    path, revision = sibling._checkout(tmp_path, entry)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
            marketplace_root=str(tmp_path / "market"),
        )
    )
    assert result.status == "success", result.stderr
    service = (tmp_path / "home/.config/systemd/user" / f"{label}.service").read_text(
        encoding="utf-8"
    )
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in service
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config-root"' in service
    assert "KAFKA_" not in service
