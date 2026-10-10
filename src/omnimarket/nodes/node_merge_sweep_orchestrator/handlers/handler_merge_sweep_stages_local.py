# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge-sweep stages, resolved from the sibling nodes' own contracts (OMN-20676).

The orchestrator imports no sibling node's handler or model package. A stage is found the way the
runtime finds it: the sibling's ``contract.yaml`` names the handler for the operation, and that
handler is loaded and called with the shared request model. The effect node runs on the host that
holds the watcher state, the ledger copy and the runner, and this gateway runs where the
orchestrator does, so one serve process on that host carries a whole sweep. ``handlers`` replaces a
stage's handler (a runner that is not the local one, a clock that is not the host's).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from importlib.resources import files
from typing import Any, cast

import yaml

from omnimarket.models.merge_sweep import (
    ModelMergeSweepReadRequest,
    ModelMergeSweepReadResult,
)
from omnimarket.models.merge_sweep.model_merge_sweep_effect import (
    ModelMergeSweepLaneRunRequest,
    ModelMergeSweepLaneRunResult,
    ModelMergeSweepLoadRequest,
    ModelMergeSweepLoadResult,
)
from omnimarket.models.merge_sweep.model_merge_sweep_plan import (
    ModelMergeSweepBrief,
    ModelMergeSweepBriefRequest,
    ModelMergeSweepPlanRequest,
    ModelMergeSweepPlanResult,
)
from omnimarket.models.merge_sweep.model_merge_sweep_retry import (
    ModelMergeSweepRetryRequest,
    ModelMergeSweepRetryResult,
)

STAGE_OPERATIONS: dict[str, tuple[str, str]] = {
    "load_facts": ("node_merge_sweep_effect", "load_merge_sweep_facts"),
    "read": ("node_merge_sweep_reading_compute", "read_merge_sweep_state"),
    "plan": ("node_merge_sweep_plan_compute", "plan_merge_sweep_lanes"),
    "brief": ("node_merge_sweep_plan_compute", "render_merge_sweep_lane_brief"),
    "run_lane": ("node_merge_sweep_effect", "run_merge_sweep_lane"),
    "decide_retry": ("node_merge_sweep_plan_compute", "decide_merge_sweep_lane_retry"),
}


def _contract_handler(node: str, operation: str) -> Any:
    """Instantiate the handler a node's contract routes ``operation`` to."""
    package = f"omnimarket.nodes.{node}"
    contract = yaml.safe_load(files(package).joinpath("contract.yaml").read_text())
    for entry in contract["handler_routing"]["handlers"]:
        if entry["operation"] == operation:
            module = importlib.import_module(entry["handler"]["module"])
            return getattr(module, entry["handler"]["name"])()
    raise LookupError(f"{node} routes no handler for operation {operation}")


class LocalMergeSweepStages:
    """Call each stage's handler in this process."""

    def __init__(self, handlers: Mapping[str, Any] | None = None) -> None:
        self._handlers: dict[str, Any] = dict(handlers or {})

    def _stage(self, stage: str) -> Any:
        if stage not in self._handlers:
            self._handlers[stage] = _contract_handler(*STAGE_OPERATIONS[stage])
        return self._handlers[stage]

    def load_facts(
        self, request: ModelMergeSweepLoadRequest
    ) -> ModelMergeSweepLoadResult:
        return cast(
            ModelMergeSweepLoadResult, self._stage("load_facts").handle(request)
        )

    def read(self, request: ModelMergeSweepReadRequest) -> ModelMergeSweepReadResult:
        return cast(ModelMergeSweepReadResult, self._stage("read").handle(request))

    def plan(self, request: ModelMergeSweepPlanRequest) -> ModelMergeSweepPlanResult:
        return cast(ModelMergeSweepPlanResult, self._stage("plan").handle(request))

    def brief(self, request: ModelMergeSweepBriefRequest) -> ModelMergeSweepBrief:
        return cast(ModelMergeSweepBrief, self._stage("brief").handle(request))

    def run_lane(
        self, request: ModelMergeSweepLaneRunRequest
    ) -> ModelMergeSweepLaneRunResult:
        return cast(
            ModelMergeSweepLaneRunResult, self._stage("run_lane").handle(request)
        )

    def decide_retry(
        self, request: ModelMergeSweepRetryRequest
    ) -> ModelMergeSweepRetryResult:
        return cast(
            ModelMergeSweepRetryResult, self._stage("decide_retry").handle(request)
        )
