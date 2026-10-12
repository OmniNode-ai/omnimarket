# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn a scheduled fire into the sweep's bus input; the runtime owns publication.

The constructor binds deployment values through the contract overlay. The handler reads only
that supplied template and the fire, and never loads sweep facts or accesses the bus.
"""

from __future__ import annotations

from omnimarket.models.merge_sweep.model_merge_sweep_run import (
    ModelMergeSweepRunRequest,
)
from omnimarket.models.merge_sweep.model_merge_sweep_schedule_config import (
    scheduled_request_config,
)
from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire


class HandlerMergeSweepScheduledRequest:
    """Pure translation of one fire to one typed run request, with no bus access."""

    def __init__(
        self,
        template: ModelMergeSweepRunRequest | None = None,
        workflow: str | None = None,
    ) -> None:
        if template is None:
            configured_workflow, template = scheduled_request_config()
            workflow = workflow or configured_workflow
        if not workflow:
            raise ValueError("scheduled sweep workflow must be supplied")
        self._template = template
        self._workflow = workflow

    def handle(self, request: ModelScheduledFire) -> ModelMergeSweepRunRequest | None:
        if request.workflow != self._workflow:
            return None
        return self._template.model_copy(
            update={
                "now": request.tick_now.isoformat().replace("+00:00", "Z"),
                "lane": f"{self._template.lane}-{request.fire_id}"
                if self._template.lane
                else request.fire_id,
            }
        )


__all__: list[str] = ["HandlerMergeSweepScheduledRequest"]
