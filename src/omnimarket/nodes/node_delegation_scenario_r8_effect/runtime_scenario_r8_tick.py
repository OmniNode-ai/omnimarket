# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r8_effect.runtime_scenario_r8_tick``.

The scheduled tick of scenario R8: run it once as a SCHEDULE run, print the result and write the last-run record.
The probe tenant key is filled from the node's settings overlay through the secret store, as ``runtime_scenario_r8.py``
fills it; the tenant address still comes from the environment file.
R8 reads the probe tenant's address and key from its environment, so the tick carries no secret-store fill.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from omnimarket.handlers.delegation_readiness_scenario import (
    ScenarioTickBuild,
    scenario_tick_main,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
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
        prog="node_delegation_scenario_r8_effect",
        scenario_id="R8",
        home_env=load_runtime().home_env,
        build=build
        or (lambda environment, trigger: build_runner(environment, trigger=trigger)),
    )


if __name__ == "__main__":
    sys.exit(main())
