# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Red check names and the head's runs, for the change-control cascade test."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCascadeCheckFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    red: tuple[str, ...]
    runs: tuple[tuple[str | None, ...], ...] = ()
    annotations: dict[str, str | None] | None = None


__all__: list[str] = ["ModelCascadeCheckFacts"]
