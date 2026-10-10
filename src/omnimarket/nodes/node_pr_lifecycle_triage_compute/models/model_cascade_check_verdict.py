# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which red checks are in the change-control cascade, and whether the red is only that cascade."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCascadeCheckVerdict(BaseModel):
    """``cascade_checks`` are the given names for which ``is_cascade_check`` holds, in order; ``cascade_only``
    is ``cascade_only(red)``: the summary and reviewer family set aside, at least one red left, all of them cascade."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cascade_checks: tuple[str, ...]
    cascade_only: bool


__all__: list[str] = ["ModelCascadeCheckVerdict"]
