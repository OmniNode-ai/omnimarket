# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observed nonempty stratum with insufficient eligible candidates."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDelegationEvalShortfall(BaseModel):
    """Observed nonempty stratum with insufficient eligible candidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stratum: str
    quota: int
    available: int
    taken: int
