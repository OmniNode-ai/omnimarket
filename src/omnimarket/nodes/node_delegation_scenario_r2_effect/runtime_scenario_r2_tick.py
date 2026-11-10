# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r2_effect.runtime_scenario_r2_tick``.

The scheduled tick of scenario R2: run it once as a SCHEDULE run, print the result and write the last-run record.
Its settings and probe key are read from the environment file the unit loads, never from the tick.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from omnimarket.handlers.delegation_readiness_scenario import (
    ScenarioTickBuild,
    scenario_tick_main,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    build_runner,
    load_runtime,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    build: ScenarioTickBuild | None = None,
) -> int:
    return scenario_tick_main(
        argv,
        prog="node_delegation_scenario_r2_effect",
        scenario_id="R2",
        home_env=load_runtime().home_env,
        build=build
        or (lambda environment, trigger: build_runner(environment, trigger=trigger)),
    )


if __name__ == "__main__":
    sys.exit(main())
