# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The send step of one lab-fill lane as one operation: render the brief, check it, launch (OMN-23277).

This replaces lab-fill.js's laneTask, dispatchCommand and send job. The result is the launch result, so a caller
reads one shape whether the lane was sent, refused by the brief check, or stopped by the send gate.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
)

from .model_lab_fill_send_gate import CLOSER_EXEMPT_KINDS, DEFAULT_FACT_HOSTS

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelLabFillSendRequest(BaseModel):
    """One planned lane: its item and render config, and where its brief and facts are read."""

    model_config = _FROZEN

    render: ModelLabFillLaneRenderRequest
    brief_dir: str
    ledger_path: str
    approved_work_path: str = ""
    operator_id: str = ""
    window_end_epoch_s: int | None = None
    lanes_left: int = Field(default=1, ge=1)
    watcher_state_path: str = ""
    closer_exempt_kinds: tuple[str, ...] = CLOSER_EXEMPT_KINDS
    fact_hosts: tuple[str, ...] = DEFAULT_FACT_HOSTS
    needs_hosts: tuple[str, ...] = ()
    host_reach: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
