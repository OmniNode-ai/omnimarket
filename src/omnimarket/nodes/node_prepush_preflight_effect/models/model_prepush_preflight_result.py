# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the pre-push stage hooks said about a pending push range."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_prepush_preflight_effect.models.model_prepush_refusal import (
    ModelPrepushRefusal,
)


class ModelPrepushPreflightResult(BaseModel):
    """``status``: ``ok`` every pre-push hook passed, ``refused`` at least one refused (``refusals``),
    ``no-config`` no pre-commit configuration, ``no-range`` no range could be named, ``empty-range`` nothing
    is pending, ``error`` the hooks could not be run (``error_tail``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ok", "refused", "no-config", "no-range", "empty-range", "error"]
    range_from: str | None = None
    range_to: str | None = None
    refusals: tuple[ModelPrepushRefusal, ...] = ()
    error_tail: str = ""
