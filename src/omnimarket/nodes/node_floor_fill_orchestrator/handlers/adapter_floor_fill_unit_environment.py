# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live tick unit's environment: the ledger append command, the workspace config root and the overlay roots.

The live tick reads the work ledger through the bus append command and finds its overlay through the overlay roots, so its
unit carries ``ONEX_LEDGER_BUS_APPEND_COMMAND``, ``ONEX_WORKSPACE_CONFIG_ROOT`` and ``ONEX_SKILL_OVERLAY_ROOTS``. The
names and the append entry are the node contract's ``schedule`` block. The append command is built from the fill host's
own row of the lab host table (``ledger_bus_python``, ``ledger_bus_lane``); the other two are what the installing host
declares in its environment (the overlay roots already resolved, as the install read its overlay from them). A value
that is not declared refuses naming it; no value is a literal here.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_floor_fill_orchestrator.models import FloorFillConfigError
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_unit_environment import (
    ModelFloorFillUnitEnvironmentRequest,
    ModelFloorFillUnitEnvironmentResult,
)


def _refuse(key: str, why: str) -> FloorFillConfigError:
    return FloorFillConfigError(f"{key}: {why}", (key,))


class AdapterFloorFillUnitEnvironment:
    """Build the live unit's three variables, or refuse naming the one that is not declared."""

    def handle(
        self, request: ModelFloorFillUnitEnvironmentRequest
    ) -> ModelFloorFillUnitEnvironmentResult:
        names = request.schedule.unit_environment
        declared = {
            name: request.installing_environment.get(name, "").strip()
            for name in (names.workspace_config_root, names.overlay_roots)
        }
        for name, value in declared.items():
            if not value:
                raise _refuse(
                    name, "the installing host's environment does not declare it"
                )
        return ModelFloorFillUnitEnvironmentResult(
            variables={
                names.ledger_append_command: self._append_command(request),
                **declared,
            }
        )

    @staticmethod
    def _append_command(request: ModelFloorFillUnitEnvironmentRequest) -> str:
        row = request.host_row
        prefix = f"lab_hosts.hosts[{request.host}]"
        if row is None:
            raise _refuse(prefix, "the lab host table holds no row for the fill host")
        if not row.ledger_bus_python:
            raise _refuse(
                f"{prefix}.ledger_bus_python", "the row declares no ledger interpreter"
            )
        if not row.ledger_bus_lane:
            raise _refuse(
                f"{prefix}.ledger_bus_lane", "the row declares no ledger bus lane"
            )
        root = Path(request.omni_root)
        declared = Path(row.ledger_bus_python)
        interpreter = declared if declared.is_absolute() else root / declared
        return (
            f"{interpreter} -m {request.schedule.ledger_append_entry} "
            f"--bus-lane {row.ledger_bus_lane} --omnibase-path {root}"
        )


__all__: list[str] = ["AdapterFloorFillUnitEnvironment"]
