# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt facts stay loose because runner refusals must be named, not crash."""

from pydantic import BaseModel, ConfigDict

from .model_lab_fill_plan import _CAMEL


class ModelLabFillPlacementRequest(BaseModel):
    """No receipt means pending; no detach means not started."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    planned: tuple[dict[str, object], ...]
    dispatched: tuple[dict[str, object] | None, ...] = ()
    receipts: tuple[dict[str, object] | None, ...] = ()
    project_id: str = ""


class ModelLabFillPlacementResult(BaseModel):
    """Pins and window exhaustion are separate decisions for the caller."""

    model_config = _CAMEL
    placements: tuple[dict[str, object], ...]
    deferred: tuple[dict[str, object], ...]
    failure: dict[str, object] | None
