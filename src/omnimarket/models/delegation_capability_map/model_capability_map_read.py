# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capability-map read one routing request carries (OMN-20170)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)


class ModelCapabilityMapRead(BaseModel):
    """What the caller read at its I/O boundary for one request (OMN-20170).

    ``cells`` is None when the capability map could not be read;
    ``eval_line_verdict`` is the class's latest false-pass line verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cells: tuple[ModelCapabilityCell, ...] | None
    eval_line_verdict: str | None
