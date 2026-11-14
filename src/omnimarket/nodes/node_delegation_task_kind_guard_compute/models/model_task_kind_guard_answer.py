# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.enum_task_kind_guard import (
    EnumTaskKindGuardReason,
    EnumTaskKindGuardRefusal,
    EnumTaskKindGuardRoute,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_tally import (
    ModelTaskKindGuardTally,
)


class ModelTaskKindGuardAnswer(BaseModel):
    """The guard's decision for one local answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    route: EnumTaskKindGuardRoute | None = None
    reason: EnumTaskKindGuardReason | None = None
    refusal: EnumTaskKindGuardRefusal | None = None
    detail: str = ""
    tally: ModelTaskKindGuardTally = ModelTaskKindGuardTally()
    scored_correct: bool | None = None
    local_backend_id: str | None = None
    accuracy: float | None = None
    accuracy_lower_bound: float | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> ModelTaskKindGuardAnswer:
        if self.ok:
            if self.refusal is not None:
                msg = "ok answer must not carry a refusal"
                raise ValueError(msg)
            if self.route is None or self.reason is None:
                msg = "ok answer must carry route and reason"
                raise ValueError(msg)
        else:
            if self.refusal is None:
                msg = "refusal answer must carry a refusal"
                raise ValueError(msg)
            if self.route is not None or self.reason is not None:
                msg = "refusal answer must not carry route or reason"
                raise ValueError(msg)
            if not self.detail:
                msg = "refusal answer must carry a non-empty detail"
                raise ValueError(msg)
        return self


__all__: list[str] = ["ModelTaskKindGuardAnswer"]
