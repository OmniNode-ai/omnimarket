# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r8_effect <verb>``.

``run``       run scenario R8 once, write its STATUS row and emit its result on the bus (``--manual`` marks the run manual,
              which never fills a schedule slot). ``--record <file>`` also writes the run's result and STATUS row to a file,
              the record a hosted workflow uploads, and, because a hosted runner holds no ledger route, takes the place of
              the ledger append.
``schedule``  ``install`` or ``uninstall`` the node's six-hourly timer.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.handlers.delegation_readiness_scenario import scenario_main
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    NODE_MODULE,
    HandlerScenarioR8,
    build_runner,
    load_schedule,
)

RECORD_FLAG = "--record"


def _split_record(argv: Sequence[str]) -> tuple[list[str], Path | None]:
    """Take ``--record <file>`` out of ``argv``; the shared scenario parser knows nothing of it."""
    rest: list[str] = []
    record: Path | None = None
    items = list(argv)
    index = 0
    while index < len(items):
        if items[index] == RECORD_FLAG:
            if index + 1 >= len(items):
                raise SystemExit(f"{RECORD_FLAG} needs a file name")
            record = Path(items[index + 1])
            index += 2
            continue
        rest.append(items[index])
        index += 1
    return rest, record


def _build_with(
    record: Path | None,
) -> Callable[[EnumScenarioTrigger], HandlerScenarioR8]:
    def _build(trigger: EnumScenarioTrigger) -> HandlerScenarioR8:
        return build_runner(trigger=trigger, record=record)

    return _build


def main(argv: Sequence[str] | None = None) -> int:
    rest, record = _split_record(sys.argv[1:] if argv is None else argv)
    return scenario_main(
        rest,
        prog="node_delegation_scenario_r8_effect",
        node_module=NODE_MODULE,
        scenario_id="R8",
        schedule=load_schedule(),
        build=_build_with(record),
    )
