# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated caller-provided sources and the three rollup output representations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from omnimarket.events.friction_rollup import (
    ModelFrictionClassOwner,
    ModelFrictionClassRule,
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)

__all__ = [
    "ModelFrictionClassOwner",
    "ModelFrictionClassRule",
    "ModelFrictionRollupRequest",
    "ModelFrictionRollupResult",
    "ModelFrictionRollupSource",
]


class ModelFrictionRollupResult(BaseModel):
    """Exactly the old report, markdown rendering and indented JSON text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: dict[str, Any]
    markdown: str
    json_text: str
