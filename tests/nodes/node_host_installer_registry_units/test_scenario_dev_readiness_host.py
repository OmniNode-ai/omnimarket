# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The R8 and R9 scenario units carry the dev readiness host the scheduled tick waits on (OMN-25878).

Both scenario ticks read the lab host-table row name of the dev host from ``ONEX_DEV_READINESS_HOST`` before they run
(the ``dev_readiness`` block of each node contract) and exit 65 when it is unset. Until the installer carried the name, a
unit installed through it ran every tick into that refusal: h053's journal holds 13 such exits and no Finished line from the
merge of the readiness wait (2026-11-22T23:26:38Z) to 2026-11-23T07:28:32Z.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)
from tests.nodes.node_host_installer_registry_units.test_scenario_r3_r5_r8_units import (
    UNITS,
    _checkout,
)

READINESS_ENV = "ONEX_DEV_READINESS_HOST"
SCENARIO_UNITS = ("scenario-r8-systemd", "scenario-r9-systemd")
NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
NODE_BY_UNIT = {
    "scenario-r8-systemd": "node_delegation_scenario_r8_effect",
    "scenario-r9-systemd": "node_delegation_scenario_r9_effect",
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
    monkeypatch.delenv(READINESS_ENV, raising=False)


@pytest.mark.parametrize("unit", SCENARIO_UNITS)
def test_the_unit_names_the_variable_its_node_contract_waits_on(unit: str) -> None:
    contract = yaml.safe_load(
        (NODES / NODE_BY_UNIT[unit] / "contract.yaml").read_text(encoding="utf-8")
    )
    declared = contract["runtime"]["dev_readiness"]["host_env"]
    spec = load_units()[unit]
    assert declared == READINESS_ENV
    assert declared in (spec.get("environment_inputs") or {})
    assert declared in (spec.get("required_environment_inputs") or [])


@pytest.mark.parametrize("unit", SCENARIO_UNITS)
def test_install_renders_the_dev_readiness_host_into_the_unit(
    tmp_path: Path, unit: str
) -> None:
    spec = UNITS[unit]
    path, revision = _checkout(tmp_path, spec["entry"])
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            ledger_host_name="h053",
            dev_readiness_host="h201",
            workspace_config_root=str(tmp_path / "config"),
        )
    )
    assert result.status == "success", result.stderr
    service = (
        tmp_path / "home/.config/systemd/user" / f"{spec['label']}.service"
    ).read_text(encoding="utf-8")
    assert f'"{READINESS_ENV}=h201"' in service


@pytest.mark.parametrize("unit", SCENARIO_UNITS)
def test_install_without_the_dev_readiness_host_is_refused_and_names_it(
    tmp_path: Path, unit: str
) -> None:
    """Failing control: a unit that would exit 65 on every tick is not installed."""
    spec = UNITS[unit]
    path, revision = _checkout(tmp_path, spec["entry"])
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            ledger_host_name="h053",
            workspace_config_root=str(tmp_path / "config"),
        )
    )
    assert result.status == "failure"
    assert READINESS_ENV in result.stderr
    assert not (
        tmp_path / "home/.config/systemd/user" / f"{spec['label']}.timer"
    ).exists()
