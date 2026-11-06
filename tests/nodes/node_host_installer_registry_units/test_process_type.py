# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22699: the floor publisher's tick unit is not clamped to launchd's Background class, and an undeclared unit still is."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    UNIT,
    make_checkout,
    plist_of,
    real_run,
    request,
)

HARNESS_UNIT = "lab-test-harness"
HARNESS_ENTRY = (
    "src/omnimarket/nodes/node_lab_test_harness_effect/runtime_lab_test_harness.py"
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def render(tmp_path: Path, unit: str, entry: str | None = None) -> dict[str, Any]:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=entry) if entry else make_checkout(branch)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path, unit=unit, project_path=str(branch), expected_revision=revision
        )
    )
    assert result.status == "success", result.stderr
    return plist_of(result.rendered)


def test_the_floor_publisher_unit_runs_in_the_standard_class(tmp_path: Path) -> None:
    """Under Background the floor step took more than 7 minutes against 43 s in the foreground (2026-11-06)."""
    assert render(tmp_path, UNIT)["ProcessType"] == "Standard"


def test_a_unit_that_declares_no_process_type_keeps_the_background_class(
    tmp_path: Path,
) -> None:
    assert load_units()[HARNESS_UNIT].get("process_type") is None
    assert render(tmp_path, HARNESS_UNIT, HARNESS_ENTRY)["ProcessType"] == "Background"


def test_a_process_type_launchd_does_not_know_is_refused(tmp_path: Path) -> None:
    units = load_units()
    units[UNIT] = {**units[UNIT], "process_type": "Realtime"}
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    result = HandlerRegistryUnits(real_run, units=units).handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert result.status == "failure"
    assert "Realtime" in result.stderr
