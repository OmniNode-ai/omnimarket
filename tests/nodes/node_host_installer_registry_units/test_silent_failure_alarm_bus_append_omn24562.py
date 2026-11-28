# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24562: the silent failure alarm unit declares the bus append command its ledger rows are written with.

Without it the alarm appends through ``onex work-ledger``, which binds no bus lane and no workspace root, so every
episode row exits 69 on a pool host and the tick exits 3.
"""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)


def test_unit_environment_names_the_bound_bus_append_command() -> None:
    unit: Any = load_units()["silent-failure-alarm"]
    command = unit["environment"]["ONEX_LEDGER_BUS_APPEND_COMMAND"]
    assert "omnimarket.work_ledger_bus.cli append" in command
    assert "--bus-lane dev" in command
    assert "--omnibase-path {registry_root}" in command
