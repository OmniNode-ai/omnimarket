# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22181: the guide's recommended VM passes the onex setup preflight.

The 2026-11-02 clean macOS VM walk measured 17 GB free on the guide's 4 vCPU,
8 GB, 40 GB VM from the stock image, and the retired onboarding script refused
it with "17 GB free disk (needs 20)". onex setup retired that blanket floor
(omnimarket#5813). This test replays the walk's recorded machine against the
preflight so a blanket free-disk floor cannot come back unnoticed.
"""

from __future__ import annotations

import json
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
WALK = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "onboarding_walk_2026_11_02"
    / "disk_preflight.json"
)


def walk_machine() -> ModelSetupMachineFacts:
    walk = json.loads(WALK.read_text(encoding="utf-8"))
    return ModelSetupMachineFacts.model_validate(
        {
            "platform": walk["platform"],
            "architecture": walk["architecture"],
            "macos_version": walk["macos_version"],
            "cpus": walk["cpus"],
            "ram_bytes": walk["ram_gib"] * GIB,
            "free_disk_bytes": walk["disk_free_gib"] * GIB,
            "administrator": True,
            "virtual_machine": walk["virtual_machine"],
            "rosetta": False,
            "docker_installed": False,
            "stack_running": False,
            "occupied_ports": [],
            "missing_tools": [],
        }
    )


def test_walk_2026_11_02_fixture_is_the_refused_machine() -> None:
    walk = json.loads(WALK.read_text(encoding="utf-8"))
    assert walk["disk_free_gib"] < 20 < walk["disk_total_gib"]
    assert walk["retired_refusal"] == "17 GB free disk (needs 20)"


def test_walk_2026_11_02_guide_vm_passes_preflight(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(collect=lambda _: walk_machine()).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "success", result.fix
    assert "free_disk_bytes" not in result.evidence
