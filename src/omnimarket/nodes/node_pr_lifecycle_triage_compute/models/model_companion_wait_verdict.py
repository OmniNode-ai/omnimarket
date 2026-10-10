# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a red PR only waits on its open change-control companion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCompanionWaitVerdict(BaseModel):
    """``waiting`` False carries no companion; True names it and the companion's own reds at its head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    waiting: bool
    companion: str | None = None
    companion_red: tuple[str, ...] = ()


__all__: list[str] = ["ModelCompanionWaitVerdict"]
