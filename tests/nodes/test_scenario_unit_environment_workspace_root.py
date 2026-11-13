# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every delegation scenario unit carries the workspace config root variable.

Each scenario's schedule lists ``ONEX_WORKSPACE_CONFIG_ROOT`` in its ``unit_environment``, so the
rendered unit copies the variable from the installing environment; a run that does not set it
renders no such line, which shows the line comes from the contract's list and not the renderer.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

from omnimarket.handlers.delegation_scenario_runtime import render_systemd

pytestmark = pytest.mark.unit

WORKSPACE_ROOT_VARIABLE = "ONEX_WORKSPACE_CONFIG_ROOT"
SCENARIO_IDS = ["r2", "r3", "r4", "r5", "r6"]


def _scenario(sid: str) -> tuple[object, str]:
    module = import_module(
        f"omnimarket.nodes.node_delegation_scenario_{sid}_effect"
        f".handlers.handler_scenario_{sid}"
    )
    node_module = getattr(
        module, "NODE_MODULE", f"omnimarket.nodes.node_delegation_scenario_{sid}_effect"
    )
    return module.load_schedule(), node_module


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_scenario_schedule_declares_the_workspace_config_root(sid: str) -> None:
    schedule, _ = _scenario(sid)
    assert WORKSPACE_ROOT_VARIABLE in schedule.unit_environment


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_rendered_service_carries_the_workspace_config_root(sid: str) -> None:
    schedule, node_module = _scenario(sid)
    service, _ = render_systemd(
        schedule,
        node_module=node_module,
        omni_root=Path("/srv/registry"),
        home=Path("/srv/user"),
        python=Path("/usr/bin/python3"),
        environment={WORKSPACE_ROOT_VARIABLE: "/srv/config"},
    )
    assert f'Environment="{WORKSPACE_ROOT_VARIABLE}=/srv/config"\n' in service


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_a_service_without_the_variable_set_carries_no_root_line(sid: str) -> None:
    schedule, node_module = _scenario(sid)
    service, timer = render_systemd(
        schedule,
        node_module=node_module,
        omni_root=Path("/srv/registry"),
        home=Path("/srv/user"),
        python=Path("/usr/bin/python3"),
        environment={},
    )
    assert WORKSPACE_ROOT_VARIABLE not in service
    assert WORKSPACE_ROOT_VARIABLE not in timer
