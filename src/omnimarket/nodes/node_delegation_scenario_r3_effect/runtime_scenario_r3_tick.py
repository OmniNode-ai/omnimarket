# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r3_effect.runtime_scenario_r3_tick``.

The scheduled tick of scenario R3: run it once as a SCHEDULE run, print the result and write the last-run record.
The probe key is filled from the node's settings overlay through the secret store, as ``runtime_scenario_r3.py``
fills it.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from omnimarket.handlers.delegation_readiness_scenario import (
    ScenarioTickBuild,
    scenario_tick_main,
)
from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.nodes.node_delegation_scenario_r3_effect.handlers.handler_scenario_r3 import (
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
        prog="node_delegation_scenario_r3_effect",
        scenario_id="R3",
        home_env=load_runtime().home_env,
        build=build
        or (
            lambda environment, trigger: build_runner(
                environment,
                trigger=trigger,
                settings_store=SecretStoreSettingsReader(),
            )
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
