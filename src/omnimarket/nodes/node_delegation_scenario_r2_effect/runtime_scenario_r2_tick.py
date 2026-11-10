# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scheduled tick of scenario R2, the entry the registry installer renders (OMN-23432).

``python -m omnimarket.nodes.node_delegation_scenario_r2_effect.runtime_scenario_r2_tick
--omni-home <root> --state-dir <dir>`` runs scenario R2 once as a SCHEDULE run: the environment
is the process environment with the tick's home set, its settings filled from the secret store
(through the machine's identity) where the environment does not hold them, and the result goes
to the ledger, the bus and stdout. A missing ledger route refuses with 65; the record in
``--state-dir/last-run.json`` holds the conclusion and the names the store did not supply,
never a value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.delegation_scenario_runtime import write_atomic
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    HandlerScenarioR2,
    build_runner,
    load_runtime,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2_settings import (
    HandlerSecretStoreSettings,
    ProtocolSettingStore,
    fill_settings,
)

NODE = "node_delegation_scenario_r2_effect"
EXIT_REFUSED = 65
RunnerFactory = Callable[[Mapping[str, str], EnumScenarioTrigger], HandlerScenarioR2]


def _runner_factory(
    environment: Mapping[str, str], trigger: EnumScenarioTrigger
) -> HandlerScenarioR2:
    return build_runner(env=environment, trigger=trigger)


def _record(
    state_dir: Path,
    *,
    scenario: str,
    conclusion: str,
    unresolved: list[str],
    detail: str | None = None,
) -> None:
    body: dict[str, object] = {
        "scenario": scenario,
        "conclusion": conclusion,
        "unresolved_settings": list(unresolved),
        "ran_at": datetime.now(UTC).isoformat(),
    }
    if detail is not None:
        body["refused"] = detail
    write_atomic(state_dir / "last-run.json", json.dumps(body, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    store: ProtocolSettingStore | None = None,
    runner_factory: RunnerFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{NODE}.tick",
        description="one scheduled tick of scenario R2: the run's settings from the "
        "environment and, where absent, the secret store",
    )
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    runtime = load_runtime()
    environment: dict[str, str] = dict(os.environ)  # ONEX_FLAG_EXEMPT
    environment[runtime.home_env] = str(args.registry_root)
    filled, unresolved = asyncio.run(
        fill_settings(
            environment,
            runtime,
            store if store is not None else HandlerSecretStoreSettings(),
        )
    )
    factory = runner_factory if runner_factory is not None else _runner_factory
    try:
        result = factory(filled, EnumScenarioTrigger.SCHEDULE).run()
    except RuntimeError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        _record(
            args.state_dir,
            scenario=runtime.scenario_id,
            conclusion="refused",
            unresolved=list(unresolved),
            detail=str(exc),
        )
        return EXIT_REFUSED
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    _record(
        args.state_dir,
        scenario=runtime.scenario_id,
        conclusion=result.conclusion.value,
        unresolved=list(unresolved),
    )
    return 0 if result.conclusion.value == "success" else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
