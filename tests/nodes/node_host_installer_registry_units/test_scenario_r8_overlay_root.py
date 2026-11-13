# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The R8 scenario unit pins the overlays root its secret store block is found under (OMN-23746).

The R8 tick reads its store block from ``<root>/node_delegation_scenario_r8_effect/overlay.yaml``. The unit declares the
root (the overlays canonical clone), may be given another root on install, and refuses a root that is a worktree.
"""

from __future__ import annotations

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)

UNIT = "scenario-r8-systemd"
ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"


def test_the_unit_declares_the_overlay_root_and_refuses_a_worktree_root() -> None:
    spec = load_units()[UNIT]
    assert spec["environment"][ROOTS_ENV] == "{overlays_root}"
    assert ROOTS_ENV in spec["environment_path_overrides"]
    assert ROOTS_ENV in spec["environment_refuse_worktree_paths"]


def test_failing_control_a_sibling_unit_without_the_declaration_is_detected() -> None:
    spec = load_units()["scenario-r4-systemd"]
    assert ROOTS_ENV not in (spec.get("environment") or {})
