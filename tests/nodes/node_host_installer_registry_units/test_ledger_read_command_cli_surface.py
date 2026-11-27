# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: a unit whose ledger read command runs ``onex ledger`` reveals the internal command groups (OMN-26578).

Measured on h202: the lane-dispatch unit exited 1 on every tick with ``Error: No such command 'ledger'`` although the
``onex.cli`` entry point ``ledger`` is installed in the venv the unit's read command names. The omnibase_core in that
venv declares ``ledger`` an INTERNAL command group and lists and resolves INTERNAL groups only when
``ONEX_CLI_SHOW_ALL_COMMANDS`` is on. The unit declared no such variable, so the group was hidden.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)

SWITCH = "ONEX_CLI_SHOW_ALL_COMMANDS"
READ = "ONEX_LEDGER_BUS_READ_COMMAND"

pytestmark = pytest.mark.unit


def _units_running_onex_ledger() -> list[str]:
    return sorted(
        name
        for name, unit in load_units().items()
        if " ledger " in str((unit.get("environment") or {}).get(READ, ""))
        and "/bin/onex " in str((unit.get("environment") or {}).get(READ, ""))
    )


def test_the_census_finds_units_that_run_onex_ledger() -> None:
    assert "lane-dispatch-systemd" in _units_running_onex_ledger()


@pytest.mark.parametrize("name", _units_running_onex_ledger())
def test_a_unit_that_runs_onex_ledger_turns_the_command_surface_on(name: str) -> None:
    environment = load_units()[name]["environment"]
    assert str(environment.get(SWITCH)) == "1", (
        f"{name} runs `onex ledger` but declares no {SWITCH}=1"
    )
