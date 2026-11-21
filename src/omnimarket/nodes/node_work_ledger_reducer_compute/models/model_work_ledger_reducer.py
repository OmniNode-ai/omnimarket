# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the work-ledger reducer compute node (OMN-22915).

One question per request: the shadow judge's verdict beside the live judge's, the guard replay of recorded
rows, the rebuild of the table from the accepted-row topic, or the replay of a trailing window of accepted rows and refused requests. The request carries exactly the sub-request
its operation names and no other.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    ModelGuardReplayRequest,
    ModelGuardReplayResult,
    ModelShadowJudgeRequest,
    ModelShadowVerdictRecord,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_rebuild import (
    ModelRebuildRequest,
    ModelRebuildResult,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_window_replay import (
    ModelWindowReplayRequest,
    ModelWindowReplayResult,
)

Operation = Literal["shadow_judge", "guard_replay", "rebuild", "window_replay"]
_FIELD_OF: dict[str, str] = {
    "shadow_judge": "shadow",
    "guard_replay": "replay",
    "rebuild": "rebuild",
    "window_replay": "window",
}


class ModelWorkLedgerReducerRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    shadow: ModelShadowJudgeRequest | None = None
    replay: ModelGuardReplayRequest | None = None
    rebuild: ModelRebuildRequest | None = None
    window: ModelWindowReplayRequest | None = None

    @model_validator(mode="after")
    def _only_the_named_sub_request(self) -> Self:
        wanted = _FIELD_OF[self.operation]
        for name in _FIELD_OF.values():
            present = getattr(self, name) is not None
            if name == wanted and not present:
                raise ValueError(
                    f"operation {self.operation} needs its {wanted} request"
                )
            if name != wanted and present:
                raise ValueError(
                    f"operation {self.operation} takes no {name} request, only {wanted}"
                )
        return self


class ModelWorkLedgerReducerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Operation
    shadow: ModelShadowVerdictRecord | None = None
    replay: ModelGuardReplayResult | None = None
    rebuild: ModelRebuildResult | None = None
    window: ModelWindowReplayResult | None = None


__all__: list[str] = ["ModelWorkLedgerReducerRequest", "ModelWorkLedgerReducerResult"]
