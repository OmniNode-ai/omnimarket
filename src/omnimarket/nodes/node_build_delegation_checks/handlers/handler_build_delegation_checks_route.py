# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBuildDelegationChecksRoute: the handler the runtime builds with no argument, its ports made from the overlay.

The ports are built on the first request: the settings from the settings store's overlay, the probe runner from the overlay's
check routes (or, for a replay of earlier results, the recorded results file ``ONEX_BUILD_DELEGATION_CHECKS_REPLAY`` names), the store and the merge source under the registry root, the sink on the bus lane the overlay names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from omnimarket.nodes.node_build_delegation_checks.adapters.bus_sink import (
    BusRegressionSink,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.command_check_runner import (
    CommandCheckRunner,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.file_build_store import (
    FileBuildStore,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    GitMergeSource,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    RecordedResultsRunner,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    ModelBuildChecksResult,
    ModelCheckOutcome,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_build_delegation_checks.overlay import load_overlay
from omnimarket.nodes.node_build_delegation_checks.protocols.protocol_build_checks import (
    ProtocolCheckRunner,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_TOPICS: dict[str, str] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))[
    "topics"
]
REGRESSION_TOPIC = _TOPICS["regression_detected"]
ALARM_TOPIC = _TOPICS["alarm_delivery_requested"]
RESULT_TOPIC = _TOPICS["build_checks_recorded"]
REPLAY_ENV = "ONEX_BUILD_DELEGATION_CHECKS_REPLAY"
OMNI_HOME_ENV = "OMNI_HOME"


def recorded_runner(path: Path) -> RecordedResultsRunner:
    """A runner over a recorded results file: ``{"builds": {build_id: {check_id: {reading, run_id, detail}}}}``."""
    raw = json.loads(path.read_text(encoding="utf-8"))["builds"]
    return RecordedResultsRunner(
        {
            build: {
                check: ModelCheckOutcome.model_validate(item)
                for check, item in checks.items()
            }
            for build, checks in raw.items()
        }
    )


class HandlerBuildDelegationChecksRoute:
    def handle(self, request: ModelRebuildCompletedObserved) -> ModelBuildChecksResult:
        settings = load_overlay()
        home = os.environ.get(OMNI_HOME_ENV, "").strip()
        if not home:
            raise OSError(f"{OMNI_HOME_ENV} names no registry root")
        root = Path(home)
        replay = os.environ.get(REPLAY_ENV, "").strip()
        runner: ProtocolCheckRunner = (
            recorded_runner(Path(replay))
            if replay
            else CommandCheckRunner(settings.check_routes)
        )
        sink = BusRegressionSink(
            regression_topic=REGRESSION_TOPIC,
            alarm_topic=ALARM_TOPIC,
            result_topic=RESULT_TOPIC,
            bus="kafka",
            lane=settings.bus_lane,
            workspace=root,
        )
        handler = HandlerBuildDelegationChecks(
            settings=settings,
            runner=runner,
            store=FileBuildStore(
                root / settings.history_relpath, settings.history_keep
            ),
            merges=GitMergeSource(root / settings.merge_clone_relpath),
            sink=sink,
        )
        return handler.handle(request)


__all__ = [
    "ALARM_TOPIC",
    "REGRESSION_TOPIC",
    "REPLAY_ENV",
    "RESULT_TOPIC",
    "HandlerBuildDelegationChecksRoute",
    "recorded_runner",
]
