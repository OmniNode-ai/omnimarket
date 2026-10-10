# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stages of a merge sweep, as the orchestrator calls them (OMN-20676).

Each stage is an operation of a sibling node (the effect node's ``load_merge_sweep_facts`` and
``run_merge_sweep_lane``, the reading node's ``read_merge_sweep_state``, the plan node's
``plan_merge_sweep_lanes``, ``render_merge_sweep_lane_brief`` and
``decide_merge_sweep_lane_retry``). The orchestrator holds none of their logic.
"""

from __future__ import annotations

from typing import Protocol

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


class ProtocolMergeSweepStages(Protocol):
    def load_facts(
        self, request: ModelMergeSweepLoadRequest
    ) -> ModelMergeSweepLoadResult: ...

    def read(
        self, request: ModelMergeSweepReadRequest
    ) -> ModelMergeSweepReadResult: ...

    def plan(
        self, request: ModelMergeSweepPlanRequest
    ) -> ModelMergeSweepPlanResult: ...

    def brief(self, request: ModelMergeSweepBriefRequest) -> ModelMergeSweepBrief: ...

    def run_lane(
        self, request: ModelMergeSweepLaneRunRequest
    ) -> ModelMergeSweepLaneRunResult: ...

    def decide_retry(
        self, request: ModelMergeSweepRetryRequest
    ) -> ModelMergeSweepRetryResult: ...
