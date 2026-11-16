# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r7_effect <verb>``.

``run``       run scenario R7 once, write its STATUS row and emit its result on the bus (``--manual`` marks the run manual,
              which never fills a schedule slot).
``schedule``  ``install`` or ``uninstall`` the node's daily timer.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.handlers.delegation_readiness_scenario import scenario_main
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r7_effect.handlers.handler_scenario_r7 import (
    NODE_MODULE,
    HandlerScenarioR7,
    build_runner,
    load_schedule,
)


def _build(trigger: EnumScenarioTrigger) -> HandlerScenarioR7:
    return build_runner(trigger=trigger)


def main(argv: Sequence[str] | None = None) -> int:
    return scenario_main(
        argv,
        prog="node_delegation_scenario_r7_effect",
        node_module=NODE_MODULE,
        scenario_id="R7",
        schedule=load_schedule(),
        build=lambda trigger: _build(trigger),
    )
