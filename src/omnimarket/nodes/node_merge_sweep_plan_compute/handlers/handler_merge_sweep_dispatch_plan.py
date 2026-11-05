# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the repository before a sweep's planned lanes cross the dispatch boundary."""

from omnimarket.models.merge_sweep.model_merge_sweep_plan import (
    ModelMergeSweepDispatchPlanRequest,
    ModelMergeSweepLane,
    ModelMergeSweepPlanResult,
)


class HandlerMergeSweepDispatchPlan:
    """The brief and runner consume the same repository on every diagnosis."""

    def handle(
        self, request: ModelMergeSweepDispatchPlanRequest
    ) -> ModelMergeSweepPlanResult:
        def bind(lane: ModelMergeSweepLane) -> ModelMergeSweepLane:
            if lane.kind == "diagnose" and not lane.repo:
                return lane.model_copy(update={"repo": request.diagnose_repo})
            if not lane.repo:
                raise ValueError(f"{lane.kind} lane has no repository")
            return lane

        return request.plan.model_copy(
            update={
                "lanes": [bind(lane) for lane in request.plan.lanes],
                "dispatch": [bind(lane) for lane in request.plan.dispatch],
            }
        )
