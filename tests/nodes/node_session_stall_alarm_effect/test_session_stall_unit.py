# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall alarm runs from a launchd unit the host installer declares, every 300 seconds (OMN-25405, criterion 3)."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

pytestmark = pytest.mark.unit

UNIT = "session-stall-alarm"
LABEL = "ai.omninode.session-stall-alarm"
ROOT = Path(__file__).resolve().parents[3]
MODULE = "omnimarket.nodes.node_session_stall_alarm_effect.runtime_session_stall_tick"


def test_the_unit_is_declared_as_a_launchd_tick_every_300_seconds() -> None:
    declared = load_units()[UNIT]
    assert declared["platform"] == "launchd"
    assert declared["kind"] == "registry_module_tick"
    assert declared["label"] == LABEL
    assert declared["schedule"]["start_interval"] == 300
    assert declared["module"] == MODULE
    assert (ROOT / declared["entry_file"]).is_file()
    # A hung read ends before the next tick is due.
    assert declared["run_timeout_s"] < 300


def test_the_rendered_plist_runs_the_tick_module_every_300_seconds(
    tmp_path: Path,
) -> None:
    request = ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    text = handler.render(request, load_units()[UNIT], tmp_path, "uv", "python")
    plist = plistlib.loads(text.encode())
    assert plist["Label"] == LABEL
    assert plist["StartInterval"] == 300
    assert MODULE in plist["ProgramArguments"] or MODULE in " ".join(
        plist["ProgramArguments"]
    )
    assert plist["EnvironmentVariables"]["ONEX_SKILL_OVERLAY_ROOTS"]
    arguments = " ".join(plist["ProgramArguments"])
    assert "--omni-home" in arguments
    assert "--state-dir" in arguments
