# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send one planned lab-fill lane: render its brief, pass the send gate and the brief check, launch it (OMN-23277)."""

from __future__ import annotations

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
    ModelLabFillLaneRenderRequest,
)

from ..models import ModelLabFillLaunchRequest, ModelLabFillLaunchResult
from ..models.model_lab_fill_send import ModelLabFillSendRequest
from .handler_lab_fill_launch import HandlerLabFillLaunch


class HandlerLabFillSend:
    """The whole send step; the launch handler holds the gate, the brief rules and the runner."""

    def __init__(self, launch: HandlerLabFillLaunch | None = None) -> None:
        self._launch = launch if launch is not None else HandlerLabFillLaunch()

    def handle(self, request: ModelLabFillSendRequest) -> ModelLabFillLaunchResult:
        render = ModelLabFillLaneRenderRequest.model_validate(request.render)
        rendered = HandlerLabFillLaneRender().handle(render)
        return self._launch.handle(
            ModelLabFillLaunchRequest(
                launch=rendered.launch,
                kind=render.item.kind,
                brief=rendered.brief,
                brief_dir=request.brief_dir,
                ledger_path=request.ledger_path,
                approved_work_path=request.approved_work_path,
                operator_id=request.operator_id,
                window_end_epoch_s=request.window_end_epoch_s,
                lanes_left=request.lanes_left,
                watcher_state_path=request.watcher_state_path,
                closer_exempt_kinds=request.closer_exempt_kinds,
                fact_hosts=request.fact_hosts,
                needs_hosts=request.needs_hosts,
                host_reach=request.host_reach,
            )
        )
