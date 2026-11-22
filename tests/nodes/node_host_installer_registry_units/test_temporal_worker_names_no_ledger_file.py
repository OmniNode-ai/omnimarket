# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The temporal worker unit names no ledger file: its steps append over the bus and read no ledger path.

Each worker queue's node (the workflow 1 node, the merged-change runner read workflow, the sprint closer and the
worktree prune) holds no read of the ledger path variable; the closer reads rows through the host's declared reader
argv and the unit's rows go out through the bus append command. At the merge base the unit required the path variable,
so an install that named no ledger path was refused, and the rendered unit carried it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)
from tests.nodes.node_host_installer_registry_units.test_service_type_unit import (
    linux_host,
    request,
)

# `linux_host` is an autouse fixture (a Linux host with uv) taken from the sibling module: naming
# it in `__all__` keeps the import used and makes pytest collect it here.
__all__ = ["linux_host"]

pytestmark = pytest.mark.unit

UNIT = "temporal-worker-systemd"
PATH_VARIABLE = "ONEX_LEDGER" + "_PATH"


def test_the_unit_declares_no_ledger_path_input() -> None:
    unit = load_units()[UNIT]
    assert PATH_VARIABLE not in unit["environment_inputs"]
    assert PATH_VARIABLE not in unit["required_environment_inputs"]
    assert PATH_VARIABLE not in unit["environment"]


def test_the_unit_still_appends_over_the_bus_as_its_host() -> None:
    unit = load_units()[UNIT]
    assert unit["environment"]["ONEX_LEDGER_WRITE_VIA"] == "bus"
    assert (
        "work_ledger_bus.cli append --bus-lane dev"
        in (unit["environment"]["ONEX_LEDGER_BUS_APPEND_COMMAND"])
    )
    assert unit["environment_inputs"]["ONEX_LEDGER_HOST_NAME"] == "ledger_host_name"
    assert "ONEX_LEDGER_HOST_NAME" in unit["required_environment_inputs"]


def test_an_install_naming_no_ledger_path_renders_with_no_path_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PATH_VARIABLE, raising=False)
    unit = load_units()[UNIT]
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        request(tmp_path, UNIT, ledger_path=""),
        unit,
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert PATH_VARIABLE not in service
    assert "ONEX_LEDGER_WRITE_VIA=bus" in service
    assert "ONEX_LEDGER_HOST_NAME=h-example" in service
