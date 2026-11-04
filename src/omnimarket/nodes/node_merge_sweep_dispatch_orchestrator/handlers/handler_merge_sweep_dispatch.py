# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic replacement of the sweep workflow's dispatch phase.

The reader owns planning and receipt filtering; the lane contract owns admission,
brief checking, runner arguments and retries. This node only binds the plan's
identity to its record and calls the existing public bus ingress. No fallback
repository, direct runner call, model agent or rewritten record exists here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.lane_dispatch.model_lane_dispatch_effect import (
    ModelLaneDispatchRequest,
    ModelLaneDispatchResult,
)
from omnimarket.nodes.node_lane_dispatch_effect import dispatch as dispatch_over_bus

from ..models.model_merge_sweep_dispatch import (
    ModelMergeSweepDispatchLane,
    ModelMergeSweepDispatchOutcome,
    ModelMergeSweepDispatchRequest,
    ModelMergeSweepDispatchResult,
)


class HandlerMergeSweepDispatch:
    """Bind every planned lane to one immutable dispatch instruction record."""

    def __init__(
        self,
        dispatch: Callable[[ModelLaneDispatchRequest], ModelLaneDispatchResult]
        | None = None,
    ) -> None:
        self._dispatch = dispatch if dispatch is not None else dispatch_over_bus
        contract = yaml.safe_load(
            files("omnimarket.nodes.node_merge_sweep_dispatch_orchestrator")
            .joinpath("contract.yaml")
            .read_text()
        )
        self._kinds: dict[str, str] = contract["dispatch_kinds"]

    def _identity_error(
        self, lane: ModelMergeSweepDispatchLane, parent: str, spec: dict[str, Any]
    ) -> tuple[str, str] | None:
        if not lane.repo or not lane.repo.strip():
            return "REMOTE-LANE-JOB-SPEC-REFUSED", "planned lane is missing repo"
        if lane.kind not in self._kinds:
            return (
                "SWEEP-KIND-REFUSED",
                f"lane kind {lane.kind} is not dispatched by the merge sweep",
            )
        expected: dict[str, Any] = {
            "lane": lane.lane,
            "ticket": lane.ticket,
            "parent": parent,
            "lane_kind": self._kinds[lane.kind],
            "pr": list(lane.prs),
        }
        # A reader may name the short repository; its record always names owner/repo.
        repo = spec.get("repo")
        if not isinstance(repo, str) or (
            repo != lane.repo
            and ("/" in lane.repo or repo.rsplit("/", 1)[-1] != lane.repo)
        ):
            return (
                "DISPATCH-IDENTITY-REFUSED",
                "planned repo differs from dispatch record",
            )
        if lane.host is not None:
            expected["host"] = lane.host
        for field, value in expected.items():
            if spec.get(field) != value:
                return (
                    "DISPATCH-IDENTITY-REFUSED",
                    f"planned {field} differs from dispatch record",
                )
        if lane.host and spec.get("placement") != "lab":
            return (
                "REMOTE-LANE-HOST-REFUSED",
                "a plan-pinned host requires lab placement",
            )
        if lane.host_why and lane.host_why not in str(spec.get("task", "")):
            return (
                "DISPATCH-IDENTITY-REFUSED",
                "the pinned host reason is missing from the task",
            )
        return None

    def handle(
        self, request: ModelMergeSweepDispatchRequest
    ) -> ModelMergeSweepDispatchResult:
        outcomes: list[ModelMergeSweepDispatchOutcome] = []
        names = [lane.lane for lane in request.lanes]
        for lane in request.lanes[: request.max_lanes]:
            result: ModelLaneDispatchResult
            try:
                raw = json.loads(lane.record_json)
                spec = raw.get("spec", raw) if isinstance(raw, dict) else {}
                error = self._identity_error(
                    lane, request.parent, spec if isinstance(spec, dict) else {}
                )
                if names.count(lane.lane) > 1:
                    error = (
                        "DISPATCH-IDENTITY-REFUSED",
                        "duplicate planned lane identity",
                    )
                if error:
                    result = ModelLaneDispatchResult(
                        status="refused", error_code=error[0], detail=error[1]
                    )
                else:
                    result = self._dispatch(
                        ModelLaneDispatchRequest(
                            record_json=lane.record_json,
                            output_dir=str(Path(request.output_dir) / lane.lane),
                            execute=request.execute,
                        )
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                result = ModelLaneDispatchResult(
                    status="refused", error_code="DISPATCH-UNREADABLE", detail=str(exc)
                )
            outcomes.append(
                ModelMergeSweepDispatchOutcome(
                    **result.model_dump(),
                    lane=lane.lane,
                    kind=lane.kind,
                    repo=lane.repo,
                    ticket=lane.ticket,
                    host=lane.host,
                )
            )
        return ModelMergeSweepDispatchResult(
            ok=all(outcome.status != "refused" for outcome in outcomes),
            outcomes=tuple(outcomes),
            deferred=max(0, len(request.lanes) - request.max_lanes),
        )
