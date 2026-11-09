# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r5_effect <verb>``.

``run``       run scenario R5 once, write its STATUS row and emit its result on the bus (``--manual`` marks the run manual,
              which never fills a schedule slot).
``schedule``  ``install`` or ``uninstall`` the node's hourly timer.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.handlers.delegation_readiness_scenario import scenario_main
from omnimarket.nodes.node_delegation_scenario_r5_effect.handlers.handler_scenario_r5 import (
    NODE_MODULE,
    build_runner,
    load_schedule,
)


def main(argv: Sequence[str] | None = None) -> int:
    return scenario_main(
        argv,
        prog="node_delegation_scenario_r5_effect",
        node_module=NODE_MODULE,
        scenario_id="R5",
        schedule=load_schedule(),
        build=lambda trigger: build_runner(trigger=trigger),
    )
