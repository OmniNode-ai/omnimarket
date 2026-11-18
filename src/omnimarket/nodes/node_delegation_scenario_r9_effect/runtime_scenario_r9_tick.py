# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r9_effect.runtime_scenario_r9_tick``.

The scheduled tick of scenario R9: run it once as a SCHEDULE run, print the result and write the last-run record.
R9 publishes raw bus commands and reads R8's overlay document for its control lane; it holds no secret setting, so
the tick carries no secret-store fill.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from omnimarket.handlers.delegation_readiness_scenario import (
    ScenarioTickBuild,
    scenario_tick_main,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.handler_scenario_r9 import (
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
        prog="node_delegation_scenario_r9_effect",
        scenario_id="R9",
        home_env=load_runtime().home_env,
        build=build
        or (lambda environment, trigger: build_runner(environment, trigger=trigger)),
    )


if __name__ == "__main__":
    sys.exit(main())
